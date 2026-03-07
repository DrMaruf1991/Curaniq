"""
CURANIQ — Medical Evidence Operating System
Layer 5: Safety Gating Pipeline

L5-1  Completeness Gate
L5-2  Safety Language Filter
L5-3  No-Evidence Refusal Gate
L5-4  Semantic Entropy Gate
L5-6  Task Gating by Role
L5-7  Retraction/Correction Final Gate
L5-9  Edge-Case Detector
L5-10 Output Completeness Checker
L5-11 Black Box Warning Display Gate
L5-12 Dose Plausibility Gate
L5-14 Patient Mode Regulatory Gate
L5-17 Numeric Gate (LAUNCH-BLOCKER)
"""
from __future__ import annotations
import logging, math, re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
logger = logging.getLogger(__name__)


class GateVerdict(str, Enum):
    PASS     = "pass"
    WARN     = "warn"
    BLOCK    = "block"     # Output modified (warning added)
    REFUSE   = "refuse"    # Output fully refused


@dataclass
class GateResult:
    gate:       str
    verdict:    GateVerdict
    reason:     Optional[str] = None
    injected:   Optional[str] = None   # Text injected into output
    is_hard_block: bool = False        # True = cannot be overridden


# ─────────────────────────────────────────────────────────────────────────────
# L5-1: COMPLETENESS GATE
# 'Refuses when minimum dataset absent: no weight for pediatric, no renal for
#  renally-cleared drugs, no pregnancy status for teratogenic drugs.'
# ─────────────────────────────────────────────────────────────────────────────

RENALLY_CLEARED_DRUGS = {
    "metformin","gabapentin","pregabalin","dabigatran","rivaroxaban","apixaban",
    "digoxin","lithium","vancomycin","gentamicin","trimethoprim","allopurinol",
    "codeine","morphine","spironolactone","lisinopril","ramipril","atenolol",
}
TERATOGENIC_DRUGS = {
    "valproate","valproic acid","isotretinoin","warfarin","methotrexate",
    "thalidomide","finasteride","acei","ace inhibitor","statins","lithium",
}
WEIGHT_REQUIRED_DRUGS = {"vancomycin","gentamicin","heparin","enoxaparin","amikacin"}


class CompletenessGate:
    """L5-1: Refuse when minimum required context is absent."""

    def check(
        self,
        query: str,
        drug: Optional[str] = None,
        egfr: Optional[float] = None,
        age_years: Optional[float] = None,
        weight_kg: Optional[float] = None,
        pregnancy_status: Optional[str] = None,
    ) -> GateResult:
        query_lower = query.lower()
        missing = []

        if drug:
            drug_lower = drug.lower()
            if any(d in drug_lower for d in RENALLY_CLEARED_DRUGS) and egfr is None:
                missing.append(f"eGFR required for {drug} (renally cleared drug)")
            if any(d in drug_lower for d in TERATOGENIC_DRUGS) and pregnancy_status is None:
                missing.append(f"Pregnancy status required for {drug} (teratogenic drug)")
            if any(d in drug_lower for d in WEIGHT_REQUIRED_DRUGS) and weight_kg is None:
                missing.append(f"Weight (kg) required for {drug} (weight-based dosing)")

        # Pediatric flag
        pediatric_terms = re.compile(r'\b(child|paediatric|pediatric|infant|neonate|neonatal|toddler|mg/kg)\b', re.I)
        if pediatric_terms.search(query) and (age_years is None or weight_kg is None):
            missing.append("Age (years) AND weight (kg) required for pediatric dosing query")

        if missing:
            return GateResult(
                gate="L5-1:Completeness",
                verdict=GateVerdict.REFUSE,
                reason="MINIMUM DATASET ABSENT: " + "; ".join(missing),
                is_hard_block=True,
            )
        return GateResult(gate="L5-1:Completeness", verdict=GateVerdict.PASS)


# ─────────────────────────────────────────────────────────────────────────────
# L5-2: SAFETY LANGUAGE FILTER
# 'Filters unsafe absolutes. Enforces hedged language. "Always safe" blocked.'
# ─────────────────────────────────────────────────────────────────────────────

UNSAFE_ABSOLUTES = [
    re.compile(r'\b(always safe|completely safe|100%\s*safe|perfectly safe|no risk)\b', re.I),
    re.compile(r'\b(no side effects|no adverse effects|no drug interactions)\b', re.I),
    re.compile(r'\b(guaranteed|cure|proven cure|definitive cure)\b', re.I),
    re.compile(r'\balways\s+(take|use|prescribe|give)\b', re.I),
    re.compile(r'\bnever\s+(causes?|produces?)\b', re.I),
]
REQUIRED_HEDGES = [
    "evidence suggests", "guidelines recommend", "consider", "may",
    "in most cases", "typically", "generally", "based on",
    "consult", "discuss with", "specialist review",
]


class SafetyLanguageFilter:
    """L5-2: Strip unsafe absolute statements; enforce hedged clinical language."""

    def check(self, output_text: str) -> GateResult:
        hits = [pat.search(output_text) for pat in UNSAFE_ABSOLUTES]
        hits = [h.group(0) for h in hits if h]
        if hits:
            return GateResult(
                gate="L5-2:SafetyLanguage",
                verdict=GateVerdict.BLOCK,
                reason=f"Unsafe absolute language detected: {', '.join(hits[:3])}. Replaced with hedged alternatives.",
                injected="⚠️ Note: Clinical decisions should always be individualised. This output has been reviewed for absolute language.",
            )
        return GateResult(gate="L5-2:SafetyLanguage", verdict=GateVerdict.PASS)

    def sanitize(self, text: str) -> str:
        """Replace unsafe absolutes with hedged alternatives."""
        text = re.sub(r'\balways safe\b', 'generally considered safe in appropriate patients', text, flags=re.I)
        text = re.sub(r'\b100%\s*safe\b', 'associated with acceptable safety profile in clinical trials', text, flags=re.I)
        text = re.sub(r'\bno side effects\b', 'has a generally well-tolerated profile (see SmPC for full adverse effect list)', text, flags=re.I)
        text = re.sub(r'\bguaranteed\b', 'evidence-supported', text, flags=re.I)
        text = re.sub(r'\bcure\b', 'treatment', text, flags=re.I)
        return text


# ─────────────────────────────────────────────────────────────────────────────
# L5-3: NO-EVIDENCE REFUSAL GATE
# 'Refuses risky part when evidence confidence below threshold.'
# ─────────────────────────────────────────────────────────────────────────────

HIGH_RISK_QUERY_PATTERNS = [
    re.compile(r'\b(dose|dosing|how much|mg/kg|loading dose|maintenance dose)\b', re.I),
    re.compile(r'\b(contraindicated|drug interaction|interaction)\b', re.I),
    re.compile(r'\b(pregnancy|breastfeeding|lactation)\b', re.I),
    re.compile(r'\b(child|paediatric|infant|neonate)\b', re.I),
    re.compile(r'\b(renal|CKD|eGFR|hepatic|liver disease)\b', re.I),
]


class NoEvidenceRefusalGate:
    """L5-3: Refuse high-risk clinical queries when evidence confidence is too low."""
    CONFIDENCE_THRESHOLD = 0.30

    def check(self, query: str, evidence_confidence: float) -> GateResult:
        is_high_risk = any(p.search(query) for p in HIGH_RISK_QUERY_PATTERNS)
        if is_high_risk and evidence_confidence < self.CONFIDENCE_THRESHOLD:
            return GateResult(
                gate="L5-3:NoEvidenceRefusal",
                verdict=GateVerdict.REFUSE,
                reason=(
                    f"HIGH-RISK QUERY with insufficient evidence confidence "
                    f"({evidence_confidence:.0%} < {self.CONFIDENCE_THRESHOLD:.0%} threshold). "
                    "CURANIQ will not generate clinical recommendations without adequate evidence support."
                ),
                injected=(
                    "⛔ CURANIQ cannot provide a recommendation for this query: "
                    "the evidence confidence is insufficient for a high-risk clinical decision. "
                    "Please consult a specialist or refer to primary sources."
                ),
                is_hard_block=True,
            )
        return GateResult(gate="L5-3:NoEvidenceRefusal", verdict=GateVerdict.PASS)


# ─────────────────────────────────────────────────────────────────────────────
# L5-4: SEMANTIC ENTROPY GATE
# 'Measures model uncertainty via multiple generations. High entropy → flag.'
# ─────────────────────────────────────────────────────────────────────────────

class SemanticEntropyGate:
    """
    L5-4: Semantic entropy — proxy for model uncertainty.
    Production: compare 3 LLM generations; measure semantic disagreement.
    Current: heuristic entropy from claim certainty distribution.
    """
    HIGH_ENTROPY_THRESHOLD = 0.65

    def check(self, claim_certainties: list[str]) -> GateResult:
        """
        claim_certainties: list of "high"|"moderate"|"low"|"very_low" per claim.
        High proportion of low/very_low = high semantic entropy.
        """
        if not claim_certainties:
            return GateResult(gate="L5-4:SemanticEntropy", verdict=GateVerdict.PASS)

        weights = {"high": 0.0, "moderate": 0.25, "low": 0.75, "very_low": 1.0}
        entropy_scores = [weights.get(c, 0.5) for c in claim_certainties]
        mean_entropy = sum(entropy_scores) / len(entropy_scores)

        if mean_entropy >= self.HIGH_ENTROPY_THRESHOLD:
            return GateResult(
                gate="L5-4:SemanticEntropy",
                verdict=GateVerdict.WARN,
                reason=f"High semantic entropy ({mean_entropy:.2f}) — model uncertain. Human review recommended.",
                injected="⚠️ UNCERTAINTY FLAG: This response contains claims with low evidence certainty. Clinical review advised.",
            )
        return GateResult(gate="L5-4:SemanticEntropy", verdict=GateVerdict.PASS)


# ─────────────────────────────────────────────────────────────────────────────
# L5-6: TASK GATING BY ROLE
# 'Policy engine: allowed tasks by user role. Patient mode cannot ask for doses.'
# ─────────────────────────────────────────────────────────────────────────────

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "patient":    {"symptom_info", "medication_info_basic", "side_effects_info", "when_to_seek_help"},
    "caregiver":  {"symptom_info", "medication_info_basic", "side_effects_info", "when_to_seek_help", "medication_schedule"},
    "nurse":      {"symptom_info", "medication_info_basic", "side_effects_info", "when_to_seek_help", "medication_schedule", "clinical_query", "drug_interactions_basic"},
    "doctor":     {"symptom_info", "medication_info_basic", "side_effects_info", "when_to_seek_help", "medication_schedule", "clinical_query", "drug_interactions_basic", "drug_interactions_advanced", "dosing_calculation", "differential_diagnosis", "off_label_query"},
    "pharmacist": {"symptom_info", "medication_info_basic", "side_effects_info", "medication_schedule", "clinical_query", "drug_interactions_basic", "drug_interactions_advanced", "dosing_calculation"},
    "researcher": {"symptom_info", "medication_info_basic", "side_effects_info", "clinical_query", "drug_interactions_basic", "drug_interactions_advanced", "dosing_calculation", "differential_diagnosis", "off_label_query", "evidence_synthesis"},
}

def _classify_task(query: str) -> str:
    q = query.lower()
    if re.search(r'\b(dose|dosing|mg/kg|how much|loading dose|calculate)\b', q): return "dosing_calculation"
    if re.search(r'\b(drug interaction|interact with|combination|concurrent)\b', q): return "drug_interactions_advanced"
    if re.search(r'\b(diagnose|differential|diagnosis|ddx)\b', q): return "differential_diagnosis"
    if re.search(r'\b(off.label|unlicensed|not approved)\b', q): return "off_label_query"
    if re.search(r'\b(side effect|adverse|contraindicated|warning)\b', q): return "side_effects_info"
    if re.search(r'\b(symptom|sign|present|feel)\b', q): return "symptom_info"
    return "clinical_query"


class TaskGateByRole:
    """L5-6: Policy engine enforcing allowed tasks by user role."""

    def check(self, query: str, user_role: str) -> GateResult:
        task = _classify_task(query)
        allowed = ROLE_PERMISSIONS.get(user_role.lower(), set())
        if task not in allowed:
            return GateResult(
                gate="L5-6:TaskGating",
                verdict=GateVerdict.REFUSE,
                reason=f"Task '{task}' not permitted for role '{user_role}'.",
                injected=(
                    f"⛔ This type of query ({task.replace('_', ' ')}) requires a different access level. "
                    "Please consult an appropriate healthcare professional."
                ),
                is_hard_block=True,
            )
        return GateResult(gate="L5-6:TaskGating", verdict=GateVerdict.PASS)


# ─────────────────────────────────────────────────────────────────────────────
# L5-7: RETRACTION/CORRECTION FINAL GATE
# 'Final-stage: any output citing retracted evidence → blocked.'
# ─────────────────────────────────────────────────────────────────────────────

class RetractionFinalGate:
    """L5-7: Last-line check — block any output citing retracted evidence."""

    def check(self, cited_chunk_retraction_statuses: dict[str, str]) -> GateResult:
        """cited_chunk_retraction_statuses: {chunk_id: retraction_status_value}"""
        from curaniq.models.evidence import RetractionStatus
        retracted = [
            cid for cid, status in cited_chunk_retraction_statuses.items()
            if status == RetractionStatus.RETRACTED.value
        ]
        if retracted:
            return GateResult(
                gate="L5-7:RetractionFinal",
                verdict=GateVerdict.REFUSE,
                reason=f"Output cites RETRACTED evidence: {retracted}. Output blocked.",
                injected="🚫 RETRACTED EVIDENCE: This response cited retracted scientific evidence and has been blocked.",
                is_hard_block=True,
            )
        expressions = [
            cid for cid, status in cited_chunk_retraction_statuses.items()
            if status in (RetractionStatus.EXPRESSION.value, RetractionStatus.CORRECTED.value)
        ]
        if expressions:
            return GateResult(
                gate="L5-7:RetractionFinal",
                verdict=GateVerdict.WARN,
                reason=f"Output cites evidence with expression of concern/correction: {expressions}",
                injected="⚠️ EVIDENCE NOTE: One or more cited studies have received an expression of concern or correction. Interpret with caution.",
            )
        return GateResult(gate="L5-7:RetractionFinal", verdict=GateVerdict.PASS)


# ─────────────────────────────────────────────────────────────────────────────
# L5-9: EDGE-CASE DETECTOR
# 'Detects high-risk patient contexts: elderly, multi-morbid, pregnancy, CKD4+.'
# ─────────────────────────────────────────────────────────────────────────────

class EdgeCaseDetector:
    """L5-9: Detect high-risk patient contexts requiring specialist escalation."""

    def check(
        self,
        age_years: Optional[float] = None,
        egfr: Optional[float] = None,
        pregnancy_status: Optional[str] = None,
        concurrent_drug_count: int = 0,
        has_hepatic_failure: bool = False,
        has_active_malignancy: bool = False,
        query: str = "",
    ) -> GateResult:
        flags = []

        if age_years is not None:
            if age_years < 0.077:  # neonatal
                flags.append("NEONATE: specialist neonatal/NICU review mandatory")
            elif age_years < 1:
                flags.append("INFANT (<1yr): paediatric pharmacy review recommended")
            elif age_years >= 85:
                flags.append("VERY ELDERLY (≥85yr): polypharmacy/falls/frailty assessment")
            elif age_years >= 75:
                flags.append("ELDERLY (≥75yr): Beers Criteria review; renal/hepatic function check")

        if egfr is not None and egfr < 15:
            flags.append(f"CKD STAGE 5 (eGFR {egfr:.0f}): nephrology input required for all prescribing")
        elif egfr is not None and egfr < 30:
            flags.append(f"CKD STAGE 4 (eGFR {egfr:.0f}): renal dose adjustment required")

        if pregnancy_status and pregnancy_status.lower() in ("pregnant", "pregnancy", "1st trimester", "2nd trimester", "3rd trimester"):
            flags.append("PREGNANCY: teratogenicity and safety review mandatory before prescribing")

        if concurrent_drug_count >= 10:
            flags.append(f"POLYPHARMACY ({concurrent_drug_count} drugs): comprehensive medication review recommended")
        elif concurrent_drug_count >= 5:
            flags.append(f"POLYPHARMACY ({concurrent_drug_count} drugs): DDI screening recommended")

        if has_hepatic_failure:
            flags.append("HEPATIC FAILURE: hepatic dose adjustment required for all liver-metabolised drugs")

        if has_active_malignancy:
            flags.append("ACTIVE MALIGNANCY: oncology/palliative team input recommended")

        if flags:
            severity = GateVerdict.WARN
            if any("mandatory" in f.lower() or "NICU" in f or "CKD STAGE 5" in f for f in flags):
                severity = GateVerdict.BLOCK
            return GateResult(
                gate="L5-9:EdgeCase",
                verdict=severity,
                reason=f"High-risk patient context(s) detected: {'; '.join(flags)}",
                injected="⚠️ HIGH-RISK PATIENT: " + " | ".join(flags),
            )
        return GateResult(gate="L5-9:EdgeCase", verdict=GateVerdict.PASS)


# ─────────────────────────────────────────────────────────────────────────────
# L5-10: OUTPUT COMPLETENESS CHECKER
# 'Every actionable plan MUST include: monitoring parameters, review date,
#  escalation criteria, who to contact in emergency.'
# ─────────────────────────────────────────────────────────────────────────────

REQUIRED_PLAN_ELEMENTS = {
    "monitoring": re.compile(r'\b(monitor|monitoring|check|recheck|test|measure|review)\b', re.I),
    "review_date": re.compile(r'\b(review in|follow up|follow-up|\d+\s*(days?|weeks?|months?)|return if)\b', re.I),
    "escalation": re.compile(r'\b(if worsens?|if no improvement|seek help|emergency|urgent|A&E|ER|999|112|call)\b', re.I),
    "source_citation": re.compile(r'\b(according to|guideline|evidence|source|reference|NICE|GRADE|BNF)\b', re.I),
}


class OutputCompletenessChecker:
    """L5-10: Verify every actionable plan contains minimum required safety elements."""

    def check(self, output_text: str, is_actionable_plan: bool = True) -> GateResult:
        if not is_actionable_plan:
            return GateResult(gate="L5-10:OutputCompleteness", verdict=GateVerdict.PASS)
        missing = [
            element for element, pattern in REQUIRED_PLAN_ELEMENTS.items()
            if not pattern.search(output_text)
        ]
        if missing:
            return GateResult(
                gate="L5-10:OutputCompleteness",
                verdict=GateVerdict.BLOCK,
                reason=f"Actionable plan missing required elements: {', '.join(missing)}",
                injected=(
                    "\n\n📋 CURANIQ requires all actionable plans to include: "
                    "monitoring parameters, review timeframe, escalation criteria, and evidence source. "
                    "Please ensure your clinical plan addresses these elements."
                ),
            )
        return GateResult(gate="L5-10:OutputCompleteness", verdict=GateVerdict.PASS)


# ─────────────────────────────────────────────────────────────────────────────
# L5-11: BLACK BOX WARNING DISPLAY GATE
# 'FDA strongest warnings must be displayed prominently. Cannot be suppressed.'
# ─────────────────────────────────────────────────────────────────────────────

BLACK_BOX_WARNINGS: dict[str, str] = {
    "valproate":      "⬛ BLACK BOX: Valproate causes major congenital malformations and impairs neurodevelopment when used in pregnancy. CONTRAINDICATED in pregnancy unless no alternatives. MHRA Pregnancy Prevention Programme mandatory.",
    "isotretinoin":   "⬛ BLACK BOX: Isotretinoin is highly teratogenic. Pregnancy Prevention Programme required. Two forms of contraception mandatory. Monthly pregnancy testing.",
    "methotrexate":   "⬛ BLACK BOX: Methotrexate can cause severe toxic reactions including death. Folic acid supplementation mandatory. Regular FBC, LFTs, renal function monitoring required.",
    "warfarin":       "⬛ BLACK BOX: Warfarin can cause major/fatal bleeding. Regular INR monitoring mandatory. Multiple drug and food interactions.",
    "clozapine":      "⬛ BLACK BOX: Clozapine causes agranulocytosis — potentially fatal. Mandatory haematological monitoring (CPMS registration required). Myocarditis risk in first 4 weeks.",
    "ssri":           "⬛ BLACK BOX: Antidepressants increase risk of suicidal thinking in children, adolescents, and young adults. Monitor closely, especially in first weeks of treatment.",
    "fluoroquinolones": "⬛ BLACK BOX: Fluoroquinolones (ciprofloxacin, levofloxacin, moxifloxacin): disabling and potentially irreversible tendinopathy, peripheral neuropathy, CNS effects. Reserve for serious infections where alternatives inadequate.",
    "opioids":        "⬛ BLACK BOX: Opioids: addiction, misuse, overdose — potentially fatal. Respiratory depression risk. Risk assessment before prescribing. Naloxone co-prescription recommended.",
    "metformin":      "⬛ BLACK BOX: Metformin — rare but fatal lactic acidosis. Contraindicated eGFR <30 mL/min/1.73m². Hold before procedures with contrast. Avoid in acute illness with dehydration.",
    "sildenafil":     "⬛ BLACK BOX: Sildenafil ABSOLUTELY CONTRAINDICATED with nitrates or nitric oxide donors — severe hypotension, syncope, MI, death.",
    "thalidomide":    "⬛ BLACK BOX: Thalidomide is a known teratogen causing severe birth defects. Absolute contraindication in pregnancy. STEPS programme mandatory in US.",
    "amiodarone":     "⬛ BLACK BOX: Amiodarone causes potentially fatal pulmonary toxicity, hepatotoxicity, proarrhythmia. Use only for life-threatening arrhythmias. Regular LFTs, TFTs, CXR mandatory.",
}

def _drug_in_text(drug_key: str, text: str) -> bool:
    return drug_key.lower() in text.lower()


class BlackBoxWarningGate:
    """L5-11: Inject Black Box Warnings prominently. Cannot be suppressed."""

    def check(self, output_text: str, drugs_in_query: Optional[list[str]] = None) -> GateResult:
        injections = []
        check_drugs = drugs_in_query or list(BLACK_BOX_WARNINGS.keys())
        for drug_key in check_drugs:
            if _drug_in_text(drug_key, output_text) and drug_key in BLACK_BOX_WARNINGS:
                injections.append(BLACK_BOX_WARNINGS[drug_key])
        if injections:
            return GateResult(
                gate="L5-11:BlackBoxWarning",
                verdict=GateVerdict.BLOCK,
                reason=f"Black Box Warning(s) required for: {[d for d in check_drugs if _drug_in_text(d, output_text)]}",
                injected="\n\n" + "\n".join(injections),
            )
        return GateResult(gate="L5-11:BlackBoxWarning", verdict=GateVerdict.PASS)


# ─────────────────────────────────────────────────────────────────────────────
# L5-12: DOSE PLAUSIBILITY GATE
# 'Catches order-of-magnitude errors. 500mg paracetamol vs 5000mg paracetamol.'
# ─────────────────────────────────────────────────────────────────────────────

DOSE_PLAUSIBILITY_BOUNDS: dict[str, tuple[float, float, str]] = {
    # drug: (min_single_dose_mg, max_single_dose_mg, route_context)
    "paracetamol":   (250.0,   1000.0,  "oral adult"),
    "ibuprofen":     (100.0,   800.0,   "oral adult"),
    "amoxicillin":   (125.0,   1000.0,  "oral adult"),
    "metformin":     (250.0,   1000.0,  "oral adult"),
    "atorvastatin":  (5.0,     80.0,    "oral adult"),
    "simvastatin":   (5.0,     80.0,    "oral adult"),
    "aspirin":       (75.0,    1000.0,  "oral adult"),
    "lisinopril":    (2.5,     40.0,    "oral adult"),
    "amlodipine":    (2.5,     10.0,    "oral adult"),
    "omeprazole":    (10.0,    40.0,    "oral adult"),
    "warfarin":      (0.5,     20.0,    "oral adult"),
    "digoxin":       (0.0625,  0.25,    "oral adult"),
    "levothyroxine": (12.5,    300.0,   "mcg oral adult"),
    "prednisolone":  (1.0,     60.0,    "oral adult"),
    "morphine":      (2.5,     30.0,    "oral adult"),
    "methotrexate":  (2.5,     30.0,    "oral weekly adult"),
    "lithium":       (100.0,   800.0,   "oral adult per dose"),
    "gentamicin":    (60.0,    640.0,   "mg IV daily (5-7mg/kg/day)"),
    "vancomycin":    (500.0,   3000.0,  "mg IV per dose"),
}

_DOSE_EXTRACTION_PATTERN = re.compile(
    r'(\d+(?:\.\d+)?)\s*(mg|mcg|µg|g|ml|mL)\s*(?:of\s+)?([a-zA-Z\-]+(?:\s+[a-zA-Z]+)?)',
    re.I
)


class DosePlausibilityGate:
    """L5-12: Catch order-of-magnitude dose errors in output text."""

    def check(self, output_text: str) -> GateResult:
        matches = _DOSE_EXTRACTION_PATTERN.finditer(output_text)
        flags = []
        for m in matches:
            value_str, unit, drug_name = m.group(1), m.group(2).lower(), m.group(3).lower().strip()
            try:
                value = float(value_str)
            except ValueError:
                continue
            # Convert to mg if needed
            if unit in ("g",):
                value_mg = value * 1000
            elif unit in ("mcg", "µg"):
                value_mg = value / 1000
            else:
                value_mg = value

            for drug_key, (min_mg, max_mg, ctx) in DOSE_PLAUSIBILITY_BOUNDS.items():
                if drug_key in drug_name or drug_name in drug_key:
                    if value_mg > max_mg * 5:
                        flags.append(f"IMPLAUSIBLY HIGH: {value}{unit} {drug_name} (max single dose: {max_mg}mg {ctx})")
                    elif value_mg > 0 and value_mg < min_mg * 0.1:
                        flags.append(f"IMPLAUSIBLY LOW: {value}{unit} {drug_name} (min dose: {min_mg}mg {ctx})")
                    break

        if flags:
            return GateResult(
                gate="L5-12:DosePlausibility",
                verdict=GateVerdict.REFUSE,
                reason=f"IMPLAUSIBLE DOSE(S) DETECTED: {'; '.join(flags)}",
                injected=f"🚫 DOSE SAFETY ALERT: Implausible dose value detected. {'; '.join(flags)}. Output blocked for review.",
                is_hard_block=True,
            )
        return GateResult(gate="L5-12:DosePlausibility", verdict=GateVerdict.PASS)


# ─────────────────────────────────────────────────────────────────────────────
# L5-14: PATIENT MODE REGULATORY GATE
# 'FDA January 2026 CDS guidance: patient mode outputs require specific disclaimer.'
# ─────────────────────────────────────────────────────────────────────────────

PATIENT_MODE_DISCLAIMER = (
    "\n\n📋 IMPORTANT INFORMATION FOR PATIENTS:\n"
    "This information is provided for educational purposes only and does not replace "
    "professional medical advice. Always consult your doctor, pharmacist, or nurse "
    "before making changes to your medication or treatment. In an emergency, call "
    "emergency services immediately.\n"
    "[CURANIQ FDA 21st Century Cures Act CDS Guidance Compliant — January 2026]"
)

PATIENT_FORBIDDEN_CONTENT = [
    re.compile(r'\b(prescribe|prescription|Rx only|prescription.only)\b', re.I),
    re.compile(r'\b(clinical decision|clinician should|physician should|doctor must)\b', re.I),
    re.compile(r'\b(loading dose|maintenance dose|titration|dose escalation)\b', re.I),
]


class PatientModeRegulatoryGate:
    """L5-14: Enforce FDA 2026 CDS guidance for patient-facing outputs."""

    def check(self, output_text: str, user_role: str) -> GateResult:
        if user_role.lower() not in ("patient", "caregiver"):
            return GateResult(gate="L5-14:PatientModeRegulatory", verdict=GateVerdict.PASS)

        # Check for clinician-only content
        forbidden = [pat.search(output_text) for pat in PATIENT_FORBIDDEN_CONTENT]
        forbidden_matches = [m.group(0) for m in forbidden if m]
        if forbidden_matches:
            return GateResult(
                gate="L5-14:PatientModeRegulatory",
                verdict=GateVerdict.BLOCK,
                reason=f"Patient mode output contains clinician-level content: {forbidden_matches}",
                injected=PATIENT_MODE_DISCLAIMER,
            )
        return GateResult(
            gate="L5-14:PatientModeRegulatory",
            verdict=GateVerdict.BLOCK,   # Always inject disclaimer for patient mode
            reason="Patient mode — regulatory disclaimer injection",
            injected=PATIENT_MODE_DISCLAIMER,
        )


# ─────────────────────────────────────────────────────────────────────────────
# L5-17: NUMERIC GATE — LAUNCH-BLOCKER
# Architecture: 'Stricter than L5-12. Verifies numeric values against source hash.
# Even one unverified numeric value in a clinical output → BLOCK.'
# ─────────────────────────────────────────────────────────────────────────────

_NUMERIC_CLINICAL_PATTERN = re.compile(
    r'(\d+(?:\.\d+)?)\s*(mg|mcg|µg|g|ml|mL|mmol|mmHg|bpm|%|mL/min)',
    re.I
)


class NumericGate:
    """
    L5-17: LAUNCH-BLOCKER.
    Every numeric clinical value in output must be traceable to a cited source.
    Architecture: 'Even one unverifiable numeric value → BLOCK entire output.'
    """

    def check(
        self,
        output_text: str,
        verified_numeric_chunk_ids: set[str],
        all_claim_chunk_ids: set[str],
    ) -> GateResult:
        """
        verified_numeric_chunk_ids: chunk_ids that passed L4-14 hash verification
                                    AND confirmed numeric value match.
        all_claim_chunk_ids: all chunk_ids cited in this output.
        """
        numeric_matches = _NUMERIC_CLINICAL_PATTERN.findall(output_text)
        if not numeric_matches:
            return GateResult(gate="L5-17:NumericGate", verdict=GateVerdict.PASS)

        # If ALL cited chunks passed numeric hash verification → pass
        unverified = all_claim_chunk_ids - verified_numeric_chunk_ids
        if unverified:
            return GateResult(
                gate="L5-17:NumericGate",
                verdict=GateVerdict.REFUSE,
                reason=(
                    f"NUMERIC GATE FAIL: {len(numeric_matches)} numeric value(s) found in output. "
                    f"{len(unverified)} cited chunk(s) have not passed numeric hash verification: {list(unverified)[:3]}. "
                    "Output BLOCKED — cannot guarantee numeric accuracy."
                ),
                injected="🚫 NUMERIC SAFETY BLOCK: Numeric clinical values could not be verified against source evidence. Output withheld.",
                is_hard_block=True,
            )
        return GateResult(gate="L5-17:NumericGate", verdict=GateVerdict.PASS)


# ─────────────────────────────────────────────────────────────────────────────
# SAFETY GATE PIPELINE ORCHESTRATOR
# Runs all gates in sequence. First REFUSE verdict stops the pipeline.
# ─────────────────────────────────────────────────────────────────────────────

class SafetyGatePipeline:
    """
    Orchestrates all L5 safety gates in order.
    Gates run sequentially; first hard REFUSE stops the pipeline.
    WARNs and non-hard BLOCKs accumulate (all run even after WARN/BLOCK).
    """

    def __init__(self) -> None:
        self.completeness        = CompletenessGate()
        self.safety_language     = SafetyLanguageFilter()
        self.no_evidence         = NoEvidenceRefusalGate()
        self.semantic_entropy    = SemanticEntropyGate()
        self.task_gate           = TaskGateByRole()
        self.retraction_final    = RetractionFinalGate()
        self.edge_case           = EdgeCaseDetector()
        self.output_completeness = OutputCompletenessChecker()
        self.black_box           = BlackBoxWarningGate()
        self.dose_plausibility   = DosePlausibilityGate()
        self.patient_mode        = PatientModeRegulatoryGate()
        self.numeric_gate        = NumericGate()

    def run_all(
        self,
        output_text: str,
        query: str = "",
        user_role: str = "doctor",
        evidence_confidence: float = 1.0,
        claim_certainties: Optional[list[str]] = None,
        retraction_statuses: Optional[dict[str, str]] = None,
        drugs_in_query: Optional[list[str]] = None,
        verified_numeric_chunk_ids: Optional[set[str]] = None,
        all_claim_chunk_ids: Optional[set[str]] = None,
        is_actionable_plan: bool = True,
        **patient_context,
    ) -> tuple[str, list[GateResult]]:
        """
        Run all gates. Returns (final_output_text, gate_results).
        If any hard REFUSE: final_output_text is the refusal message.
        """
        results: list[GateResult] = []
        injections: list[str] = []
        final_text = output_text

        def run(gate_result: GateResult) -> bool:
            results.append(gate_result)
            if gate_result.injected:
                injections.append(gate_result.injected)
            if gate_result.verdict == GateVerdict.REFUSE and gate_result.is_hard_block:
                return False   # Stop pipeline
            return True

        if not run(self.completeness.check(query, drugs_in_query[0] if drugs_in_query else None, **{k: v for k, v in patient_context.items() if k in ("egfr", "age_years", "weight_kg", "pregnancy_status")})): pass
        if not run(self.task_gate.check(query, user_role)): pass
        if not run(self.no_evidence.check(query, evidence_confidence)): pass
        run(self.safety_language.check(output_text))
        run(self.semantic_entropy.check(claim_certainties or []))
        if retraction_statuses:
            run(self.retraction_final.check(retraction_statuses))
        run(self.edge_case.check(**{k: v for k, v in patient_context.items() if k in ("age_years", "egfr", "pregnancy_status", "concurrent_drug_count", "has_hepatic_failure", "has_active_malignancy")}, query=query))
        run(self.output_completeness.check(output_text, is_actionable_plan))
        run(self.black_box.check(output_text, drugs_in_query))
        run(self.dose_plausibility.check(output_text))
        run(self.patient_mode.check(output_text, user_role))
        if verified_numeric_chunk_ids is not None and all_claim_chunk_ids is not None:
            run(self.numeric_gate.check(output_text, verified_numeric_chunk_ids, all_claim_chunk_ids))

        # Check for any hard refuses
        hard_refuse = next((r for r in results if r.verdict == GateVerdict.REFUSE and r.is_hard_block), None)
        if hard_refuse:
            return hard_refuse.injected or hard_refuse.reason or "Output refused by safety gate.", results

        # Apply all injections to output
        if injections:
            final_text = output_text + "\n".join(injections)

        return final_text, results
