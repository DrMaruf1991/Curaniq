# CURANIQ Truth Core Hardening Patch

This patch converts CURANIQ from a demo-friendly evidence-aware prototype toward a fail-closed medical evidence engine.

## What was added

1. `curaniq/truth_core/config.py`
   - Runtime mode: `demo`, `research`, `clinician_prod`.
   - `clinician_prod` disables seed evidence, mock LLM generation, and stale high-risk evidence.

2. `curaniq/truth_core/source_registry.py`
   - Approved source registry with source type, authority level, jurisdiction, TTL, fail-closed policy, and allowed claim types.

3. `curaniq/truth_core/claim_requirements.py`
   - Claim-type-specific evidence requirements.
   - Dosing, contraindication, drug interaction, and safety warning claims fail closed unless the correct source class exists.
   - PubMed alone is intentionally insufficient for high-risk dosing claims.

4. `curaniq/truth_core/freshness.py`
   - Pre-generation freshness and source-class validation.
   - Blocks retracted, stale, superseded, withdrawn, unapproved, or wrong-source-class evidence.

5. Production fail-closed behavior
   - `HybridRetriever` now refuses in `clinician_prod` when live/current governed evidence is unavailable.
   - Seed evidence is allowed only when policy permits.
   - Mock generation is blocked in `clinician_prod`.

6. Schema unification
   - Added missing enum values used by layers: `EvidenceTier.NEGATIVE_TRIAL`, `EvidenceTier.UNKNOWN`, `ClaimType.SAFETY_WARNING`, `ClaimType.UNKNOWN`, `Jurisdiction.INTL`, `Jurisdiction.WHO`, `Jurisdiction.CIS`.
   - Added source metadata fields separating source date from retrieval date: `source_last_updated_at`, `source_version`, `retrieved_at`, `content_hash`, `license_status`, `superseded_by`, `source_trust_score`, `applicability_score`, `patient_subgroup_match`, `guideline_status`.

7. FDA label date fix
   - OpenFDA retrieval no longer sets `published_date = now`.
   - It separates source update date from retrieval/verification date.

8. API production role guard
   - In `clinician_prod`, clinician role can no longer be self-declared from JSON body alone.
   - Minimal `X-CURANIQ-API-KEY` + `X-CURANIQ-ROLE` guard added; replace with JWT/OIDC before hospital deployment.

9. Runtime compatibility fixes
   - Added backward-compatible class aliases/wrappers required by the main pipeline.
   - Fixed missing defaults for tenant-bound components used during singleton startup.
   - Fixed pipeline early `cql_results` initialization.
   - Fixed coverage scope internal alias bug.
   - Fixed CQL compatibility shims for QT and medication intelligence engines.
   - Fixed audit ledger storage reference to nonexistent `entry.evidence_count`.

10. Tests
   - `tests/test_truth_core_static.py` verifies core safety contracts without external API calls.
   - `scripts/local_truth_core_static_check.py` performs no-dependency static checks.

## Verified locally in this environment

- Imported `curaniq.core.pipeline` successfully.
- Imported `curaniq.api.main` successfully.
- Instantiated `CURANIQPipeline` in `clinician_prod` with no seed evidence loaded.
- Simulated a clinician production query while internet/API retrieval was unavailable; response failed closed with refusal and zero evidence cards.
- Ran pytest safety tests: 5 passed.

## Important remaining production work

This patch does not magically make the system clinically validated. Before real doctor deployment, you still need:

- real licensed guideline/drug-interaction/dosing sources;
- source version tracker backed by database;
- medical NLI/entailment verifier replacing keyword overlap;
- full JWT/OIDC auth and tenant isolation;
- formal validation dataset and regression suite;
- human clinician review workflow;
- legal/regulatory classification by jurisdiction;
- monitoring and incident-response process.

The key safety improvement is that `clinician_prod` now fails closed instead of silently using demo evidence or mock generation.
