"""
CURANIQ — L5-13: Deterministic Triage Gate (Pre-LLM)
Architecture spec: Hard rule-based classifier firing BEFORE any LLM processing.
Detects life-threatening emergencies; on trigger, pipeline HALTS.
LLMs cannot be trusted with primary triage.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from curaniq.models.schemas import PatientContext, TriageAssessment, TriageResult


# ─────────────────────────────────────────────────────────────────────────────
# TRIAGE CRITERIA  (START / JumpSTART protocol + CURANIQ additions)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TriageCriterion:
    criterion_id: str
    description: str
    text_patterns: list[re.Pattern]
    result: TriageResult
    escalation_message: str
    vital_sign_check: Optional[callable] = field(default=None, repr=False)


# Pre-compile all patterns for performance
_PATTERNS: dict[str, list[re.Pattern]] = {

    "altered_mental_status": [
        re.compile(r"\b(confusion|confused|disoriented|unresponsive|unconscious|"
                   r"altered\s+mental|ams|gcs\s*[<≤]\s*1[0-4]|not\s+responding|"
                   r"lethargy|lethargic|coma|obtunded|stupor)\b", re.I),
    ],

    "respiratory_failure": [
        re.compile(r"\b(spo2|o2\s+sat|oxygen\s+sat)[^\d]*([0-8]\d|90)\s*%", re.I),
        re.compile(r"\b(can'?t\s+breathe|cannot\s+breathe|not\s+breathing|"
                   r"respiratory\s+arrest|apnea|apnoea|stridor|severe\s+dyspnea|"
                   r"gasping)\b", re.I),
    ],

    "hypotension": [
        re.compile(r"\bsbp\s*[<≤]\s*90\b", re.I),
        re.compile(r"\bblood\s+pressure\s*[<≤]\s*90\b", re.I),
        re.compile(r"\b(hypotensive|shock|cardiovascular\s+collapse)\b", re.I),
    ],

    "cardiac_emergency": [
        re.compile(r"\b(chest\s+pain|chest\s+tightness)\b.{0,80}\b(diaphoresis|"
                   r"sweating|radiation|arm|jaw|stemi|nstemi)\b", re.I),
        re.compile(r"\b(stemi|heart\s+attack|myocardial\s+infarction|cardiac\s+arrest|"
                   r"v\s*fib|ventricular\s+fibrillation|pulseless)\b", re.I),
    ],

    "stroke": [
        re.compile(r"\b(fast\s+test|facial\s+droop|arm\s+weakness|speech\s+difficulty|"
                   r"sudden\s+weakness|stroke|tia|sudden\s+numbness|"
                   r"sudden\s+vision\s+loss|worst\s+headache)\b", re.I),
    ],

    "anaphylaxis": [
        re.compile(r"\b(anaphylaxis|anaphylactic|throat\s+closing|throat\s+swelling|"
                   r"tongue\s+swelling|angioedema.{0,30}airway|epinephrine\s+needed|"
                   r"epipen|adrenaline\s+needed)\b", re.I),
    ],

    "active_hemorrhage": [
        re.compile(r"\b(massive\s+bleeding|arterial\s+bleed|hemorrhagic\s+shock|"
                   r"exsanguinating|uncontrolled\s+bleed|gi\s+bleed.{0,40}hemodynamic)\b", re.I),
    ],

    "severe_sepsis": [
        re.compile(r"\b(septic\s+shock|severe\s+sepsis|quick\s+sofa|qsofa\s*[≥>]\s*2|"
                   r"lactate\s*[≥>]\s*2|organ\s+failure.{0,30}infection)\b", re.I),
    ],

    "overdose_imminent": [
        re.compile(r"\b(overdose|ingested.{0,40}(lethal|toxic|massive)|"
                   r"took\s+too\s+many|intentional\s+ingestion|suicide\s+attempt.{0,30}meds)\b", re.I),
    ],
}

TRIAGE_CRITERIA: list[TriageCriterion] = [
    TriageCriterion(
        criterion_id="AMS",
        description="Altered mental status",
        text_patterns=_PATTERNS["altered_mental_status"],
        result=TriageResult.EMERGENCY,
        escalation_message=(
            "⚠️ EMERGENCY: Signs of altered mental status detected. "
            "Call emergency services (103/112) immediately. "
            "Do NOT wait for AI advice — this requires immediate human assessment. "
            "Position patient safely; monitor airway, breathing, circulation."
        ),
    ),
    TriageCriterion(
        criterion_id="RESP_FAIL",
        description="SpO2 <90% or severe respiratory distress",
        text_patterns=_PATTERNS["respiratory_failure"],
        result=TriageResult.EMERGENCY,
        escalation_message=(
            "⚠️ EMERGENCY: Severe respiratory distress or hypoxia detected. "
            "Call emergency services immediately. "
            "Apply supplemental oxygen if available. "
            "Prepare for airway management — do NOT await AI analysis."
        ),
    ),
    TriageCriterion(
        criterion_id="HYPOTENSION",
        description="SBP <90 mmHg / shock",
        text_patterns=_PATTERNS["hypotension"],
        result=TriageResult.EMERGENCY,
        escalation_message=(
            "⚠️ EMERGENCY: Hypotension / shock indicators detected. "
            "Call emergency services immediately. "
            "Lay patient flat, elevate legs if no respiratory compromise. "
            "Establish IV access; do NOT delay for AI consultation."
        ),
    ),
    TriageCriterion(
        criterion_id="CARDIAC",
        description="Suspected acute coronary syndrome or cardiac arrest",
        text_patterns=_PATTERNS["cardiac_emergency"],
        result=TriageResult.EMERGENCY,
        escalation_message=(
            "⚠️ EMERGENCY: Possible acute coronary event or cardiac arrest. "
            "Call emergency services (103/112) immediately. "
            "Initiate CPR if pulseless. Administer aspirin 300mg if STEMI suspected "
            "and not contraindicated — do NOT await AI confirmation for life-saving steps."
        ),
    ),
    TriageCriterion(
        criterion_id="STROKE",
        description="FAST criteria / acute neurological deficit",
        text_patterns=_PATTERNS["stroke"],
        result=TriageResult.EMERGENCY,
        escalation_message=(
            "⚠️ EMERGENCY: Stroke indicators (FAST criteria) detected. "
            "Time is brain — call emergency services immediately. "
            "Note exact time of symptom onset. "
            "Do NOT give aspirin until hemorrhagic stroke is excluded by CT."
        ),
    ),
    TriageCriterion(
        criterion_id="ANAPHYLAXIS",
        description="Anaphylaxis with airway compromise",
        text_patterns=_PATTERNS["anaphylaxis"],
        result=TriageResult.EMERGENCY,
        escalation_message=(
            "⚠️ EMERGENCY: Anaphylaxis with potential airway involvement. "
            "Administer epinephrine 0.5mg IM (anterolateral thigh) immediately. "
            "Call emergency services. Position supine (unless respiratory distress). "
            "This is the first-line treatment — do NOT delay for AI advice."
        ),
    ),
    TriageCriterion(
        criterion_id="HEMORRHAGE",
        description="Active life-threatening hemorrhage",
        text_patterns=_PATTERNS["active_hemorrhage"],
        result=TriageResult.EMERGENCY,
        escalation_message=(
            "⚠️ EMERGENCY: Active hemorrhage with hemodynamic instability indicated. "
            "Call emergency services immediately. Apply direct pressure to bleeding site. "
            "Do NOT remove impaled objects. Establish large-bore IV access if trained."
        ),
    ),
    TriageCriterion(
        criterion_id="SEPTIC_SHOCK",
        description="Septic shock / severe sepsis with organ failure",
        text_patterns=_PATTERNS["severe_sepsis"],
        result=TriageResult.EMERGENCY,
        escalation_message=(
            "⚠️ EMERGENCY: Severe sepsis / septic shock indicators. "
            "Initiate sepsis bundle immediately (Surviving Sepsis Campaign). "
            "Blood cultures → IV antibiotics within 1 hour of recognition. "
            "IV fluid resuscitation 30 mL/kg crystalloid. Call emergency services."
        ),
    ),
    TriageCriterion(
        criterion_id="OVERDOSE",
        description="Suspected lethal overdose / intentional ingestion",
        text_patterns=_PATTERNS["overdose_imminent"],
        result=TriageResult.EMERGENCY,
        escalation_message=(
            "⚠️ EMERGENCY: Suspected overdose or intentional ingestion. "
            "Call emergency services and Poison Control (Uzbekistan: +998 71 244-35-60) immediately. "
            "Do NOT induce vomiting unless specifically directed by Poison Control. "
            "Keep patient awake and monitor breathing."
        ),
    ),
]


# ─────────────────────────────────────────────────────────────────────────────
# VITAL SIGN EXTRACTOR  (for structured patient context checks)
# ─────────────────────────────────────────────────────────────────────────────

def _check_vitals_from_context(ctx: Optional[PatientContext]) -> list[tuple[str, str]]:
    """
    Checks structured PatientContext for emergency vital sign values.
    Returns list of (criterion_id, message) for any triggered criteria.
    """
    if not ctx:
        return []
    triggered = []

    # SpO2 < 90 from renal data proxy (if future context has vitals)
    # For now: check for dialysis context (already captured in TriageCriteria)
    if ctx.renal and ctx.renal.on_dialysis and ctx.is_pregnant:
        triggered.append((
            "HIGH_RISK_RENAL_PREG",
            "High-risk context: pregnant patient on dialysis — activate specialist consultation immediately."
        ))

    return triggered


# ─────────────────────────────────────────────────────────────────────────────
# TRIAGE GATE  (L5-13 public interface)
# ─────────────────────────────────────────────────────────────────────────────

class TriageGate:
    """
    L5-13: Deterministic Triage Gate.

    Fires BEFORE any LLM call, any retrieval, any processing.
    Pattern matches query text against life-threatening emergency criteria.
    On EMERGENCY trigger → pipeline HALTS and returns pre-scripted escalation.
    No LLM inference occurs for EMERGENCY cases.
    """

    def __init__(self) -> None:
        self._criteria = TRIAGE_CRITERIA

    def assess(
        self,
        query_text: str,
        patient_context: Optional[PatientContext] = None,
    ) -> TriageAssessment:
        """
        Assess a clinical query for emergency triage criteria.
        Returns TriageAssessment — EMERGENCY causes full pipeline halt.
        """
        triggered_criteria: list[str] = []
        escalation_message: Optional[str] = None
        highest_result = TriageResult.CLEAR

        clean_text = query_text.strip()

        for criterion in self._criteria:
            matched = False
            for pattern in criterion.text_patterns:
                if pattern.search(clean_text):
                    matched = True
                    break

            if matched:
                triggered_criteria.append(criterion.description)
                if criterion.result == TriageResult.EMERGENCY:
                    # First emergency match sets the message
                    if escalation_message is None:
                        escalation_message = criterion.escalation_message
                    highest_result = TriageResult.EMERGENCY
                elif (
                    criterion.result == TriageResult.URGENT
                    and highest_result == TriageResult.CLEAR
                ):
                    highest_result = TriageResult.URGENT

        # Check structured patient vitals
        vital_flags = _check_vitals_from_context(patient_context)
        for flag_id, flag_msg in vital_flags:
            triggered_criteria.append(flag_id)
            if escalation_message is None:
                escalation_message = flag_msg

        return TriageAssessment(
            result=highest_result,
            triggered_criteria=triggered_criteria,
            escalation_message=escalation_message,
            assessed_at=datetime.now(timezone.utc),
        )

    def is_clear(self, query_text: str, patient_context: Optional[PatientContext] = None) -> bool:
        """Convenience method — True if safe to proceed to pipeline."""
        return self.assess(query_text, patient_context).result == TriageResult.CLEAR
