"""Static safety contract tests for clinician production enforcement.

These tests intentionally inspect the production-critical code paths without
requiring a live Postgres server. They prevent regressions where the application
silently falls back to demo storage or hides source-sync observability failures.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_clinician_prod_forbids_jsonl_and_memory_audit():
    src = read("curaniq/audit/storage.py")
    assert "clinician_prod forbids JSONL audit storage" in src
    assert "clinician_prod forbids in-memory audit storage" in src
    assert "CURANIQ_AUDIT_BACKEND=postgresql" in src


def test_clinician_prod_source_sync_failure_must_be_persisted_or_raise():
    src = read("curaniq/services/source_sync_service.py")
    assert "No connector configured for source" in src
    assert "clinician_prod could not persist source sync failure" in src
    assert "is_clinician_prod()" in src


def test_clinician_prod_boot_checks_are_called_by_pipeline():
    src = read("curaniq/core/pipeline.py")
    assert "enforce_production_boot" in src
    assert "self.production_readiness_report = enforce_production_boot()" in src


def test_clinician_prod_retrieval_requires_evidence_db_persistence():
    src = read("curaniq/core/pipeline_components.py")
    assert "fail_closed_evidence_db_persistence_failed" in src
    assert "fail_closed_no_live_evidence_seed_disabled" in src
    assert "self._use_evidence_db" in src


def test_clinician_prod_source_registry_is_db_backed_and_active_only():
    src = read("curaniq/truth_core/source_registry.py")
    assert "clinician_prod source registry has no active approved DB sources" in src
    assert "status != SourceStatusEnum.ACTIVE.value" in src
    assert "license_expired" in src
