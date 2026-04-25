"""Production readiness and fail-closed enforcement for CURANIQ.

This module is deliberately small and dependency-light. It is called during
clinician production boot to prove that the database, migrations, source
registry, evidence tables, and audit backend are available before any clinical
answer path can run.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List


from curaniq.truth_core.config import is_clinician_prod


class ProductionReadinessError(RuntimeError):
    """Raised when clinician production mode cannot be started safely."""


@dataclass(frozen=True)
class ReadinessReport:
    passed: bool
    checks: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def require_passed(self) -> "ReadinessReport":
        if not self.passed:
            raise ProductionReadinessError("; ".join(self.failures))
        return self


REQUIRED_TABLES = {
    "tenants",
    "users",
    "sources",
    "source_versions",
    "source_sync_runs",
    "evidence_objects",
    "evidence_versions",
    "audit_events",
    "audit_chain_heads",
}


def assert_database_ready(require_postgres: bool | None = None) -> None:
    from curaniq.db.engine import get_engine, is_postgres
    from sqlalchemy import text
    """Verify DB connectivity and, in production, forbid SQLite."""
    if require_postgres is None:
        require_postgres = is_clinician_prod()
    if require_postgres and not is_postgres():
        raise ProductionReadinessError(
            "clinician_prod requires PostgreSQL; set CURANIQ_DATABASE_URL to a postgresql:// URL."
        )
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))


def assert_migrations_current() -> None:
    from curaniq.db.engine import get_engine
    from sqlalchemy import inspect
    """Verify that all core tables exist.

    This is intentionally schema-level and does not run Alembic itself. In
    production, migrations must be run before the service boots.
    """
    engine = get_engine()
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    missing = sorted(REQUIRED_TABLES - existing)
    if missing:
        raise ProductionReadinessError(
            "Database schema is not ready; missing tables: " + ", ".join(missing)
        )


def assert_source_registry_ready() -> None:
    """Verify at least one active governed source exists in DB."""
    from curaniq.db import get_session
    from curaniq.db.models import Source
    from curaniq.db.repositories import SourceRepository

    with get_session() as s:
        active = SourceRepository(s).list_active()
        if not active:
            raise ProductionReadinessError(
                "No active governed evidence sources are registered in the production database."
            )
        blocked = [src.source_type for src in active if (src.license_status or "").lower() in {"expired", "license_expired"}]
        if blocked:
            raise ProductionReadinessError(
                "Active sources have expired licenses: " + ", ".join(blocked)
            )


def assert_evidence_store_ready() -> None:
    """Verify evidence tables can be queried. Evidence may be empty at boot, but table must exist."""
    from curaniq.db import get_session
    from curaniq.db.repositories import EvidenceRepository

    with get_session() as s:
        EvidenceRepository(s).list_current(limit=1)


def assert_audit_backend_ready() -> None:
    """Verify audit backend is production-safe and appendable."""
    from curaniq.audit.storage import get_storage_backend, PostgresBackend

    backend = get_storage_backend()
    if is_clinician_prod() and not isinstance(backend, PostgresBackend):
        raise ProductionReadinessError(
            "clinician_prod requires CURANIQ_AUDIT_BACKEND=postgresql and a working PostgresBackend."
        )
    backend.count()


def run_production_readiness_checks(require_postgres: bool | None = None) -> ReadinessReport:
    checks: List[str] = []
    failures: List[str] = []
    for name, fn in [
        ("database_ready", lambda: assert_database_ready(require_postgres=require_postgres)),
        ("migrations_current", assert_migrations_current),
        ("source_registry_ready", assert_source_registry_ready),
        ("evidence_store_ready", assert_evidence_store_ready),
        ("audit_backend_ready", assert_audit_backend_ready),
    ]:
        try:
            fn()
            checks.append(name)
        except Exception as exc:  # collect all failures for actionable startup logs
            failures.append(f"{name}: {exc}")
    return ReadinessReport(passed=not failures, checks=checks, failures=failures)


def enforce_production_boot() -> ReadinessReport:
    """Fail closed during clinician production startup; no-op report otherwise."""
    if not is_clinician_prod():
        return ReadinessReport(passed=True, checks=["non_production_mode"], failures=[])
    return run_production_readiness_checks(require_postgres=True).require_passed()
