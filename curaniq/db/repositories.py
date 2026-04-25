"""
Repository layer — typed CRUD over the ORM.

Each repository wraps a SQLAlchemy session-bound table and exposes a
domain-shaped API. The layer modules (truth_core.source_registry,
audit.storage) consume these instead of raw ORM, so the in-memory and
DB-backed implementations are interchangeable.
"""
from __future__ import annotations
import hashlib
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional, Iterable, Sequence

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from curaniq.db.models import (
    Source, SourceVersion, SourceSyncRun,
    EvidenceObjectDB, EvidenceVersion,
    AuditEvent, AuditChainHead,
    Tenant, User,
    SourceStatusEnum, SyncOutcomeEnum, AuditEventTypeEnum,
    DEFAULT_TENANT_ID,
)


# FIX-32: process-level per-tenant lock for SQLite-mode concurrent appends.
# Postgres uses SELECT ... FOR UPDATE which provides true cross-process row
# locking. SQLite's locking is database-wide and via SQLAlchemy's connection
# pool can interleave writers in ways that violate per-tenant chain serialization.
# A per-tenant Python lock guarantees ordering within a single process, which
# is sufficient for development/test deployments. Multi-process deployments
# MUST use Postgres.
_chain_locks: dict[uuid.UUID, threading.Lock] = {}
_chain_locks_meta = threading.Lock()


def _get_chain_lock(tid: uuid.UUID) -> threading.Lock:
    """Return the per-tenant chain lock, lazily creating it."""
    lock = _chain_locks.get(tid)
    if lock is not None:
        return lock
    with _chain_locks_meta:
        lock = _chain_locks.get(tid)
        if lock is None:
            lock = threading.Lock()
            _chain_locks[tid] = lock
        return lock


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Tenants
# ─────────────────────────────────────────────────────────────────────────────

class TenantRepository:
    def __init__(self, session: Session):
        self.s = session

    def ensure_default(self) -> Tenant:
        existing = self.s.get(Tenant, DEFAULT_TENANT_ID)
        if existing:
            return existing
        t = Tenant(
            id=DEFAULT_TENANT_ID,
            name="Default Tenant",
            slug="default",
            jurisdiction="INT",
        )
        self.s.add(t)
        self.s.flush()
        return t

    def create(self, name: str, slug: str, jurisdiction: str = "INT",
               runtime_mode: str = "demo") -> Tenant:
        t = Tenant(name=name, slug=slug, jurisdiction=jurisdiction, runtime_mode=runtime_mode)
        self.s.add(t)
        self.s.flush()
        return t

    def get(self, tenant_id: uuid.UUID) -> Optional[Tenant]:
        return self.s.get(Tenant, tenant_id)

    def by_slug(self, slug: str) -> Optional[Tenant]:
        return self.s.execute(select(Tenant).where(Tenant.slug == slug)).scalar_one_or_none()

    def list(self) -> list[Tenant]:
        return list(self.s.execute(select(Tenant).order_by(Tenant.created_at)).scalars())


# ─────────────────────────────────────────────────────────────────────────────
# Sources
# ─────────────────────────────────────────────────────────────────────────────

class SourceRepository:
    def __init__(self, session: Session):
        self.s = session

    def upsert(
        self,
        source_type: str,
        display_name: str,
        authority_level: int = 5,
        jurisdictions: Iterable[str] = ("INT",),
        ttl_seconds: int = 86400,
        license_status: str = "open",
        license_expires_at: Optional[datetime] = None,
        fail_closed_high_risk: bool = True,
        allowed_claim_types: Iterable[str] = (),
        base_url: Optional[str] = None,
        notes: Optional[str] = None,
    ) -> Source:
        existing = self.by_type(source_type)
        if existing:
            existing.display_name = display_name
            existing.authority_level = authority_level
            existing.jurisdictions = ",".join(jurisdictions)
            existing.ttl_seconds = ttl_seconds
            existing.license_status = license_status
            existing.license_expires_at = license_expires_at
            existing.fail_closed_high_risk = fail_closed_high_risk
            existing.allowed_claim_types = ",".join(allowed_claim_types)
            existing.base_url = base_url
            existing.notes = notes
            self.s.flush()
            return existing
        src = Source(
            source_type=source_type,
            display_name=display_name,
            authority_level=authority_level,
            jurisdictions=",".join(jurisdictions),
            ttl_seconds=ttl_seconds,
            license_status=license_status,
            license_expires_at=license_expires_at,
            fail_closed_high_risk=fail_closed_high_risk,
            allowed_claim_types=",".join(allowed_claim_types),
            base_url=base_url,
            notes=notes,
        )
        self.s.add(src)
        self.s.flush()
        return src

    def by_type(self, source_type: str) -> Optional[Source]:
        return self.s.execute(
            select(Source).where(Source.source_type == source_type)
        ).scalar_one_or_none()

    def list_active(self, jurisdiction: Optional[str] = None) -> list[Source]:
        q = select(Source).where(Source.status == SourceStatusEnum.ACTIVE.value)
        rows = list(self.s.execute(q).scalars())
        if jurisdiction:
            rows = [r for r in rows if jurisdiction in (r.jurisdictions or "").split(",") or "INT" in (r.jurisdictions or "").split(",")]
        return rows

    def list_all(self) -> list[Source]:
        return list(self.s.execute(select(Source).order_by(Source.authority_level, Source.source_type)).scalars())

    def mark_synced(
        self,
        source_id: uuid.UUID,
        outcome: SyncOutcomeEnum,
        items_fetched: int = 0,
        items_new: int = 0,
        items_updated: int = 0,
        items_superseded: int = 0,
        error_class: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> SourceSyncRun:
        src = self.s.get(Source, source_id)
        if src is None:
            raise ValueError(f"Source not found: {source_id}")
        run = SourceSyncRun(
            source_id=source_id,
            started_at=_utcnow(),
            finished_at=_utcnow(),
            outcome=outcome.value,
            items_fetched=items_fetched,
            items_new=items_new,
            items_updated=items_updated,
            items_superseded=items_superseded,
            error_class=error_class,
            error_message=error_message,
        )
        self.s.add(run)
        src.last_attempted_sync_at = _utcnow()
        if outcome == SyncOutcomeEnum.SUCCESS:
            src.last_successful_sync_at = _utcnow()
            src.consecutive_failures = 0
        else:
            src.consecutive_failures = (src.consecutive_failures or 0) + 1
            # Auto-degrade after 3 failures
            if src.consecutive_failures >= 3 and src.status == SourceStatusEnum.ACTIVE.value:
                src.status = SourceStatusEnum.DEGRADED.value
        self.s.flush()
        return run

    def check_license_expiry(self, threshold_days: int = 30) -> list[Source]:
        """Return sources whose license expires within `threshold_days` (or already expired)."""
        cutoff = _utcnow().timestamp() + threshold_days * 86400
        out: list[Source] = []
        for src in self.list_all():
            if src.license_expires_at is None:
                continue
            if src.license_expires_at.timestamp() <= cutoff:
                out.append(src)
        return out


# ─────────────────────────────────────────────────────────────────────────────
# Evidence
# ─────────────────────────────────────────────────────────────────────────────

class EvidenceRepository:
    def __init__(self, session: Session):
        self.s = session

    def upsert_evidence(
        self,
        source_id: uuid.UUID,
        external_id: str,
        snippet: str,
        title: Optional[str] = None,
        url: Optional[str] = None,
        doi: Optional[str] = None,
        authors: Iterable[str] = (),
        tier: str = "unknown",
        grade: Optional[str] = None,
        jurisdiction: str = "INT",
        published_date: Optional[datetime] = None,
        source_last_updated_at: Optional[datetime] = None,
        source_version: Optional[str] = None,
        license_status: str = "open",
        staleness_ttl_hours: int = 24,
    ) -> tuple[EvidenceObjectDB, bool]:
        """Insert or version-bump evidence.

        Returns (evidence, was_new). On content hash change, supersedes the
        prior current version and inserts a new one with version = prev+1.
        """
        snippet_hash = _sha256(snippet)
        existing = self._current_for_external(source_id, external_id)
        if existing is None:
            ev = EvidenceObjectDB(
                source_id=source_id,
                external_id=external_id,
                title=title,
                snippet=snippet,
                snippet_hash=snippet_hash,
                url=url,
                doi=doi,
                authors=",".join(authors) if authors else None,
                tier=tier,
                grade=grade,
                jurisdiction=jurisdiction,
                published_date=published_date,
                source_last_updated_at=source_last_updated_at,
                source_version=source_version,
                license_status=license_status,
                version=1,
                is_current=True,
                staleness_ttl_hours=staleness_ttl_hours,
            )
            self.s.add(ev)
            self.s.flush()
            return ev, True

        if existing.snippet_hash == snippet_hash:
            existing.last_verified_at = _utcnow()
            existing.source_last_updated_at = source_last_updated_at or existing.source_last_updated_at
            self.s.flush()
            return existing, False

        # Content changed — supersede and version-bump
        prior = existing
        prior.is_current = False
        prior.is_superseded = True
        ev = EvidenceObjectDB(
            source_id=source_id,
            external_id=external_id,
            title=title,
            snippet=snippet,
            snippet_hash=snippet_hash,
            url=url,
            doi=doi,
            authors=",".join(authors) if authors else None,
            tier=tier,
            grade=grade,
            jurisdiction=jurisdiction,
            published_date=published_date,
            source_last_updated_at=source_last_updated_at,
            source_version=source_version,
            license_status=license_status,
            version=prior.version + 1,
            is_current=True,
            staleness_ttl_hours=staleness_ttl_hours,
        )
        self.s.add(ev)
        self.s.flush()
        prior.superseded_by_id = ev.id
        ver = EvidenceVersion(
            evidence_object_id=ev.id,
            version=ev.version,
            change_type="content_update",
            previous_snippet_hash=prior.snippet_hash,
            new_snippet_hash=ev.snippet_hash,
            diff_summary=f"snippet changed (prev {len(prior.snippet)}b → new {len(snippet)}b)",
        )
        self.s.add(ver)
        self.s.flush()
        return ev, True

    def mark_retracted(self, evidence_id: uuid.UUID, notice: str = "") -> EvidenceObjectDB:
        ev = self.s.get(EvidenceObjectDB, evidence_id)
        if ev is None:
            raise ValueError(f"Evidence not found: {evidence_id}")
        ev.is_retracted = True
        ev.retraction_notice = notice
        ev.is_current = False
        self.s.add(EvidenceVersion(
            evidence_object_id=ev.id,
            version=ev.version,
            change_type="retraction",
            previous_snippet_hash=ev.snippet_hash,
            new_snippet_hash=ev.snippet_hash,
            diff_summary=notice[:500],
        ))
        self.s.flush()
        return ev

    def _current_for_external(self, source_id: uuid.UUID, external_id: str) -> Optional[EvidenceObjectDB]:
        return self.s.execute(
            select(EvidenceObjectDB).where(
                EvidenceObjectDB.source_id == source_id,
                EvidenceObjectDB.external_id == external_id,
                EvidenceObjectDB.is_current.is_(True),
            )
        ).scalar_one_or_none()

    def get(self, evidence_id: uuid.UUID) -> Optional[EvidenceObjectDB]:
        return self.s.get(EvidenceObjectDB, evidence_id)

    def list_current(self, jurisdiction: Optional[str] = None, limit: int = 100) -> list[EvidenceObjectDB]:
        q = select(EvidenceObjectDB).where(EvidenceObjectDB.is_current.is_(True))
        if jurisdiction:
            q = q.where((EvidenceObjectDB.jurisdiction == jurisdiction) | (EvidenceObjectDB.jurisdiction == "INT"))
        q = q.limit(limit)
        return list(self.s.execute(q).scalars())

    def stale_count(self) -> int:
        """Return count of currently-marked-current rows that are past their TTL."""
        rows = list(self.s.execute(select(EvidenceObjectDB).where(EvidenceObjectDB.is_current.is_(True))).scalars())
        now = _utcnow()
        n = 0
        for r in rows:
            ttl = r.staleness_ttl_hours or 24
            age = (now - r.last_verified_at).total_seconds() / 3600
            if age > ttl:
                n += 1
        return n


# ─────────────────────────────────────────────────────────────────────────────
# Audit
# ─────────────────────────────────────────────────────────────────────────────

class AuditRepository:
    """Append-only audit ledger with per-tenant hash chain."""

    GENESIS_HASH = "0" * 64

    def __init__(self, session: Session):
        self.s = session

    def _ensure_chain_head(self, tid: uuid.UUID) -> AuditChainHead:
        """Race-safe creation of a per-tenant chain head row.

        FIX-32: Under concurrent load, multiple threads/transactions can all
        observe `head is None` and race to INSERT, causing UNIQUE constraint
        violations. We resolve this with INSERT-or-noop semantics: try to
        insert the genesis row; if a peer has already done so, roll back the
        SAVEPOINT and re-read. Either way, we return a valid head.

        Postgres: ON CONFLICT DO NOTHING via SAVEPOINT.
        SQLite:   IntegrityError caught and rolled back to SAVEPOINT.
        """
        head = self.s.get(AuditChainHead, tid)
        if head is not None:
            return head
        # Try to create. Use a nested transaction (SAVEPOINT) so a UNIQUE
        # collision rolls back ONLY the head insert, not the whole audit append.
        try:
            with self.s.begin_nested():
                head = AuditChainHead(
                    tenant_id=tid,
                    head_event_id=None,
                    head_chain_hash=self.GENESIS_HASH,
                    head_sequence=0,
                )
                self.s.add(head)
                self.s.flush()
            return head
        except IntegrityError:
            # Peer transaction inserted concurrently; re-read and use theirs.
            self.s.expire_all()
            head = self.s.get(AuditChainHead, tid)
            if head is None:
                # Vanishingly unlikely (would require the peer rolled back),
                # but recurse once rather than infinite-loop on broken DB.
                raise RuntimeError("AuditChainHead vanished after IntegrityError")
            return head

    def _lock_chain_head(self, tid: uuid.UUID) -> AuditChainHead:
        """Re-read the chain head with a row-level lock.

        FIX-32: Postgres uses SELECT ... FOR UPDATE so concurrent writers
        serialize at the row level (each waits for the prior writer's commit
        before reading head_sequence). SQLite does not support FOR UPDATE,
        but its default writer-exclusive transaction model already provides
        the same guarantee — only one writer holds the DB lock at a time.

        Combined with the UniqueConstraint on (tenant_id, sequence) in the
        AuditEvent table, this provides defense-in-depth: even if a lock
        somehow fails, duplicate sequence numbers are rejected by the DB.
        """
        # Ensure exists (race-safe)
        self._ensure_chain_head(tid)
        # Lock the row
        from curaniq.db.engine import is_postgres
        if is_postgres():
            stmt = select(AuditChainHead).where(AuditChainHead.tenant_id == tid).with_for_update()
            return self.s.execute(stmt).scalar_one()
        # SQLite: writers serialize globally; a fresh read is sufficient
        self.s.expire_all()
        return self.s.get(AuditChainHead, tid)

    def append(
        self,
        event_type: str,
        payload: dict,
        tenant_id: Optional[uuid.UUID] = None,
        user_id: Optional[uuid.UUID] = None,
        query_id: Optional[uuid.UUID] = None,
        model_id: Optional[str] = None,
        model_version: Optional[str] = None,
        pipeline_version: Optional[str] = None,
        ip_address: Optional[str] = None,
        user_agent: Optional[str] = None,
        _retry: int = 0,
    ) -> AuditEvent:
        tid = tenant_id or DEFAULT_TENANT_ID
        # FIX-32: SQLite needs Python-level serialization because its connection
        # pool + ORM interactions can interleave writes in ways that defeat
        # transaction-level serialization. Postgres uses SELECT FOR UPDATE in
        # _lock_chain_head and does not need this Python lock.
        from curaniq.db.engine import is_postgres
        if not is_postgres():
            lock = _get_chain_lock(tid)
            with lock:
                return self._append_locked(
                    event_type, payload, tid, user_id, query_id,
                    model_id, model_version, pipeline_version,
                    ip_address, user_agent, _retry,
                )
        return self._append_locked(
            event_type, payload, tid, user_id, query_id,
            model_id, model_version, pipeline_version,
            ip_address, user_agent, _retry,
        )

    def _append_locked(
        self,
        event_type: str,
        payload: dict,
        tid: uuid.UUID,
        user_id: Optional[uuid.UUID],
        query_id: Optional[uuid.UUID],
        model_id: Optional[str],
        model_version: Optional[str],
        pipeline_version: Optional[str],
        ip_address: Optional[str],
        user_agent: Optional[str],
        _retry: int,
    ) -> AuditEvent:
        """Inner append assuming serialization (either via SQLite Python lock
        or Postgres SELECT FOR UPDATE). May still hit transient IntegrityError
        in unusual session-state cases; retried up to 3 times."""
        # FIX-32: row-level lock on chain head guarantees serialized append.
        head = self._lock_chain_head(tid)
        next_sequence = head.head_sequence + 1
        # Canonical JSON (sorted keys, no spaces) for deterministic hashing
        payload_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        payload_hash = _sha256(payload_json)
        chain_input = f"{head.head_chain_hash}{payload_hash}{next_sequence}{event_type}"
        chain_hash = _sha256(chain_input)

        ev = AuditEvent(
            tenant_id=tid,
            user_id=user_id,
            query_id=query_id,
            event_type=event_type,
            sequence=next_sequence,
            payload_json=payload_json,
            payload_hash=payload_hash,
            prev_chain_hash=head.head_chain_hash,
            chain_hash=chain_hash,
            model_id=model_id,
            model_version=model_version,
            pipeline_version=pipeline_version,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        self.s.add(ev)
        try:
            self.s.flush()
        except (IntegrityError, OperationalError):
            # Defense-in-depth: retry on collision or DB-busy
            self.s.rollback()
            if _retry < 5:
                time.sleep(0.01 * (2 ** _retry))  # backoff: 10ms, 20ms, 40ms, 80ms, 160ms
                return self._append_locked(
                    event_type, payload, tid, user_id, query_id,
                    model_id, model_version, pipeline_version,
                    ip_address, user_agent, _retry + 1,
                )
            raise
        head.head_event_id = ev.id
        head.head_chain_hash = chain_hash
        head.head_sequence = next_sequence
        self.s.flush()
        return ev

    def verify_chain(self, tenant_id: Optional[uuid.UUID] = None) -> tuple[bool, Optional[str]]:
        """Recompute the chain from genesis. Returns (ok, error_message)."""
        tid = tenant_id or DEFAULT_TENANT_ID
        rows = list(self.s.execute(
            select(AuditEvent).where(AuditEvent.tenant_id == tid).order_by(AuditEvent.sequence)
        ).scalars())
        prev_hash = self.GENESIS_HASH
        for i, ev in enumerate(rows, start=1):
            if ev.sequence != i:
                return False, f"sequence gap at row {ev.id} (got {ev.sequence}, expected {i})"
            if ev.prev_chain_hash != prev_hash:
                return False, f"chain break at sequence {ev.sequence}"
            recomputed_payload = _sha256(ev.payload_json)
            if recomputed_payload != ev.payload_hash:
                return False, f"payload tampered at sequence {ev.sequence}"
            recomputed_chain = _sha256(f"{prev_hash}{ev.payload_hash}{ev.sequence}{ev.event_type}")
            if recomputed_chain != ev.chain_hash:
                return False, f"chain hash mismatch at sequence {ev.sequence}"
            prev_hash = ev.chain_hash
        return True, None

    def list_for_query(self, query_id: uuid.UUID) -> list[AuditEvent]:
        return list(self.s.execute(
            select(AuditEvent).where(AuditEvent.query_id == query_id).order_by(AuditEvent.sequence)
        ).scalars())

    def count(self, tenant_id: Optional[uuid.UUID] = None) -> int:
        tid = tenant_id or DEFAULT_TENANT_ID
        return self.s.query(AuditEvent).filter(AuditEvent.tenant_id == tid).count()

    def export(self, tenant_id: Optional[uuid.UUID] = None) -> list[dict]:
        """Return tamper-evident export of the chain."""
        tid = tenant_id or DEFAULT_TENANT_ID
        rows = list(self.s.execute(
            select(AuditEvent).where(AuditEvent.tenant_id == tid).order_by(AuditEvent.sequence)
        ).scalars())
        return [
            {
                "id": str(r.id),
                "sequence": r.sequence,
                "event_type": r.event_type,
                "tenant_id": str(r.tenant_id) if r.tenant_id else None,
                "query_id": str(r.query_id) if r.query_id else None,
                "payload_json": r.payload_json,
                "payload_hash": r.payload_hash,
                "prev_chain_hash": r.prev_chain_hash,
                "chain_hash": r.chain_hash,
                "model_id": r.model_id,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
