# CURANIQ Postgres — Quickstart

This is the canonical deployment path for CURANIQ with PostgreSQL.

## Local development with Postgres (recommended)

```bash
# 1. Bring up Postgres via docker-compose
docker compose up -d
# Postgres now running on localhost:5432
#   user:     curaniq
#   password: curaniq
#   database: curaniq

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Edit .env to set:
#   CURANIQ_ENV=demo  (or research, or clinician_prod)
#   CURANIQ_DATABASE_URL=postgresql+psycopg2://curaniq:curaniq@localhost:5432/curaniq
#   CURANIQ_AUDIT_BACKEND=postgresql
#   CURANIQ_SOURCE_REGISTRY_DB=1
#   CURANIQ_EVIDENCE_DB=1

# 4. Apply database migrations
alembic upgrade head
# Creates: tenants, users, sources, source_versions, source_sync_runs,
#          evidence_objects, evidence_versions, audit_events, audit_chain_heads

# 5. Run tests against the real Postgres
pytest tests/ -v
# Expected: 61 passed

# 6. Start the API server
python run.py
# API on http://localhost:8000
# OpenAPI docs at http://localhost:8000/docs
```

## Production deployment (clinician_prod)

`clinician_prod` enforces the following at boot. **All must be satisfied or the
pipeline refuses to start:**

| Required | What it means |
|---|---|
| `CURANIQ_ENV=clinician_prod` | Activates fail-closed enforcement |
| `CURANIQ_DATABASE_URL=postgresql+...` | **SQLite is forbidden in production.** |
| Postgres reachable from app | Boot-time `SELECT 1` check |
| Migrations applied | All 9 production tables must exist |
| `CURANIQ_AUDIT_BACKEND=postgresql` | JSONL and memory backends are forbidden |
| `CURANIQ_SOURCE_REGISTRY_DB=1` | Static-only registry is forbidden |
| `CURANIQ_EVIDENCE_DB=1` | Live evidence must persist through `EvidenceRepository` |
| `CURANIQ_API_KEY` | Replaces self-declared role from request body |

If any check fails, `curaniq.db.production.enforce_production_boot()` raises
`ProductionReadinessError` before any clinical answer can be generated.

### Recommended production stack

- Managed Postgres (RDS, Cloud SQL, Yandex Cloud, etc.) — NOT the local
  docker-compose
- Connection pooling: tune `CURANIQ_DB_POOL_SIZE`, `CURANIQ_DB_POOL_OVERFLOW`
- Run `alembic upgrade head` as part of CI/CD before app start
- Schedule `python -m curaniq.workers.source_sync_worker` via cron / Celery beat
  / Kubernetes CronJob
- Set `CURANIQ_REVIEW_SECRET` to a strong secret (used for cryptographic
  signing of clinician review records)

## Verifying production readiness

```python
from curaniq.db.production import run_production_readiness_checks

report = run_production_readiness_checks()
print(report.passed)        # True/False
print(report.checks)        # ['postgres_url', 'tables', 'sources', ...]
print(report.failures)      # [] if all checks passed
```

## Postgres tables created

| Table | Purpose |
|---|---|
| `tenants` | Multi-tenant isolation (hospitals/institutions) |
| `users` | Clinicians/admins with license tracking, MFA hooks |
| `sources` | Approved evidence sources (NCBI, NICE, WHO, ...) |
| `source_versions` | Per-source license/schema version history |
| `source_sync_runs` | Each scheduled sync attempt — success/failure |
| `evidence_objects` | Curated evidence with content hashing, retraction tracking |
| `evidence_versions` | Append-only version history per evidence object |
| `audit_events` | Append-only audit log with hash chain (tamper-evident) |
| `audit_chain_heads` | Per-tenant chain head pointer |

## Backup & integrity

The audit ledger is **cryptographically chained**. Use:

```python
from curaniq.db import get_session, AuditRepository
with get_session() as s:
    ok, error = AuditRepository(s).verify_chain()
    print(f"Chain intact: {ok}")
```

Any unauthorized modification of any `audit_events` row breaks the chain.
Run this verification on a schedule and alert on `ok == False`.

## Concurrency safety

The audit ledger is verified for concurrent writers:
- 8 threads × 5 events = 40 events written, chain intact
- 16 threads × 10 events = 160 events in 2.0s, no errors
- Per-tenant Python lock for SQLite, `SELECT FOR UPDATE` for Postgres

See `tests/test_db.py::test_audit_concurrent_writes_preserve_chain`.

## What this Postgres setup does NOT include

- Real licensed connectors for DailyMed, ClinicalTrials.gov, NICE, WHO, EMA,
  Cochrane, NCCN, ASCO — these need API credentials and (for several) license
  agreements
- Production-grade medical NLI model (currently uses heuristic claim checking
  in some paths)
- JWT/OIDC auth — replace `X-CURANIQ-API-KEY` before hospital deployment
- Hospital-grade EHR/FHIR integration — the FHIR scaffold exists but is not a
  validated SMART-on-FHIR production integration

These remain in the production roadmap. The Postgres backbone is the load-
bearing foundation they will build on.
