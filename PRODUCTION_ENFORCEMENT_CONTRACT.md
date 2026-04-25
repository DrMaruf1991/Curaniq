# CURANIQ Production Enforcement Contract v4

This package contains the explicit fail-closed production contract requested for the medical evidence engine.

## Enforced in `clinician_prod`

1. **No SQLite in clinician production**
   - `curaniq/db/production.py` requires PostgreSQL when `CURANIQ_ENV=clinician_prod`.
   - Production boot fails before the clinical answer path if the DB is not reachable or required tables are missing.

2. **No JSONL or memory audit fallback in clinician production**
   - `curaniq/audit/storage.py` now forbids `jsonl` and `memory` audit backends in clinician production.
   - `CURANIQ_AUDIT_BACKEND=postgresql` is required.

3. **No silent source registry DB failure in clinician production**
   - `curaniq/truth_core/source_registry.py` raises if DB-backed source governance cannot load.
   - Static source policies remain allowed only for demo/research.

4. **No static-only source registry in clinician production**
   - Production uses DB-backed source policies, including status, TTL, claim-type permissions, and license status.
   - Disabled/degraded/expired sources are excluded.

5. **No live evidence bypassing the evidence DB in clinician production**
   - `curaniq/core/pipeline_components.py` persists live evidence via `EvidenceRepository` when in clinician production.
   - If persistence fails, retrieval returns an empty fail-closed evidence pack.

6. **No source sync pretending success when connector is absent**
   - `curaniq/services/source_sync_service.py` records missing connectors as failed sync runs.
   - If failure cannot be persisted in clinician production, it raises instead of silently passing.

## Still intentionally not faked

The package does not invent licensed guideline data, NCCN/ASCO/ESMO/Cochrane feeds, or a validated medical-NLI model. Those need real credentials, source agreements, and validation datasets. The system is designed to fail closed until those assets are connected.
