# CURANIQ Clinical Knowledge — Migration Playbook

This playbook is the standard procedure for migrating one clinical
engine off hardcoded constants onto `curaniq.knowledge`. Session A
(L5-12 Dose Plausibility) was the template. Sessions B–G repeat
this pattern for each remaining engine.

## The contract

Every clinical fact (drug list, dose rule, interaction, QT-risk
flag, pregnancy category, etc.) MUST flow through the
`ClinicalKnowledgeProvider` defined in `curaniq/knowledge/provider.py`.

- Engines consume facts only via the protocol — never via module-level
  constants.
- `LiveEvidenceProvider` is the only provider that may serve in
  `clinician_prod`. It calls L1 connectors at retrieval time. If the
  connector for a given fact is not wired, it MUST raise
  `KnowledgeUnavailableError` rather than fall back silently.
- `VendoredSnapshotProvider` serves vendored JSON snapshots (with
  full provenance metadata) only in `demo` and `research`. It refuses
  to instantiate in `clinician_prod` (boot-time tripwire).
- `RouterProvider` composes the above with environment-aware policy.

The static-check test
`tests/test_no_hardcoded_clinical_knowledge.py` enforces this rule
going forward. New module-level UPPER_CASE clinical containers fail
the build unless they appear on the explicit `_KNOWN_UNMIGRATED`
allowlist (which shrinks each session).

## The migration recipe (six steps, in order)

### Step 1 — Identify the container and its consumer

Pick one container from `_KNOWN_UNMIGRATED`. Locate:
- The container's source file
- The function/class that reads the container
- All call sites of that function/class
- All tests that exercise the function/class

### Step 2 — Extend the protocol if needed

Add the new method to `curaniq/knowledge/provider.py`:

```python
def get_qt_risk(self, drug: str) -> QTRiskLevel | None: ...
```

Add the typed record to `curaniq/knowledge/types.py`:

```python
@dataclass(frozen=True)
class QTRiskLevel:
    drug: str
    risk_category: str   # "known", "possible", "conditional"
    sources: list[str]
    provenance: Provenance
```

### Step 3 — Build the vendored snapshot

Create `curaniq/data/clinical/<topic>.json` with the full `_metadata`
block:

```json
{
  "_metadata": {
    "title": "...",
    "purpose": "...",
    "scope": "DEMO and RESEARCH only — clinician_prod must use live",
    "snapshot_date_iso": "<ISO-8601>",
    "snapshot_version": "vendored-YYYY.MM.DD.N",
    "source": "<governed source name>",
    "source_url": "<canonical URL>",
    "license_status": "public_domain|open|restricted|licensed",
    "extraction_method": "manual_curation|automated_extraction|live_api|cached",
    "is_authoritative": false,
    "refresh_command": "..."
  },
  "<topic>": [ ... ]
}
```

Implement `_load_<topic>` in `curaniq/knowledge/vendored.py` and add
a `get_<topic>` method to `VendoredSnapshotProvider`.

### Step 4 — Build the live connector

In `curaniq/knowledge/live.py`, define a Protocol for the connector:

```python
class _QTRiskConnector(Protocol):
    def fetch_qt_risk(self, drug: str) -> QTRiskLevel | None: ...
```

Inject it via `LiveEvidenceProvider.__init__`. The first session
that builds this Protocol's implementation wires it. Until then,
`get_qt_risk()` raises `KnowledgeUnavailableError` — this is the
correct behavior; it forces the env-aware router to either fall back
to vendored (demo) or refuse (prod).

### Step 5 — Migrate the consumer

Inject the provider into the consuming engine via constructor:

```python
class QTRiskCalculator:
    def __init__(self, knowledge_provider: ClinicalKnowledgeProvider | None = None):
        self._kp = knowledge_provider or RouterProvider()

    def assess(self, drugs: list[str]) -> ...:
        for drug in drugs:
            risk = self._kp.get_qt_risk(drug)   # was: QT_RISK_DRUGS.get(drug)
            ...
```

Delete the old module-level constant. Run all tests — they MUST stay
green.

### Step 6 — Update enforcement

1. Remove the migrated entry from `_KNOWN_UNMIGRATED` in
   `tests/test_no_hardcoded_clinical_knowledge.py`.
2. Add a regression test in `tests/test_knowledge_contract.py` for
   the new method (mirror the L5-12 pattern: provenance check,
   vendored loads, prod refuses, demo falls back).
3. Run the full suite. Must stay green.
4. Run a small attack suite specific to the migrated engine.

## Session map

| Session | Target engine | Primary source | Containers eliminated |
|---|---|---|---|
| **A (DONE)** | L5-12 Dose Plausibility | ISMP Sentinel List | `FATAL_DOSE_ERRORS`, `DOSE_PLAUSIBILITY_BOUNDS`, `RENALLY_CLEARED_DRUGS`, `TERATOGENIC_DRUGS`, `WEIGHT_REQUIRED_DRUGS` (all via dead-code deletion + provider migration) |
| B | L2-1 Ontology / Drug Synonyms | RxNorm REST + ATC/RxClass | `DRUG_NAME_VARIANTS`, `_DRUG_SYNONYMS`, function-local `_anticoag_drugs`, `_sud_drugs`, `_tdm_drugs` |
| C | L3-2/L3-7 Renal/Hepatic/Pediatric Dosing | DailyMed SPL XML section parser | `RENAL_DOSE_RULES`×2, `HEPATIC_DOSE_RULES`, `PEDIATRIC_DOSE_RULES`, `PEDIATRIC_DOSE_TABLE`, `BROSELOW_ZONES`, `FORMULARY` |
| D | L3-9/L3-12 Pregnancy + QT Risk | LactMed + CredibleMeds | `QT_RISK_DRUGS`×2, `PREGNANCY_SAFETY`, `PREGNANCY_DATA`, `ALLERGY_CROSS_REACTIVITY` |
| E | L3-2/L3-17 DDI + Food/Herb | openFDA Drug Interactions + Natural Medicines | `DDI_RULES`, `_DDI_DATABASE`, `_FOOD_HERB_DATABASE`, `DRUG_FOOD_INTERACTIONS`, `DRUG_FOOD_HERB_INTERACTIONS` |
| F | L11-1 Local Availability + L14-8 Session Memory | UZ MOH + ATC class | `uz_drugs`, `hepatic_drugs`, `weight_signals` |
| G | L4-14 hash-lock wiring + dead-code purge | (refactor) | Remove `ExtendedClaimContractEngine` (the broken `validate_chunk_id` path), wire L4-14 into the live `core/claim_contract.py` path |

## Acceptance criteria per session

- All existing tests stay green.
- New contract tests added for the new method(s).
- Migrated entries removed from `_KNOWN_UNMIGRATED`.
- Engine-specific attack suite passes (catches the architecture-named
  failure modes for that engine).
- Live provider raises `KnowledgeUnavailableError` cleanly when the
  connector is unwired.
- Vendored provider serves correctly with full provenance.
- Router behaves correctly in both demo and prod.

## End state

When all sessions land, `_KNOWN_UNMIGRATED` is empty. Every clinical
fact in CURANIQ flows through `ClinicalKnowledgeProvider`. The static
check guarantees no regression.

`clinician_prod` then has a single hard requirement: every L1
connector for the facts it consumes must be wired and reachable.
If any one connector is down, the engines that depend on it refuse
the relevant clinical query — never silently degrade. That is the
architecture's actual promise made into a build-time invariant.
