"""CURANIQ Database Layer.

Lazy exports keep non-DB/demo imports working even before SQLAlchemy is
installed. Any actual DB use still requires the dependencies in requirements.txt.
"""
from __future__ import annotations

__all__ = [
    "Base", "get_engine", "get_session", "init_db", "db_url", "is_postgres",
    "Source", "SourceVersion", "SourceSyncRun", "EvidenceObjectDB", "EvidenceVersion",
    "AuditEvent", "AuditChainHead", "Tenant", "User",
    "SourceRepository", "EvidenceRepository", "AuditRepository", "TenantRepository",
    "ProductionReadinessError", "ReadinessReport", "assert_database_ready",
    "assert_migrations_current", "assert_source_registry_ready", "assert_evidence_store_ready",
    "assert_audit_backend_ready", "run_production_readiness_checks", "enforce_production_boot",
]

_ENGINE = {"get_engine", "get_session", "init_db", "db_url", "is_postgres"}
_MODELS = {"Base", "Source", "SourceVersion", "SourceSyncRun", "EvidenceObjectDB", "EvidenceVersion", "AuditEvent", "AuditChainHead", "Tenant", "User"}
_REPOS = {"SourceRepository", "EvidenceRepository", "AuditRepository", "TenantRepository"}
_PROD = {"ProductionReadinessError", "ReadinessReport", "assert_database_ready", "assert_migrations_current", "assert_source_registry_ready", "assert_evidence_store_ready", "assert_audit_backend_ready", "run_production_readiness_checks", "enforce_production_boot"}


def __getattr__(name: str):
    if name in _ENGINE:
        from curaniq.db import engine as m
        return getattr(m, name)
    if name in _MODELS:
        from curaniq.db import models as m
        return getattr(m, name)
    if name in _REPOS:
        from curaniq.db import repositories as m
        return getattr(m, name)
    if name in _PROD:
        from curaniq.db import production as m
        return getattr(m, name)
    raise AttributeError(name)
