import os
import uuid
from datetime import datetime, timezone, timedelta

import pytest


def test_clinician_prod_forbids_sqlite(monkeypatch):
    from curaniq.db.engine import reset_engine_for_tests, get_engine
    monkeypatch.setenv("CURANIQ_ENV", "clinician_prod")
    monkeypatch.setenv("CURANIQ_DATABASE_URL", "sqlite:///:memory:")
    reset_engine_for_tests()
    with pytest.raises(RuntimeError, match="requires PostgreSQL"):
        get_engine()
    reset_engine_for_tests()


def test_audit_postgres_fails_closed_in_prod(monkeypatch):
    from curaniq.audit.storage import get_storage_backend
    monkeypatch.setenv("CURANIQ_ENV", "clinician_prod")
    monkeypatch.setenv("CURANIQ_AUDIT_BACKEND", "jsonl")
    # FIX-33: actual error message is "clinician_prod forbids JSONL audit storage; set
    # CURANIQ_AUDIT_BACKEND=postgresql." Match on the trailing instruction so this test
    # is robust to either the JSONL or memory fail-closed wording.
    with pytest.raises(RuntimeError, match="CURANIQ_AUDIT_BACKEND=postgresql"):
        get_storage_backend()


def test_source_registry_db_failure_raises_in_prod(monkeypatch):
    from curaniq.truth_core.source_registry import SourceRegistry
    monkeypatch.setenv("CURANIQ_ENV", "clinician_prod")
    monkeypatch.setenv("CURANIQ_DATABASE_URL", "sqlite:///:memory:")
    with pytest.raises(Exception):
        SourceRegistry(use_db=True)


def test_source_sync_service_records_missing_connector_failure(monkeypatch, tmp_path):
    from curaniq.db.engine import reset_engine_for_tests
    from curaniq.db import init_db, get_session, SourceRepository
    from curaniq.services.source_sync_service import SourceSyncService
    monkeypatch.setenv("CURANIQ_ENV", "demo")
    monkeypatch.setenv("CURANIQ_DATABASE_URL", f"sqlite:///{tmp_path/'sync.db'}")
    reset_engine_for_tests()
    init_db(drop_existing=True)
    with get_session() as s:
        SourceRepository(s).upsert(source_type="pubmed", display_name="PubMed")
    result = SourceSyncService(connectors={}).sync_one("pubmed")
    assert result.outcome == "failed"
    assert "No connector" in result.error


def test_evidence_repository_versions_content_change(monkeypatch, tmp_path):
    from curaniq.db.engine import reset_engine_for_tests
    from curaniq.db import init_db, get_session, SourceRepository, EvidenceRepository
    monkeypatch.setenv("CURANIQ_ENV", "demo")
    monkeypatch.setenv("CURANIQ_DATABASE_URL", f"sqlite:///{tmp_path/'evidence.db'}")
    reset_engine_for_tests()
    init_db(drop_existing=True)
    with get_session() as s:
        src = SourceRepository(s).upsert(source_type="pubmed", display_name="PubMed")
        repo = EvidenceRepository(s)
        ev1, created1 = repo.upsert_evidence(src.id, "PMID1", "old text")
        ev2, created2 = repo.upsert_evidence(src.id, "PMID1", "new text")
        assert created1 is True
        assert created2 is True
        assert ev2.version == ev1.version + 1
        assert ev1.is_superseded is True
