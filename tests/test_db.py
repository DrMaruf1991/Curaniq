"""
CURANIQ — Database Integration Tests (FIX-31)
==============================================

Tests run against a REAL database (temp-file SQLite). No mocking. No in-memory
shortcuts that would mask race conditions or schema bugs.

In production, set CURANIQ_DATABASE_URL=postgresql://... and run the same
tests against Postgres via docker-compose:
    docker compose up -d postgres
    CURANIQ_DATABASE_URL=postgresql://curaniq:curaniq@localhost:5432/curaniq pytest tests/test_db.py

The tests cover:
    1. Tenant repository — default tenant lifecycle
    2. Source repository — upsert, list, license expiry, sync tracking
    3. Evidence repository — content-hash supersession, retraction, version chain
    4. Audit repository — hash chain construction, tamper detection, replay
    5. Audit ledger integration with the existing JSONL/Postgres backend factory
    6. Source registry DB hydration
    7. Boot-time fail-closed in clinician_prod when DB unreachable
"""
from __future__ import annotations
import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ─── Per-test isolated DB ───────────────────────────────────────────────────

@pytest.fixture
def tmp_db(monkeypatch):
    """Each test gets a fresh SQLite file. Engine reset between tests."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    url = f"sqlite:///{tmp.name}"
    monkeypatch.setenv("CURANIQ_DATABASE_URL", url)
    monkeypatch.setenv("CURANIQ_ENV", "demo")  # Don't trigger fail-closed in tests

    from curaniq.db.engine import reset_engine_for_tests
    reset_engine_for_tests()

    from curaniq.db import init_db
    init_db(drop_existing=True)
    yield url

    try:
        os.unlink(tmp.name)
    except OSError:
        pass
    reset_engine_for_tests()


# ─── 1. Tenant repository ────────────────────────────────────────────────────

def test_tenant_default_is_idempotent(tmp_db):
    from curaniq.db import get_session, TenantRepository
    with get_session() as s:
        t1 = TenantRepository(s).ensure_default()
        t2 = TenantRepository(s).ensure_default()
        assert t1.id == t2.id
        assert t1.slug == "default"


def test_tenant_lookup_by_slug(tmp_db):
    from curaniq.db import get_session, TenantRepository
    with get_session() as s:
        TenantRepository(s).create(name="Tashkent General", slug="tg", jurisdiction="UZ")
    with get_session() as s:
        found = TenantRepository(s).by_slug("tg")
        assert found is not None
        assert found.jurisdiction == "UZ"


# ─── 2. Source repository ────────────────────────────────────────────────────

def test_source_upsert_is_idempotent(tmp_db):
    from curaniq.db import get_session, SourceRepository
    with get_session() as s:
        repo = SourceRepository(s)
        s1 = repo.upsert(source_type="ncbi", display_name="NCBI", authority_level=3,
                         jurisdictions=["INT"], allowed_claim_types=["safety_signal"])
    with get_session() as s:
        repo = SourceRepository(s)
        s2 = repo.upsert(source_type="ncbi", display_name="NCBI PubMed", authority_level=2,
                         jurisdictions=["INT", "US"], allowed_claim_types=["safety_signal", "efficacy"])
        assert s1.id == s2.id
        assert s2.authority_level == 2  # Updated, not duplicated.
        assert "US" in s2.jurisdictions


def test_source_sync_failure_auto_degrades(tmp_db):
    """3 consecutive failures must auto-degrade the source status."""
    from curaniq.db import get_session, SourceRepository
    from curaniq.db.models import SyncOutcomeEnum, SourceStatusEnum
    with get_session() as s:
        repo = SourceRepository(s)
        src = repo.upsert(source_type="nice", display_name="NICE", authority_level=1)
        sid = src.id
    for _ in range(3):
        with get_session() as s:
            SourceRepository(s).mark_synced(sid, SyncOutcomeEnum.FAILED,
                                            error_class="HTTPError", error_message="403")
    with get_session() as s:
        src = SourceRepository(s).by_type("nice")
        assert src.consecutive_failures == 3
        assert src.status == SourceStatusEnum.DEGRADED.value


def test_source_license_expiry_alarm(tmp_db):
    """Sources with license expiring soon must be flagged."""
    from datetime import datetime, timezone, timedelta
    from curaniq.db import get_session, SourceRepository
    expiring = datetime.now(timezone.utc) + timedelta(days=10)
    far = datetime.now(timezone.utc) + timedelta(days=400)
    with get_session() as s:
        repo = SourceRepository(s)
        repo.upsert(source_type="nccn", display_name="NCCN", authority_level=1,
                    license_status="licensed", license_expires_at=expiring)
        repo.upsert(source_type="who", display_name="WHO", authority_level=2,
                    license_status="open", license_expires_at=far)
    with get_session() as s:
        flagged = SourceRepository(s).check_license_expiry(threshold_days=30)
        types = {f.source_type for f in flagged}
        assert "nccn" in types and "who" not in types


# ─── 3. Evidence repository ──────────────────────────────────────────────────

def test_evidence_content_hash_supersedes_on_change(tmp_db):
    """Same external_id with different content must version-bump and supersede."""
    from curaniq.db import get_session, SourceRepository, EvidenceRepository
    with get_session() as s:
        sid = SourceRepository(s).upsert(source_type="ncbi", display_name="NCBI",
                                         authority_level=3).id
    with get_session() as s:
        ev1, new1 = EvidenceRepository(s).upsert_evidence(
            source_id=sid, external_id="PMID:12345",
            snippet="version 1 of the abstract", tier="moderate",
        )
        eid1 = ev1.id
        assert new1 is True
    # Same content — no new row
    with get_session() as s:
        ev2, new2 = EvidenceRepository(s).upsert_evidence(
            source_id=sid, external_id="PMID:12345",
            snippet="version 1 of the abstract", tier="moderate",
        )
        assert new2 is False
        assert ev2.id == eid1
        assert ev2.version == 1
    # Changed content — new row, prior superseded
    with get_session() as s:
        ev3, new3 = EvidenceRepository(s).upsert_evidence(
            source_id=sid, external_id="PMID:12345",
            snippet="version 2 with corrected dose", tier="moderate",
        )
        assert new3 is True
        assert ev3.id != eid1
        assert ev3.version == 2
        assert ev3.is_current is True
        prior = EvidenceRepository(s).get(eid1)
        assert prior.is_current is False
        assert prior.is_superseded is True
        assert prior.superseded_by_id == ev3.id


def test_evidence_retraction_marks_not_current(tmp_db):
    from curaniq.db import get_session, SourceRepository, EvidenceRepository
    with get_session() as s:
        sid = SourceRepository(s).upsert(source_type="ncbi", display_name="NCBI",
                                         authority_level=3).id
    with get_session() as s:
        ev, _ = EvidenceRepository(s).upsert_evidence(
            source_id=sid, external_id="PMID:99999",
            snippet="findings retracted by authors", tier="low",
        )
        eid = ev.id
    with get_session() as s:
        EvidenceRepository(s).mark_retracted(eid, notice="Retracted: data fabrication")
    with get_session() as s:
        ev = EvidenceRepository(s).get(eid)
        assert ev.is_retracted is True
        assert ev.is_current is False
        assert "fabrication" in ev.retraction_notice


# ─── 4. Audit repository — hash chain ────────────────────────────────────────

def test_audit_chain_grows_correctly(tmp_db):
    from curaniq.db import get_session, AuditRepository, TenantRepository
    with get_session() as s:
        TenantRepository(s).ensure_default()
    with get_session() as s:
        ar = AuditRepository(s)
        e1 = ar.append(event_type="query_received", payload={"q": "metformin?"})
        e2 = ar.append(event_type="evidence_retrieved", payload={"n": 5})
        e3 = ar.append(event_type="query_answered", payload={"refused": False})
        assert e1.sequence == 1
        assert e2.sequence == 2
        assert e3.sequence == 3
        assert e2.prev_chain_hash == e1.chain_hash
        assert e3.prev_chain_hash == e2.chain_hash


def test_audit_chain_verifies_intact(tmp_db):
    from curaniq.db import get_session, AuditRepository, TenantRepository
    with get_session() as s:
        TenantRepository(s).ensure_default()
    with get_session() as s:
        ar = AuditRepository(s)
        for i in range(5):
            ar.append(event_type="evidence_retrieved", payload={"i": i})
    with get_session() as s:
        ok, err = AuditRepository(s).verify_chain()
        assert ok, f"chain verification failed: {err}"


def test_audit_chain_detects_tampering(tmp_db):
    """If someone modifies a row's payload_json, verify_chain must catch it."""
    from curaniq.db import get_session, AuditRepository, TenantRepository
    from curaniq.db.models import AuditEvent
    from sqlalchemy import select

    with get_session() as s:
        TenantRepository(s).ensure_default()
    with get_session() as s:
        ar = AuditRepository(s)
        ar.append(event_type="query_received", payload={"q": "foo"})
        ar.append(event_type="query_answered", payload={"r": "ok"})

    # Tamper with the first row's payload directly
    with get_session() as s:
        first = s.execute(select(AuditEvent).order_by(AuditEvent.sequence)).scalars().first()
        first.payload_json = '{"q":"FORGED"}'

    with get_session() as s:
        ok, err = AuditRepository(s).verify_chain()
        assert ok is False
        assert err and ("payload" in err or "chain" in err)


def test_audit_export_is_complete(tmp_db):
    from curaniq.db import get_session, AuditRepository, TenantRepository
    with get_session() as s:
        TenantRepository(s).ensure_default()
    with get_session() as s:
        ar = AuditRepository(s)
        for i in range(3):
            ar.append(event_type="query_answered", payload={"i": i})
    with get_session() as s:
        export = AuditRepository(s).export()
        assert len(export) == 3
        for row in export:
            assert "chain_hash" in row and "payload_hash" in row and "sequence" in row


def test_audit_concurrent_writes_preserve_chain(tmp_db):
    """FIX-32 regression guard: 8 concurrent threads × 5 appends each must
    produce 40 audit rows with an intact hash chain. Earlier versions failed
    here with `UNIQUE constraint failed: audit_chain_heads.tenant_id` (head
    creation race) and `UNIQUE constraint failed: audit_events.tenant_id,
    audit_events.sequence` (sequence number race).

    This test pins the per-tenant lock + retry behavior so the regression
    cannot return silently.
    """
    import threading
    from curaniq.db import get_session, AuditRepository, TenantRepository

    with get_session() as s:
        TenantRepository(s).ensure_default()

    errs: list = []
    def worker(n: int):
        try:
            for i in range(5):
                with get_session() as s:
                    AuditRepository(s).append(
                        event_type="evidence_retrieved",
                        payload={"worker": n, "iter": i},
                    )
        except Exception as e:
            errs.append((n, type(e).__name__, str(e)[:120]))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=30)

    assert all(not t.is_alive() for t in threads), "thread deadlock"
    assert not errs, f"concurrent append errors: {errs}"

    with get_session() as s:
        ar = AuditRepository(s)
        assert ar.count() == 40, f"expected 40 events, got {ar.count()}"
        ok, err = ar.verify_chain()
        assert ok, f"chain corrupted under concurrency: {err}"


# ─── 5. End-to-end: existing audit ledger writes via Postgres backend ───────

def test_postgres_audit_backend_persists_and_verifies(tmp_db, monkeypatch):
    """The existing audit/storage.py:get_storage_backend() must produce a
    PostgresBackend that successfully appends and that AuditRepository can
    then verify."""
    monkeypatch.setenv("CURANIQ_AUDIT_BACKEND", "postgresql")
    from curaniq.audit.storage import get_storage_backend
    backend = get_storage_backend()
    assert backend.__class__.__name__ == "PostgresBackend"
    # Append two events through the legacy API
    backend.append({"query_id": str(uuid.uuid4()), "event_type": "query_received",
                    "payload": {"q": "hello"}})
    backend.append({"query_id": str(uuid.uuid4()), "event_type": "query_answered",
                    "payload": {"refused": False}})
    assert backend.count() >= 2
    # Verify the chain at the DB level
    from curaniq.db import get_session, AuditRepository
    with get_session() as s:
        ok, err = AuditRepository(s).verify_chain()
        assert ok, err


# ─── 6. Source registry hydrates from DB on opt-in ──────────────────────────

def test_source_registry_db_hydration(tmp_db, monkeypatch):
    monkeypatch.setenv("CURANIQ_SOURCE_REGISTRY_DB", "1")
    from curaniq.truth_core.source_registry import SourceRegistry
    from curaniq.db import get_session, SourceRepository
    reg = SourceRegistry()
    assert reg.is_db_backed is True
    # Defaults must have been seeded into Postgres `sources` table
    with get_session() as s:
        rows = SourceRepository(s).list_all()
        types = {r.source_type for r in rows}
        # At minimum these defaults exist in code; they must now exist in DB
        assert "openfda" in types or "dailymed" in types
        assert "nice" in types


# ─── 7. clinician_prod fail-closed when DB unreachable ──────────────────────

def test_clinician_prod_refuses_when_db_unreachable(monkeypatch):
    """Boot-time guarantee: clinician_prod requires reachable DB."""
    monkeypatch.setenv("CURANIQ_ENV", "clinician_prod")
    monkeypatch.setenv("CURANIQ_DATABASE_URL", "postgresql://no:no@127.0.0.1:1/nope")
    from curaniq.db.engine import reset_engine_for_tests, get_engine
    reset_engine_for_tests()
    with pytest.raises(RuntimeError) as excinfo:
        get_engine()
    assert "clinician_prod" in str(excinfo.value).lower()
    reset_engine_for_tests()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
