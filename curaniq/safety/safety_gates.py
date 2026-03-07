"""
CURANIQ — L5: Safety Gate Suite
Architecture spec: All Phase 1 safety gates run on every response before output.
Gates implemented:
  L5-1  Completeness Gate
  L5-2  Safety Language Gate
  L5-3  No-Evidence Refusal + Safe Next Steps
  L5-4  Semantic Entropy Detector
  L5-6  Task Gating by Role & Risk
  L5-7  Retraction/Correction Blocking
  L5-9  Edge-Case Detector
  L5-10 Output Completeness Gate (Stop Rules)
  L5-11 Black Box Warning / REMS Priority Gate
  L5-12 Dose Plausibility Checker
  L5-14 Patient Mode Regulatory Boundary Gate
  L5-17 Numeric Deterministic-or-Quoted Gate (enforced in L4-3; re-checked here)
"""
from __future__ import annotations
import re
from datetime import datetime, timezone
from typing import Optional

from curaniq.models.schemas import (
    AtomicClaim,
    ClaimContract,
    ClaimType,
    ClinicalQuery,
    EvidencePack,
    InteractionMode,
    PatientContext,
    SafetyFlag,
    SafetyGateResult,
    SafetyGateSuite,
    UserRole,
)


# ─────────────────────────────────────────────────────────────────────────────
# L5-1: COMPLETENESS GATE
# ─────────────────────────────────────────────────────────────────────────────

def gate_completeness(
    claim_contract: ClaimContract,
    evidence_pack: EvidencePack,
) -> SafetyGateResult:
    """
    L5-1: Ensures the response has a minimum viable evidence base.
    Fails if zero claims passed, or evidence pack is empty.
    """
    passed_claims = [c for c in claim_contract.atomic_claims if not c.is_blocked]

    if len(evidence_pack.objects) == 0:
        return SafetyGateResult(
            gate_id="L5-1",
            gate_name="Completeness Gate",
            passed=False,
            message="No evidence retrieved — cannot generate clinical response without evidence basis.",
            severity="BLOCK",
        )

    if len(passed_claims) == 0:
        return SafetyGateResult(
            gate_id="L5-1",
            gate_name="Completeness Gate",
            passed=False,
            message="All claims were blocked by the Claim Contract Engine — no verifiable clinical content to return.",
            severity="BLOCK",
        )

    if claim_contract.blocked_claims / max(claim_contract.total_claims, 1) > 0.80:
        return SafetyGateResult(
            gate_id="L5-1",
            gate_name="Completeness Gate",
            passed=False,
            message=f"{claim_contract.blocked_claims}/{claim_contract.total_claims} claims blocked (>{80}% failure rate) — insufficient evidence quality.",
            severity="BLOCK",
        )

    return SafetyGateResult(
        gate_id="L5-1",
        gate_name="Completeness Gate",
        passed=True,
        message=f"{len(passed_claims)} verified claims with {len(evidence_pack.objects)} evidence sources.",
        severity="INFO",
    )


# ─────────────────────────────────────────────────────────────────────────────
# L5-2: SAFETY LANGUAGE GATE
# ─────────────────────────────────────────────────────────────────────────────

# Patterns that indicate overconfident language not suitable for clinical output
_OVERCONFIDENT_PATTERNS = [
    re.compile(r'\b(100%\s+safe|completely\s+safe|absolutely\s+safe|guaranteed|certain\s+cure|'
               r'always\s+works|never\s+fails|definitely\s+will|no\s+risk)\b', re.I),
    re.compile(r'\bcure[sd]?\b(?!\s+(for\s+)?(symptom|pain|specific|this|the))', re.I),
    re.compile(r'\b(diagnos[eis]{1,3})\s+(?:is\s+)?(?:definitely|certainly|absolutely)\b', re.I),
]

_APPROPRIATE_HEDGES = [
    "may", "might", "consider", "evidence suggests", "generally", "typically",
    "in most cases", "based on evidence", "guideline recommends", "data supports",
    "uncertain", "limited evidence", "consult", "monitor", "recommend",
]


def gate_safety_language(
    claims: list[AtomicClaim],
    summary_text: Optional[str] = None,
) -> SafetyGateResult:
    """
    L5-2: Ensures output doesn't use overconfident clinical language.
    Medical output must use appropriate hedging for uncertain claims.
    """
    all_text = " ".join(c.claim_text for c in claims)
    if summary_text:
        all_text += " " + summary_text

    violations: list[str] = []
    for pattern in _OVERCONFIDENT_PATTERNS:
        m = pattern.search(all_text)
        if m:
            violations.append(m.group())

    if violations:
        return SafetyGateResult(
            gate_id="L5-2",
            gate_name="Safety Language Gate",
            passed=False,
            message=f"Overconfident clinical language detected: {', '.join(violations[:3])}. Clinical communication requires appropriate epistemic hedging.",
            severity="BLOCK",
        )

    return SafetyGateResult(
        gate_id="L5-2",
        gate_name="Safety Language Gate",
        passed=True,
        severity="INFO",
    )


# ─────────────────────────────────────────────────────────────────────────────
# L5-3: NO-EVIDENCE REFUSAL + SAFE NEXT STEPS
# ─────────────────────────────────────────────────────────────────────────────

def gate_no_evidence_refusal(
    evidence_pack: EvidencePack,
    query_text: str,
    claim_contract: ClaimContract,
) -> tuple[SafetyGateResult, list[str]]:
    """
    L5-3: If insufficient evidence, refuse — but ALWAYS provide safe next steps.
    Never a flat "I can't help". Graceful degradation with guidance.
    Returns (gate_result, safe_next_steps).
    """
    passed_claims = [c for c in claim_contract.atomic_claims if not c.is_blocked]
    safe_next_steps: list[str] = []

    insufficient = (
        len(evidence_pack.objects) == 0 or
        (len(passed_claims) == 0 and len(claim_contract.atomic_claims) > 0)
    )

    if insufficient:
        # Generate safe next steps based on query content
        q_lower = query_text.lower()
        if any(w in q_lower for w in ["dose", "dosing", "mg", "how much"]):
            safe_next_steps += [
                "Consult the official drug prescribing information (SmPC/FDA label)",
                "Use the hospital pharmacy's clinical decision support system",
                "Consult a clinical pharmacist",
            ]
        if any(w in q_lower for w in ["guideline", "recommendation", "protocol"]):
            safe_next_steps += [
                "Access the relevant professional society guidelines (NICE, ESC, AHA, etc.)",
                "Check institutional protocol or clinical pathway",
            ]
        if any(w in q_lower for w in ["interaction", "ddi", "drug"]):
            safe_next_steps += [
                "Check Lexicomp, Micromedex, or Stockley's Drug Interactions",
                "Consult clinical pharmacist for comprehensive DDI screening",
            ]

        safe_next_steps.append("Document clinical reasoning and consult specialist if uncertainty persists.")

        return SafetyGateResult(
            gate_id="L5-3",
            gate_name="No-Evidence Refusal Gate",
            passed=False,
            message="Insufficient evidence to provide a verified clinical answer. Safe next steps provided.",
            severity="BLOCK",
        ), safe_next_steps

    return SafetyGateResult(
        gate_id="L5-3",
        gate_name="No-Evidence Refusal Gate",
        passed=True,
        severity="INFO",
    ), safe_next_steps


# ─────────────────────────────────────────────────────────────────────────────
# L5-4: SEMANTIC ENTROPY DETECTOR
# ─────────────────────────────────────────────────────────────────────────────

def gate_semantic_entropy(
    claims: list[AtomicClaim],
    entropy_threshold: float = 0.4,
) -> SafetyGateResult:
    """
    L5-4: Detects high variance / contradiction within the claim set.
    High contradiction between claims of the same type → refusal or warning.
    Production: samples multiple LLM outputs and measures semantic variance.
    Here: detects contradictory confidence levels within same claim type.
    """
    if len(claims) < 2:
        return SafetyGateResult(gate_id="L5-4", gate_name="Semantic Entropy Detector",
                                passed=True, severity="INFO")

    # Group by claim type and check for high variance in confidence scores
    by_type: dict[ClaimType, list[float]] = {}
    for c in claims:
        if not c.is_blocked:
            if c.claim_type not in by_type:
                by_type[c.claim_type] = []
            by_type[c.claim_type].append(c.confidence_score)

    high_variance_types: list[str] = []
    for ct, scores in by_type.items():
        if len(scores) >= 2:
            variance = max(scores) - min(scores)
            if variance > entropy_threshold:
                high_variance_types.append(f"{ct.value} (spread: {variance:.2f})")

    if high_variance_types:
        return SafetyGateResult(
            gate_id="L5-4",
            gate_name="Semantic Entropy Detector",
            passed=True,     # Warning, not block (unless very high)
            message=f"High confidence variance detected in: {', '.join(high_variance_types)}. Uncertainty marker applied.",
            severity="WARNING",
            flags_raised=[SafetyFlag.EDGE_CASE],
        )

    return SafetyGateResult(gate_id="L5-4", gate_name="Semantic Entropy Detector",
                            passed=True, severity="INFO")


# ─────────────────────────────────────────────────────────────────────────────
# L5-6: TASK GATING BY ROLE & RISK
# ─────────────────────────────────────────────────────────────────────────────

# What each role is allowed to receive
_ROLE_TASK_POLICY: dict[UserRole, dict] = {
    UserRole.CLINICIAN: {
        "allowed_claim_types": list(ClaimType),   # All claim types
        "allowed_modes": list(InteractionMode),
        "dosing_detail": True,
        "ddi_detail": True,
        "contraindications": True,
    },
    UserRole.RESEARCHER: {
        "allowed_claim_types": list(ClaimType),
        "allowed_modes": list(InteractionMode),
        "dosing_detail": False,     # Research context — no clinical dosing directives
        "ddi_detail": True,
        "contraindications": True,
    },
    UserRole.PATIENT: {
        "allowed_claim_types": [ClaimType.GENERAL, ClaimType.MONITORING],
        "allowed_modes": [InteractionMode.QUICK_ANSWER, InteractionMode.DOCUMENT_PROC],
        "dosing_detail": False,
        "ddi_detail": False,
        "contraindications": False,  # FDA 2026 CDS guidance — patient mode restricted
    },
    UserRole.ADMIN: {
        "allowed_claim_types": [ClaimType.GENERAL],
        "allowed_modes": list(InteractionMode),
        "dosing_detail": False,
        "ddi_detail": False,
        "contraindications": False,
    },
}


def gate_task_by_role(
    user_role: UserRole,
    claims: list[AtomicClaim],
    mode: InteractionMode,
) -> SafetyGateResult:
    """
    L5-6: Policy engine enforcing allowed tasks by user role.
    Clinicians get full access; patients cannot receive dosing/DDI/contraindication outputs.
    """
    policy = _ROLE_TASK_POLICY.get(user_role)
    if not policy:
        return SafetyGateResult(
            gate_id="L5-6", gate_name="Task Gating by Role",
            passed=False, message=f"Unknown user role: {user_role}",
            severity="BLOCK",
        )

    if mode not in policy["allowed_modes"]:
        return SafetyGateResult(
            gate_id="L5-6", gate_name="Task Gating by Role",
            passed=False,
            message=f"Mode '{mode.value}' not allowed for role '{user_role.value}'.",
            severity="BLOCK",
        )

    # Check for blocked claim types for this role
    violations: list[str] = []
    for claim in claims:
        if claim.claim_type not in policy["allowed_claim_types"] and not claim.is_blocked:
            violations.append(f"{claim.claim_type.value}: {claim.claim_text[:60]}...")

    if violations:
        return SafetyGateResult(
            gate_id="L5-6", gate_name="Task Gating by Role",
            passed=False,
            message=f"Claims of type {violations[0].split(':')[0]} not permitted for role '{user_role.value}' per regulatory policy.",
            severity="BLOCK",
        )

    return SafetyGateResult(gate_id="L5-6", gate_name="Task Gating by Role",
                            passed=True, severity="INFO")


# ─────────────────────────────────────────────────────────────────────────────
# L5-7: RETRACTION / CORRECTION BLOCKING
# ─────────────────────────────────────────────────────────────────────────────

def gate_retraction_blocking(
    evidence_pack: EvidencePack,
    claims: list[AtomicClaim],
) -> SafetyGateResult:
    """
    L5-7: Final-stage check — any output citing retracted/corrected work blocked.
    Crossref + Retraction Watch checked at evidence ingestion; this enforces the final gate.
    """
    retracted_sources = [e for e in evidence_pack.objects if e.is_retracted]

    if retracted_sources:
        retracted_ids = {e.evidence_id for e in retracted_sources}
        # Find any non-blocked claims citing retracted evidence
        contaminated_claims = [
            c for c in claims
            if not c.is_blocked and any(eid in retracted_ids for eid in c.evidence_ids)
        ]
        if contaminated_claims:
            return SafetyGateResult(
                gate_id="L5-7", gate_name="Retraction/Correction Blocking",
                passed=False,
                message=f"{len(contaminated_claims)} claim(s) cite retracted sources. "
                        f"Retracted: {', '.join(e.source_id for e in retracted_sources[:3])}. "
                        "Response blocked pending evidence refresh.",
                severity="BLOCK",
                flags_raised=[SafetyFlag.RETRACTED_SOURCE],
            )

    return SafetyGateResult(gate_id="L5-7", gate_name="Retraction/Correction Blocking",
                            passed=True, severity="INFO")


# ─────────────────────────────────────────────────────────────────────────────
# L5-9: EDGE-CASE DETECTOR
# ─────────────────────────────────────────────────────────────────────────────

# High-risk patient contexts that require stricter gating
_EDGE_CASE_DETECTORS: list[tuple[str, re.Pattern]] = [
    ("pregnancy",         re.compile(r'\b(pregnan|gestational|trimester|obstetric|fetal|maternal)\b', re.I)),
    ("pediatrics",        re.compile(r'\b(pediatric|paediatric|child|infant|neonate|neonatal|adolescent|\d+\s*months?\s+old|weight.{0,10}kg.{0,20}child)\b', re.I)),
    ("dialysis",          re.compile(r'\b(dialysis|hemodialysis|peritoneal\s+dialysis|CRRT|esrd|end.stage\s+renal)\b', re.I)),
    ("transplant",        re.compile(r'\b(transplant|post.transplant|immunosuppression|rejection|tacrolimus\s+level)\b', re.I)),
    ("extreme_polypharmacy", re.compile(r'\b(\d+\s+medications?|\d+\s+drugs?)\b', re.I)),
    ("rare_disease",      re.compile(r'\b(orphan|rare\s+disease|ultra.rare|incidence\s+<1|1\s+in\s+\d{5,})\b', re.I)),
    ("off_label",         re.compile(r'\b(off.label|unlicensed|unapproved\s+use|outside\s+(the\s+)?indication)\b', re.I)),
    ("active_chemo",      re.compile(r'\b(chemotherapy|chemo|oncology|SACT|antineoplastic|cytotoxic)\b', re.I)),
    ("frailty",           re.compile(r'\b(frail|frailty|CFS|clinical\s+frailty|sarcopenia)\b', re.I)),
    ("hepatic_failure",   re.compile(r'\b(hepatic\s+failure|liver\s+failure|cirrhosis|child.pugh\s+[BC]|MELD)\b', re.I)),
]

# Count of active meds mentioned (rough proxy for polypharmacy)
_POLYPHARMACY_THRESHOLD = 10


def gate_edge_case_detection(
    query_text: str,
    patient_context: Optional[PatientContext] = None,
    claims: Optional[list[AtomicClaim]] = None,
) -> SafetyGateResult:
    """
    L5-9: Detects high-risk patient contexts.
    On detection: switches to stricter gating + conservative defaults + explicit warnings.
    Does NOT block — adds flags and heightened caution messaging.
    """
    detected_edge_cases: list[str] = []

    # Text-based detection
    combined_text = query_text
    if claims:
        combined_text += " " + " ".join(c.claim_text for c in claims)

    for edge_case_name, pattern in _EDGE_CASE_DETECTORS:
        if pattern.search(combined_text):
            detected_edge_cases.append(edge_case_name)

    # Structured context detection
    if patient_context:
        if patient_context.is_pregnant:
            detected_edge_cases.append("pregnancy (confirmed)")
        if patient_context.is_breastfeeding:
            detected_edge_cases.append("breastfeeding")
        if patient_context.renal and patient_context.renal.on_dialysis:
            detected_edge_cases.append("dialysis (confirmed)")
        if patient_context.age_years and patient_context.age_years < 18:
            detected_edge_cases.append(f"pediatric age ({patient_context.age_years}y)")
        if patient_context.age_years and patient_context.age_years >= 80:
            detected_edge_cases.append(f"extreme elderly ({patient_context.age_years}y)")
        if len(patient_context.active_medications) >= _POLYPHARMACY_THRESHOLD:
            detected_edge_cases.append(
                f"extreme polypharmacy ({len(patient_context.active_medications)} medications)"
            )

    detected_edge_cases = list(set(detected_edge_cases))

    if detected_edge_cases:
        return SafetyGateResult(
            gate_id="L5-9",
            gate_name="Edge-Case Detector",
            passed=True,    # Does not block — activates stricter mode
            message=(
                f"HIGH-RISK PATIENT CONTEXT DETECTED: {', '.join(detected_edge_cases)}. "
                "Standard evidence may not apply. Stricter verification activated. "
                "All recommendations carry 'limited evidence in this population' caveat. "
                "Specialist consultation strongly recommended."
            ),
            severity="WARNING",
            flags_raised=[SafetyFlag.EDGE_CASE, SafetyFlag.HIGH_RISK_PATIENT],
        )

    return SafetyGateResult(gate_id="L5-9", gate_name="Edge-Case Detector",
                            passed=True, severity="INFO")


# ─────────────────────────────────────────────────────────────────────────────
# L5-10: OUTPUT COMPLETENESS GATE (STOP RULES)
# ─────────────────────────────────────────────────────────────────────────────

def gate_output_completeness(
    claims: list[AtomicClaim],
    has_monitoring: bool,
    has_stop_rules: bool,
    has_escalation_thresholds: bool,
    has_follow_up: bool,
) -> tuple[SafetyGateResult, list[str]]:
    """
    L5-10: Every actionable plan MUST include:
    - What to monitor
    - When to stop/hold
    - Escalation thresholds
    - Follow-up interval
    Returns (gate_result, missing_elements).
    """
    dosing_claims = [c for c in claims if c.claim_type == ClaimType.DOSING and not c.is_blocked]

    # Only enforce if there are actionable dosing recommendations
    if not dosing_claims:
        return SafetyGateResult(gate_id="L5-10", gate_name="Output Completeness Gate",
                                passed=True, severity="INFO"), []

    missing: list[str] = []
    if not has_monitoring:
        missing.append("monitoring parameters (e.g., labs, vitals, symptoms)")
    if not has_stop_rules:
        missing.append("stop/hold criteria (when to discontinue or pause therapy)")
    if not has_escalation_thresholds:
        missing.append("escalation thresholds (when to seek urgent specialist review)")
    if not has_follow_up:
        missing.append("follow-up interval (when to reassess)")

    if missing:
        return SafetyGateResult(
            gate_id="L5-10",
            gate_name="Output Completeness Gate (Stop Rules)",
            passed=False,
            message=f"Actionable dosing response is missing: {'; '.join(missing)}. Safe clinical recommendations require these elements.",
            severity="WARNING",  # Warning not block — pipeline adds safe defaults
        ), missing

    return SafetyGateResult(gate_id="L5-10", gate_name="Output Completeness Gate",
                            passed=True, severity="INFO"), []


# ─────────────────────────────────────────────────────────────────────────────
# L5-11: BLACK BOX WARNING / REMS PRIORITY GATE
# ─────────────────────────────────────────────────────────────────────────────

def gate_black_box_rems(
    claims: list[AtomicClaim],
) -> SafetyGateResult:
    """
    L5-11: FDA Black Box Warnings and REMS requirements displayed with maximum prominence.
    Cannot be dismissed without clinician confirmation.
    """
    bb_claims = [c for c in claims if SafetyFlag.BLACK_BOX_WARNING in c.safety_flags and not c.is_blocked]
    rems_claims = [c for c in claims if SafetyFlag.REMS_REQUIRED in c.safety_flags and not c.is_blocked]

    messages: list[str] = []
    if bb_claims:
        messages.append(f"⬛ FDA BLACK BOX WARNING applies to {len(bb_claims)} claim(s). Mandatory clinician acknowledgment required.")
    if rems_claims:
        messages.append(f"⚠️ REMS PROGRAM REQUIRED for {len(rems_claims)} claim(s). Enrollment verification mandatory before prescribing.")

    if messages:
        return SafetyGateResult(
            gate_id="L5-11",
            gate_name="Black Box Warning / REMS Priority Gate",
            passed=True,    # Does not block — surfaces with mandatory acknowledgment flag
            message=" | ".join(messages),
            severity="WARNING",
            flags_raised=[SafetyFlag.BLACK_BOX_WARNING] if bb_claims else [],
        )

    return SafetyGateResult(gate_id="L5-11", gate_name="Black Box Warning / REMS Priority Gate",
                            passed=True, severity="INFO")


# ─────────────────────────────────────────────────────────────────────────────
# L5-12: DOSE PLAUSIBILITY CHECKER
# ─────────────────────────────────────────────────────────────────────────────

# Historical fatal medication errors database
# Source: ISMP (Institute for Safe Medication Practices) Medication Error Reports
FATAL_DOSE_ERRORS: list[dict] = [
    {
        "drug": "methotrexate",
        "error": "daily vs weekly",
        "safe_pattern": re.compile(r'\bmethotrexate\b.{0,60}\b(weekly|once\s+a\s+week|per\s+week|week)\b', re.I),
        "danger_pattern": re.compile(r'\bmethotrexate\b.{0,60}\b(daily|every\s+day|once\s+daily|od)\b', re.I),
        "message": "METHOTREXATE FATAL ERROR RISK: Methotrexate for non-oncology indications is WEEKLY, not daily. Daily dosing causes fatal bone marrow suppression. ISMP Sentinel Event.",
    },
    {
        "drug": "colchicine",
        "error": "6mg vs 0.6mg",
        "danger_pattern": re.compile(r'\bcolchicine\b.{0,60}\b(\d+\.?\d*\s*mg)\b', re.I),
        "dose_limit_mg": 1.8,   # Max safe acute dose
        "message": "COLCHICINE DOSE CHECK: Doses >1.8mg in acute gout or >0.6mg BID in prophylaxis are associated with toxicity. Verify intended dose carefully.",
    },
    {
        "drug": "vincristine",
        "error": "intrathecal vs intravenous",
        "danger_pattern": re.compile(r'\bvincristine\b.{0,60}\b(intrathecal|IT\s+injection|IT\s+admin|spinal)\b', re.I),
        "message": "VINCRISTINE FATAL ROUTE ERROR: Intrathecal vincristine is ALWAYS FATAL. Vincristine is administered IV ONLY. This response has been blocked.",
        "severity": "EMERGENCY",
    },
    {
        "drug": "heparin",
        "error": "units vs mg confusion",
        "danger_pattern": re.compile(r'\bheparin\b.{0,60}\b(\d{4,}\s*mg|\d+\s*mg)\b(?!.*units)', re.I),
        "message": "HEPARIN UNIT CONFUSION: Heparin is dosed in UNITS, not mg. 1000 units ≠ 1000mg. Verify unit specification. 10x and 100x overdoses from unit/mg confusion are ISMP Sentinel Events.",
    },
    {
        "drug": "insulin",
        "error": "U misread as 0",
        "danger_pattern": re.compile(r'\binsulin\b.{0,60}\b\d+\s*U\b', re.I),
        "message": "INSULIN UNIT NOTATION: Always write 'units' not 'U' to prevent misreading (e.g., '10U' misread as '100'). ISMP High-Alert Medication.",
    },
    {
        "drug": "morphine",
        "error": "route and dose confusion",
        "danger_pattern": re.compile(r'\bmorphine\b.{0,60}\b(\d{2,}\s*mg|\d+\s*mg.{0,20}(oral|PO))\b', re.I),
        "dose_limit_opioid_naive_mg": 15,
        "message": "MORPHINE DOSE ALERT: Doses >15mg in opioid-naive patients or incorrect route specification carry high overdose risk. Verify opioid tolerance status.",
    },
]


def gate_dose_plausibility(
    claims: list[AtomicClaim],
) -> SafetyGateResult:
    """
    L5-12: Catches order-of-magnitude dosing errors that kill patients.
    Checks against ISMP Sentinel Event database of historical fatal errors.
    """
    violations: list[str] = []
    emergency_blocks: list[str] = []

    for claim in claims:
        if claim.is_blocked:
            continue
        text = claim.claim_text

        for error_spec in FATAL_DOSE_ERRORS:
            drug = error_spec["drug"]
            if drug not in text.lower():
                continue

            danger_match = error_spec.get("danger_pattern")
            if danger_match and danger_match.search(text):
                sev = error_spec.get("severity", "")
                msg = error_spec["message"]
                if sev == "EMERGENCY":
                    emergency_blocks.append(msg)
                else:
                    violations.append(msg)

            # Check safe pattern requirement
            safe_match = error_spec.get("safe_pattern")
            if safe_match and not safe_match.search(text) and drug in text.lower():
                if "weekly" in error_spec.get("error", ""):
                    violations.append(
                        f"WARNING: {drug.capitalize()} dosing frequency not specified as 'weekly' — "
                        "high-risk omission for methotrexate."
                    )

    if emergency_blocks:
        return SafetyGateResult(
            gate_id="L5-12",
            gate_name="Dose Plausibility Checker",
            passed=False,
            message=" | ".join(emergency_blocks),
            severity="BLOCK",
            flags_raised=[SafetyFlag.DOSE_IMPLAUSIBLE],
        )

    if violations:
        return SafetyGateResult(
            gate_id="L5-12",
            gate_name="Dose Plausibility Checker",
            passed=True,
            message=" | ".join(violations[:2]),
            severity="WARNING",
            flags_raised=[SafetyFlag.DOSE_IMPLAUSIBLE],
        )

    return SafetyGateResult(gate_id="L5-12", gate_name="Dose Plausibility Checker",
                            passed=True, severity="INFO")


# ─────────────────────────────────────────────────────────────────────────────
# L5-14: PATIENT MODE REGULATORY BOUNDARY GATE
# ─────────────────────────────────────────────────────────────────────────────

_PATIENT_FORBIDDEN_CLAIM_TYPES = {
    ClaimType.DOSING,
    ClaimType.CONTRAINDICATION,
    ClaimType.DRUG_INTERACTION,
}

_PATIENT_FORBIDDEN_PATTERNS = re.compile(
    r'\b(take\s+\d+\s*mg|dose\s+is|your\s+dose|you\s+should\s+take|'
    r'prescribed\s+dose|titrate|start\s+with\s+\d|increase\s+dose)\b',
    re.I
)


def gate_patient_mode_boundary(
    user_role: UserRole,
    claims: list[AtomicClaim],
    mode: InteractionMode,
) -> SafetyGateResult:
    """
    L5-14: FDA January 2026 CDS guidance enforcement.
    Patient/caregiver mode CANNOT receive dosing, DDI, contraindication outputs.
    Any attempt to route such output to patient role → blocked and logged.
    """
    if user_role != UserRole.PATIENT:
        return SafetyGateResult(gate_id="L5-14", gate_name="Patient Mode Regulatory Boundary",
                                passed=True, severity="INFO")

    violations: list[str] = []
    for claim in claims:
        if claim.is_blocked:
            continue
        if claim.claim_type in _PATIENT_FORBIDDEN_CLAIM_TYPES:
            violations.append(f"{claim.claim_type.value} claim in patient mode")
        elif _PATIENT_FORBIDDEN_PATTERNS.search(claim.claim_text):
            violations.append("Direct dosing directive detected in patient-mode output")

    if violations:
        return SafetyGateResult(
            gate_id="L5-14",
            gate_name="Patient Mode Regulatory Boundary Gate",
            passed=False,
            message=(
                f"FDA 2026 CDS guidance violation: {violations[0]}. "
                "Patient mode is restricted to education, red-flag escalation, general wellness, "
                "and non-directive medication reminders only. Dosing/DDI/contraindication "
                "information requires clinician role."
            ),
            severity="BLOCK",
        )

    return SafetyGateResult(gate_id="L5-14", gate_name="Patient Mode Regulatory Boundary Gate",
                            passed=True, severity="INFO")


# ─────────────────────────────────────────────────────────────────────────────
# SAFETY GATE SUITE ORCHESTRATOR
# ─────────────────────────────────────────────────────────────────────────────



# ─────────────────────────────────────────────────────────────────────────────
# L5-17: NUMERIC DETERMINISTIC-OR-QUOTED GATE (defense-in-depth)
# Architecture: "Every number must be deterministic (CQL) OR verbatim-quoted
# (hash-bound). Even one unverifiable numeric value = BLOCK."
# This is THE differentiator vs GPT/Gemini. They hallucinate numbers.
# ─────────────────────────────────────────────────────────────────────────────

def gate_numeric_verification(
    claim_contract: ClaimContract,
) -> SafetyGateResult:
    """
    L5-17 defense-in-depth check.
    Reads numeric token verification status from claim_contract
    (already computed by ClaimContractEngine).
    If ANY numeric token is BLOCKED, entire response is flagged.

    No regex. No hardcoded patterns. Just reads the verification
    results that the claim contract already computed.
    """
    from curaniq.models.schemas import NumericTokenStatus

    total_numeric = 0
    blocked_numeric = 0
    blocked_details: list[str] = []

    for claim in claim_contract.atomic_claims:
        for nt in claim.numeric_tokens:
            total_numeric += 1
            if nt.status == NumericTokenStatus.BLOCKED:
                blocked_numeric += 1
                blocked_details.append(
                    f"'{nt.value_str}' in claim: '{claim.claim_text[:60]}...'"
                )

    if total_numeric == 0:
        return SafetyGateResult(
            gate_id="L5-17",
            gate_name="Numeric Deterministic-or-Quoted Gate",
            passed=True,
            message="No numeric values in output — gate not applicable.",
            severity="INFO",
        )

    if blocked_numeric > 0:
        return SafetyGateResult(
            gate_id="L5-17",
            gate_name="Numeric Deterministic-or-Quoted Gate",
            passed=False,
            message=(
                f"NUMERIC SAFETY BLOCK: {blocked_numeric}/{total_numeric} "
                f"numeric value(s) could not be verified as deterministic (CQL) "
                f"or verbatim from evidence. Unverified: "
                + "; ".join(blocked_details[:3])
                + (f" (+{blocked_numeric - 3} more)" if blocked_numeric > 3 else "")
            ),
            severity="BLOCK",
        )

    # All numeric tokens verified
    verified_det = sum(
        1 for c in claim_contract.atomic_claims
        for nt in c.numeric_tokens
        if nt.status == NumericTokenStatus.DETERMINISTIC
    )
    verified_verb = total_numeric - verified_det

    return SafetyGateResult(
        gate_id="L5-17",
        gate_name="Numeric Deterministic-or-Quoted Gate",
        passed=True,
        message=(
            f"All {total_numeric} numeric value(s) verified: "
            f"{verified_det} deterministic (CQL), "
            f"{verified_verb} verbatim from evidence."
        ),
        severity="INFO",
    )

class SafetyGateSuiteRunner:
    """
    Runs all L5 safety gates in the correct sequence.
    Returns a SafetyGateSuite with all results.
    Any BLOCK gate causes hard_block=True and prevents output.
    """

    def run_all(
        self,
        query: ClinicalQuery,
        claim_contract: ClaimContract,
        evidence_pack: EvidencePack,
        mode: InteractionMode,
        has_monitoring: bool = False,
        has_stop_rules: bool = False,
        has_escalation_thresholds: bool = False,
        has_follow_up: bool = False,
    ) -> tuple[SafetyGateSuite, list[str]]:
        """
        Execute all gates. Returns (SafetyGateSuite, safe_next_steps).
        Gates execute in order of blocking priority.
        """
        all_gates: list[SafetyGateResult] = []
        all_safe_next_steps: list[str] = []

        claims = claim_contract.atomic_claims

        # Gate 1: L5-7 Retraction Blocking (highest priority — data integrity)
        all_gates.append(gate_retraction_blocking(evidence_pack, claims))

        # Gate 2: L5-14 Patient Mode Boundary (regulatory compliance)
        all_gates.append(gate_patient_mode_boundary(query.user_role, claims, mode))

        # Gate 3: L5-6 Task Gating by Role
        all_gates.append(gate_task_by_role(query.user_role, claims, mode))

        # Gate 4: L5-3 No Evidence Refusal (gate + safe next steps)
        g3, next_steps = gate_no_evidence_refusal(evidence_pack, query.raw_text, claim_contract)
        all_gates.append(g3)
        all_safe_next_steps.extend(next_steps)

        # Gate 5: L5-1 Completeness
        all_gates.append(gate_completeness(claim_contract, evidence_pack))

        # Gate 6: L5-12 Dose Plausibility (before language check)
        all_gates.append(gate_dose_plausibility(claims))

        # Gate 7: L5-2 Safety Language
        all_gates.append(gate_safety_language(claims))

        # Gate 8: L5-9 Edge-Case Detection (enriches warnings)
        all_gates.append(gate_edge_case_detection(
            query.raw_text, query.patient_context, claims
        ))

        # Gate 9: L5-4 Semantic Entropy
        all_gates.append(gate_semantic_entropy(claims))

        # Gate 10: L5-10 Output Completeness / Stop Rules
        g10, missing_elements = gate_output_completeness(
            claims, has_monitoring, has_stop_rules, has_escalation_thresholds, has_follow_up
        )
        all_gates.append(g10)
        if missing_elements:
            all_safe_next_steps.append(
                f"This response is missing: {', '.join(missing_elements)}. "
                "Add these to your clinical documentation."
            )

        # Gate 11: L5-11 Black Box / REMS
        all_gates.append(gate_black_box_rems(claims))

        # Gate 12: L5-17 Numeric Deterministic-or-Quoted (defense-in-depth)
        # Every number must be CQL-computed or verbatim from evidence.
        # This is CURANIQ's #1 differentiator vs GPT/Gemini.
        all_gates.append(gate_numeric_verification(claim_contract))

        suite = SafetyGateSuite(
            query_id=query.query_id,
            gates=all_gates,
            overall_passed=True,  # Will be recomputed by model_validator
        )

        return suite, all_safe_next_steps
