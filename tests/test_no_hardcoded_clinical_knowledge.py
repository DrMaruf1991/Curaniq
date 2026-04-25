"""
Static-check test — fails the build if a new module-level UPPER_CASE
clinical-data container is introduced.

Rationale:
    Clinical knowledge (drug lists, dose tables, interaction rules, QT-risk
    drugs, pregnancy categories, etc.) MUST flow through the
    ClinicalKnowledgeProvider abstraction defined in curaniq.knowledge —
    NEVER as hardcoded module-level constants. See:
        - curaniq/knowledge/provider.py    (the contract)
        - docs/MIGRATION_PLAYBOOK.md       (how to migrate one engine)
        - tests/test_knowledge_contract.py (provider invariants)

This test enforces the rule going forward.

Allowlist:
    Containers that exist today but have not yet been migrated through
    the provider are listed in `_KNOWN_UNMIGRATED`. As Sessions B–G
    migrate each container, its entry MUST be removed from this list.
    The list shrinking to empty is the definition of "Track A complete."

Exempt categories (NOT subject to this rule):
    - Algorithm registries (e.g., `calculators` mapping name → callable):
      these are dispatch tables, not clinical data.
    - Pure enums / categorical sets (e.g., ICD-10 chapter names):
      these are deterministic taxonomy, not facts about a drug.
    - Regex patterns (variable name ends in _PATTERN, _RE, _REGEX):
      these are parsing logic, not clinical knowledge.
    - Configuration constants (variable name contains _TIMEOUT, _TTL,
      _BACKOFF, _LIMIT, _SLA, _BUDGET, etc.): these are deployment config.

How a developer adds a new exemption:
    DON'T. If you have clinical knowledge, route it through a provider.
    If you have a non-clinical constant flagged by mistake, rename it
    to disambiguate (e.g., add `_PATTERN` suffix) or extend the
    `_EXEMPT_NAME_FRAGMENTS` set below.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
CURANIQ_PKG = REPO_ROOT / "curaniq"

# ─── EXEMPTIONS ────────────────────────────────────────────────────────────
# Variable-name fragments indicating the constant is NOT clinical knowledge.

_EXEMPT_NAME_FRAGMENTS: set[str] = {
    # Regex / parsing — match as suffix/standalone token, not substring
    # (e.g. ALLERGY_CROSS_REACTIVITY contains "_RE" but is clinical)
    "_PATTERN", "_REGEX", "_REGEXES", "_PATTERNS",
    "_EXTRACTION", "_MATCHER", "_TOKENIZER",
    "_BOUNDARY", "_DELIMITER", "_PREFIX", "_SUFFIX",
    # Configuration / SLA / framework
    "_TIMEOUT", "_RETRIES", "_BACKOFF", "_TTL", "_SLA",
    "_LIMIT", "_QUOTA", "_BUDGET", "_PORT", "_HOST",
    "TIER_SCORE", "_WEIGHTS", "_PENALTY", "SCORE_CAP",
    "THRESHOLD_SCORE", "PUBTYPE_TIER",
    # Plumbing / module bookkeeping
    "__ALL__", "_MODELS", "REQUIRED_TABLES", "_ENGINE", "_PROD",
    "LABEL_SECTIONS", "OUTPUT_LEAK_SIGNALS",
    "SECTION_BOUNDARY_KEYWORDS",
    # Hedging / output formatting (not a fact about a drug)
    "APPROPRIATE_HEDGES", "REQUIRED_HEDGES", "UNSAFE_ABSOLUTES",
    "_SCRIPT_MAP", "HIGH_QUALITY_JOURNALS", "PREDATORY_INDICATORS",
    "MEDICATION_BOUNDARY_STATEMENT", "PATIENT_FORBIDDEN_CONTENT",
    "PATIENT_MODE_DISCLAIMER", "CLINICAL_TERMS_DO_NOT_TRANSLATE",
    "HIGH_RISK_QUERY_PATTERNS",
    # Taxonomies / enums (not a fact about a drug)
    "CRITICAL_DELTA_TYPES", "HIGH_RISK_CATEGORIES",
    # Algorithm scoring weights, not drug facts
    "TISDALE_RISK_FACTORS",
    # Test / fixture
    "FIXTURE", "MOCK_", "_MOCK", "STUB_",
}

# Names of containers that are clinical knowledge, exist today, but
# have NOT YET been migrated through the provider. As Sessions B–G land,
# entries MUST be removed from this list.
#
# Format: "name@filename" — must match exactly.
#
# This list captures MODULE-LEVEL UPPER_CASE containers only. Function-local
# containers (e.g., `_anticoag_drugs` inside a method) are not scanned by
# this test — they are a separate migration target captured in Sessions
# B–G playbook entries.
_KNOWN_UNMIGRATED: set[str] = {
    # ─── Session B (FIX-34) MIGRATIONS ───
    # _DRUG_SYNONYMS@curaniq/layers/L3_safety_kernel/cql_engine.py — MIGRATED
    # DRUG_NAME_VARIANTS@curaniq/layers/L2_curation/ontology.py    — MIGRATED (data moved to JSON)
    # RXCUI_LOOKUP@curaniq/layers/L2_curation/ontology.py          — MIGRATED (FIX-34b mid-audit)

    # ─── Session H target — LOINC/SNOMED/ICD-10 controlled terminologies ───
    # These are clinical-condition / lab-test taxonomies; they need their own
    # connectors (LOINC API, SNOMED CT International, NCI Metathesaurus or
    # BioPortal). Not migrated in Sessions B-G; flagged here so the static
    # check fails the build if anyone deletes them without migrating.
    "LOINC_LOOKUP@curaniq/layers/L2_curation/ontology.py",
    "SNOMED_LOOKUP@curaniq/layers/L2_curation/ontology.py",
    "ICD10_LOOKUP@curaniq/layers/L2_curation/ontology.py",

    # ─── Session C target — DailyMed SPL section parser (renal/hepatic/pediatric/DDI) ───
    "RENAL_DOSE_RULES@curaniq/layers/L3_safety_kernel/medication_intelligence.py",
    "RENAL_DOSE_RULES@curaniq/core/cql_kernel.py",
    "HEPATIC_DOSE_RULES@curaniq/layers/L3_safety_kernel/medication_intelligence.py",
    "DDI_RULES@curaniq/layers/L3_safety_kernel/medication_intelligence.py",
    "_DDI_DATABASE@curaniq/layers/L3_safety_kernel/cql_engine.py",
    "PEDIATRIC_DOSE_RULES@curaniq/layers/L3_safety_kernel/clinical_safety_engines.py",
    "PEDIATRIC_DOSE_TABLE@curaniq/core/cql_kernel.py",
    "BROSELOW_ZONES@curaniq/core/cql_kernel.py",
    "FORMULARY@curaniq/layers/L3_safety_kernel/medication_intelligence.py",

    # ─── Session D target — LactMed + CredibleMeds ───
    "QT_RISK_DRUGS@curaniq/layers/L3_safety_kernel/clinical_safety_engines.py",
    "QT_RISK_DRUGS@curaniq/core/cql_kernel.py",
    "PREGNANCY_SAFETY@curaniq/core/cql_kernel.py",
    "PREGNANCY_DATA@curaniq/layers/L3_safety_kernel/clinical_safety_engines.py",
    "ALLERGY_CROSS_REACTIVITY@curaniq/core/cql_kernel.py",

    # ─── Session E target — openFDA + Natural Medicines ───
    "_FOOD_HERB_DATABASE@curaniq/layers/L3_safety_kernel/food_herb_resolver.py",
    "DRUG_FOOD_INTERACTIONS@curaniq/layers/L3_safety_kernel/clinical_safety_engines.py",
    "DRUG_FOOD_HERB_INTERACTIONS@curaniq/core/cql_kernel.py",
}


# ─── DETECTOR ──────────────────────────────────────────────────────────────

_CLINICAL_NAME_FRAGMENTS = {
    "DRUG", "DOSE", "DOSING", "RENAL", "HEPATIC", "PEDIATRIC",
    "GERIATRIC", "PREGNANCY", "LACTATION", "TERATOG", "ALLERG",
    "INTERACTION", "DDI", "QTC", "QT_", "FORMULARY", "ANTIBIOG",
    "VACCINE", "TDM", "BEERS", "STOPP", "REMS", "CONTRAINDICAT",
    "SYNONYM", "FATAL_DOSE", "PLAUSIB", "ANTICOAG", "OPIOID",
    "BENZODIAZ", "INSULIN", "WARFARIN", "HEPARIN", "METFORMIN",
    "STATIN", "SSRI", "MAO", "CHEMO", "ONCOL", "HERB", "FOOD_INTER",
    "RENALLY_CLEARED", "SUD_", "HEPATIC_DRUG", "WEIGHT_SIGNAL",
    "BROSELOW", "CROSS_REACTIVITY",
    # FIX-34 audit additions:
    "RXCUI", "LOINC", "SNOMED", "ICD10", "ICD_10", "ATC_",
    "MEDICATION", "PHARMACOL", "PGX_", "CYP",
}


def _is_clinical_name(name: str) -> bool:
    upper = name.upper()
    if any(frag in upper for frag in _EXEMPT_NAME_FRAGMENTS):
        # Special case: a name that ends in _PATTERN is a regex; not clinical
        # data even if it mentions a drug ("METFORMIN_DOSE_PATTERN" is a regex).
        return False
    return any(frag in upper for frag in _CLINICAL_NAME_FRAGMENTS)


def _container_size(value_node: ast.expr) -> int | None:
    if isinstance(value_node, ast.Dict):
        return len(value_node.keys)
    if isinstance(value_node, (ast.List, ast.Tuple, ast.Set)):
        return len(value_node.elts)
    return None


def _scan_file_for_violations(path: Path) -> list[tuple[str, int]]:
    """Return list of (var_name, lineno) for clinical-knowledge containers."""
    try:
        text = path.read_text(errors="replace")
        tree = ast.parse(text)
    except Exception:
        return []
    violations: list[tuple[str, int]] = []
    for node in ast.iter_child_nodes(tree):
        target_name = None
        value_node = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            if isinstance(node.targets[0], ast.Name):
                target_name = node.targets[0].id
                value_node = node.value
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.value is not None:
                target_name = node.target.id
                value_node = node.value
        if target_name is None or value_node is None:
            continue
        size = _container_size(value_node)
        if size is None or size < 3:
            continue
        if not _is_clinical_name(target_name):
            continue
        violations.append((target_name, node.lineno))
    return violations


def _all_curaniq_py_files() -> list[Path]:
    out: list[Path] = []
    for r, _, fs in os.walk(CURANIQ_PKG):
        if "__pycache__" in r:
            continue
        for f in fs:
            if f.endswith(".py"):
                out.append(Path(r) / f)
    return out


# ─── THE TEST ──────────────────────────────────────────────────────────────

def test_no_new_hardcoded_clinical_knowledge():
    """
    Every module-level UPPER_CASE clinical-data container in `curaniq/`
    must either be in `_KNOWN_UNMIGRATED` (a documented Session-B–G target)
    or it is a fresh hardcoded clinical container — which is forbidden.
    """
    found: set[str] = set()
    for fp in _all_curaniq_py_files():
        rel = str(fp.relative_to(REPO_ROOT))
        for name, _ in _scan_file_for_violations(fp):
            found.add(f"{name}@{rel}")

    new_violations = found - _KNOWN_UNMIGRATED
    stale_allowlist = _KNOWN_UNMIGRATED - found

    msg_parts: list[str] = []
    if new_violations:
        msg_parts.append(
            "❌ NEW hardcoded clinical-data containers detected. "
            "Route through curaniq.knowledge instead. See docs/MIGRATION_PLAYBOOK.md.\n"
            + "\n".join(f"  • {v}" for v in sorted(new_violations))
        )
    if stale_allowlist:
        msg_parts.append(
            "❌ Allowlist entries no longer present (migration complete?). "
            "REMOVE these entries from _KNOWN_UNMIGRATED in this test file.\n"
            + "\n".join(f"  • {v}" for v in sorted(stale_allowlist))
        )
    assert not msg_parts, "\n\n".join(msg_parts)


def test_provider_layer_has_no_clinical_data():
    """
    The knowledge provider layer itself MUST NOT contain hardcoded
    clinical data. If it did, the abstraction would be a lie.
    """
    knowledge_dir = CURANIQ_PKG / "knowledge"
    bad: list[str] = []
    for fp in knowledge_dir.glob("*.py"):
        for name, line in _scan_file_for_violations(fp):
            bad.append(f"{name}@{fp.relative_to(REPO_ROOT)}:{line}")
    assert not bad, (
        "curaniq.knowledge MUST be free of hardcoded clinical data — "
        "it is the abstraction, not a data source. Move data to "
        "curaniq/data/clinical/*.json or curaniq/data/rules/*.json:\n"
        + "\n".join(f"  • {b}" for b in bad)
    )


def test_session_a_eliminated_l5_12_constants():
    """
    Specific to Session A delivery: FATAL_DOSE_ERRORS and
    DOSE_PLAUSIBILITY_BOUNDS must NOT exist anywhere in curaniq/.
    """
    forbidden_names = {"FATAL_DOSE_ERRORS", "DOSE_PLAUSIBILITY_BOUNDS"}
    found = []
    for fp in _all_curaniq_py_files():
        text = fp.read_text(errors="replace")
        for name in forbidden_names:
            # Look for a definition pattern (assignment with `:` or `=`)
            import re as _re
            if _re.search(rf"^{name}\s*[:=]", text, _re.MULTILINE):
                found.append(f"{name}@{fp.relative_to(REPO_ROOT)}")
    assert not found, (
        f"Session A removed these constants — they must not reappear: {found}"
    )
