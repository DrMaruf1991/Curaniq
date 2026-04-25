# CURANIQ Postgres Enforcement Phase

This patch turns the Postgres backbone into an enforced production safety layer.

## Added / hardened

1. `curaniq/db/production.py`
   - `enforce_production_boot()`
   - `run_production_readiness_checks()`
   - fail-closed checks for Postgres URL, migrations/tables, source registry, evidence DB, and audit backend.

2. `curaniq/db/__init__.py`
   - lazy DB exports so demo imports do not fail before DB dependencies are installed.
   - actual DB usage still requires `sqlalchemy`, `alembic`, and `psycopg2-binary`.

3. `curaniq/audit/storage.py`
   - clinician production no longer falls back from PostgreSQL audit to JSONL.
   - JSONL audit backend is forbidden in `CURANIQ_ENV=clinician_prod`.

4. `curaniq/truth_core/source_registry.py`
   - clinician production requires DB-backed source registry.
   - DB source status/license/TTL/allowed claim types are hydrated back into runtime policy.
   - disabled/degraded/expired sources are not approved.

5. `curaniq/core/pipeline.py`
   - calls production readiness enforcement during clinician production boot.

6. `curaniq/core/pipeline_components.py`
   - live evidence is persisted through `EvidenceRepository` when `CURANIQ_EVIDENCE_DB=1` or `clinician_prod`.
   - if evidence persistence fails in clinician production, retrieval fails closed.

7. `curaniq/services/source_sync_service.py` and `curaniq/workers/source_sync_worker.py`
   - DB-backed source sync coordinator and CLI worker skeleton.
   - records success/failure into `source_sync_runs`.
   - connector absence is recorded as failure, not falsely treated as current evidence.

8. `tests/test_postgres_enforcement_static.py`
   - tests for SQLite forbidden in clinician production, audit fail-closed behavior, DB source registry enforcement, source sync failure logging, and evidence versioning.

## Production rules now enforced

For `CURANIQ_ENV=clinician_prod`:

- PostgreSQL is required; SQLite is forbidden.
- PostgreSQL audit backend is required; JSONL fallback is forbidden.
- DB-backed source registry is required.
- Core DB tables must exist before clinical mode boots.
- Live evidence must persist to the DB; otherwise the evidence pack fails closed.
- Seed evidence and mock LLM remain blocked by Truth Core policy.

## Still not magically solved

This patch does not create licensed medical data where licenses/connectors are not configured. You still need real production connectors/credentials for NCCN/ASCO/ESMO/NICE/WHO/DailyMed/OpenFDA/ClinicalTrials.gov/local formularies, plus medical NLI/contradiction models and clinician validation before real medical deployment.
