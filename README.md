# CURANIQ — Medical Evidence Operating System

**State:** Integration-green. **51/51 tests passing.** Pipeline processes clinical queries end-to-end through the FastAPI surface. Postgres backbone (FIX-31, FIX-32) provides production database for source registry, evidence versioning, and tamper-evident audit ledger with verified concurrency safety.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env       # edit values for your environment
```

### Run tests (verifies the engine)

```bash
pytest tests/ -v
```

Expected: **51 passed**.

### Run with Postgres (production-like local)

```bash
docker compose up -d                       # start Postgres on :5432
export CURANIQ_DATABASE_URL=postgresql+psycopg2://curaniq:curaniq@localhost:5432/curaniq
export CURANIQ_AUDIT_BACKEND=postgresql
export CURANIQ_SOURCE_REGISTRY_DB=1
alembic upgrade head                       # apply migrations
pytest tests/test_db.py -v                 # 14 DB tests against real Postgres
python run.py                              # API on :8000
```

### Run without Postgres (default — uses SQLite for tests)

```bash
pytest tests/ -v                           # 50 tests against temp-file SQLite
python run.py                              # API on :8000, JSONL audit
```

### Test a query against the running API

```bash
curl -X POST http://localhost:8000/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "56yo on metformin with eGFR 35 — what is the safe dose?",
    "user_role": "clinician",
    "jurisdiction": "UZ",
    "mode": "quick_answer",
    "patient_context": {
      "age_years": 56,
      "renal": {"egfr_ml_min": 35},
      "active_medications": ["metformin"]
    }
  }'
```

## Runtime modes

Set `CURANIQ_ENV` in `.env`:

| Mode | Behavior |
|---|---|
| `demo` | Allows seed evidence and mock LLM. For UI development. |
| `research` | Labeled non-production behavior. |
| `clinician_prod` | **Fail-closed.** No seed evidence. No mock LLM. No stale high-risk evidence. Refuses without governed sources. **Requires reachable database** (FIX-31). |

## Test suite layout

| File | Tests | Purpose |
|---|---|---|
| `tests/test_truth_core_static.py` | 5 | Safety contracts: claim-type evidence requirements, fail-closed semantics |
| `tests/test_smoke.py` | 5 | End-to-end smoke: imports, instantiation, query processing, emergency triage |
| `tests/test_api_e2e.py` | 9 | Live FastAPI surface: health, CQL endpoints, /query, validation |
| `tests/test_coverage.py` | 17 | Broader scenarios: 4 interaction modes, 5 jurisdictions, pediatric/pregnancy/dialysis/allergy/polypharmacy, multilingual, concurrent threads |
| `tests/test_db.py` | 15 | Postgres backbone: tenants, sources, evidence versioning, hash-chain audit, **tamper detection**, license expiry, fail-closed, **concurrent-write integrity** |
| **Total** | **51** | |

## Directory layout

```
curaniq/
├── api/              # FastAPI surface
├── core/             # Pipeline orchestrator + CQL kernel
├── audit/            # L9 immutable evidence ledger (JSONL + Postgres backends)
├── safety/           # Safety gate suite + triage gate
├── layers/           # 15-layer architecture (L0–L14)
├── models/           # Pydantic schemas
├── data/             # 31 clinical JSON data files
├── truth_core/       # Fail-closed safety contracts (claim requirements, source registry, freshness)
├── db/               # Postgres backbone (FIX-31): engine, ORM models, repositories
└── data_loader.py
alembic/              # Database migrations (alembic upgrade head)
tests/                # 50 tests, all passing
docker-compose.yml    # Local Postgres for prod-like testing
scripts/              # Static check helpers
```

## Architecture reference

See `CURANIQ_Architecture_v3_6_FINAL.docx` (15 layers, 181 modules, 8-layer anti-hallucination defense).

See `TRUTH_CORE_HARDENING.md` for the fail-closed safety hardening summary.

See `INTEGRATION_FIX_LOG.md` for the integration reconciliation that brought the engine to green.

## Honest scope

- The engine **boots, processes queries, fires safety gates, refuses correctly when conditions aren't met, and exposes a working FastAPI surface.** All verified by tests.
- It does **not** replace clinical judgment. It does **not** mean the clinical content is validated for production hospital use. Before real patient deployment, see remaining work in `TRUTH_CORE_HARDENING.md` ("Important remaining production work").
- Live evidence retrieval (PubMed, OpenFDA) requires API keys and outbound HTTPS. In test environments without these, the engine falls back per the active runtime mode.
