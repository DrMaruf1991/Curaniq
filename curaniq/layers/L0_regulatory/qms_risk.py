"""
CURANIQ — Medical Evidence Operating System
Layer 0: Quality & Regulatory Foundation

L0-1  Quality Management System (ISO 13485-aligned)
L0-2  Risk Management Framework (ISO 14971)
L0-5  Validation Programme Design (staged evaluation)

Architecture: "Feature 0 — everything depends on this layer."

These modules enforce quality and safety controls programmatically.
Every pipeline execution is tracked, every change is versioned,
every risk is scored and mitigated.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# L0-1: QUALITY MANAGEMENT SYSTEM (ISO 13485)
# ─────────────────────────────────────────────────────────────────────────────

class DesignPhase(str, Enum):
    """ISO 13485 Design Control phases."""
    PLANNING        = "design_planning"
    INPUT           = "design_input"
    OUTPUT          = "design_output"
    REVIEW          = "design_review"
    VERIFICATION    = "design_verification"
    VALIDATION      = "design_validation"
    TRANSFER        = "design_transfer"
    CHANGE_CONTROL  = "design_change"


class ChangeCategory(str, Enum):
    """Change types requiring different approval levels."""
    COSMETIC        = "cosmetic"          # UI text, typos → auto-approve
    MINOR           = "minor"             # Non-safety logic → team lead
    SIGNIFICANT     = "significant"       # Safety-adjacent → clinical review
    SAFETY_CRITICAL = "safety_critical"   # CQL rules, gates → full board


@dataclass
class DesignControlRecord:
    """Tracks a design change through the ISO 13485 lifecycle."""
    record_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    phase: DesignPhase = DesignPhase.PLANNING
    category: ChangeCategory = ChangeCategory.MINOR
    description: str = ""
    module_affected: str = ""
    risk_assessment_id: Optional[str] = None
    approved_by: Optional[str] = None
    approval_timestamp: Optional[datetime] = None
    verification_passed: bool = False
    validation_passed: bool = False
    change_hash: str = ""


class QualityManagementSystem:
    """
    L0-1: Enforces ISO 13485 design controls on all CURANIQ changes.

    Every module change must:
    1. Be categorized (cosmetic → safety_critical)
    2. Get risk-assessed (L0-2)
    3. Pass verification (unit/integration tests)
    4. Pass validation (clinical accuracy tests)
    5. Be approved at the correct authority level
    """

    APPROVAL_LEVELS: dict[ChangeCategory, list[str]] = {
        ChangeCategory.COSMETIC:        ["auto"],
        ChangeCategory.MINOR:           ["team_lead"],
        ChangeCategory.SIGNIFICANT:     ["clinical_reviewer", "team_lead"],
        ChangeCategory.SAFETY_CRITICAL: ["clinical_director", "clinical_reviewer", "team_lead"],
    }

    def __init__(self):
        self._records: list[DesignControlRecord] = []
        self._module_versions: dict[str, str] = {}

    def register_change(
        self,
        module: str,
        description: str,
        category: ChangeCategory,
        code_diff_hash: str,
    ) -> DesignControlRecord:
        """Register a design change and create a control record."""
        record = DesignControlRecord(
            module_affected=module,
            description=description,
            category=category,
            change_hash=code_diff_hash,
        )

        # Safety-critical changes require risk assessment before proceeding
        if category == ChangeCategory.SAFETY_CRITICAL:
            record.phase = DesignPhase.INPUT
            logger.warning(
                "SAFETY_CRITICAL change registered for %s — "
                "requires risk assessment before implementation: %s",
                module, description,
            )
        else:
            record.phase = DesignPhase.OUTPUT

        self._records.append(record)
        return record

    def verify_change(self, record_id: str, test_results: dict[str, bool]) -> bool:
        """Mark change as verified if all tests pass."""
        record = self._find_record(record_id)
        if not record:
            return False

        all_passed = all(test_results.values())
        record.verification_passed = all_passed
        record.phase = DesignPhase.VERIFICATION

        if not all_passed:
            failed = [k for k, v in test_results.items() if not v]
            logger.error(
                "Verification FAILED for %s: %s", record.module_affected, failed
            )
        return all_passed

    def validate_change(self, record_id: str, clinical_accuracy: float) -> bool:
        """Mark change as clinically validated."""
        record = self._find_record(record_id)
        if not record:
            return False

        # Minimum clinical accuracy thresholds by category
        thresholds = {
            ChangeCategory.COSMETIC: 0.0,
            ChangeCategory.MINOR: 0.90,
            ChangeCategory.SIGNIFICANT: 0.95,
            ChangeCategory.SAFETY_CRITICAL: 0.99,
        }

        threshold = thresholds[record.category]
        record.validation_passed = clinical_accuracy >= threshold
        record.phase = DesignPhase.VALIDATION

        if not record.validation_passed:
            logger.error(
                "Validation FAILED for %s: accuracy %.3f < threshold %.3f",
                record.module_affected, clinical_accuracy, threshold,
            )
        return record.validation_passed

    def approve_change(self, record_id: str, approver_role: str) -> bool:
        """Approve change if approver has sufficient authority."""
        record = self._find_record(record_id)
        if not record:
            return False

        required = self.APPROVAL_LEVELS[record.category]
        if approver_role not in required and "auto" not in required:
            logger.warning(
                "Approval DENIED: %s cannot approve %s changes (requires %s)",
                approver_role, record.category.value, required,
            )
            return False

        if not record.verification_passed:
            logger.error("Cannot approve unverified change: %s", record_id)
            return False

        if record.category in (ChangeCategory.SIGNIFICANT, ChangeCategory.SAFETY_CRITICAL):
            if not record.validation_passed:
                logger.error("Cannot approve unvalidated safety change: %s", record_id)
                return False

        record.approved_by = approver_role
        record.approval_timestamp = datetime.now(timezone.utc)
        record.phase = DesignPhase.TRANSFER
        return True

    def compute_module_hash(self, filepath: str) -> str:
        """Compute SHA-256 of a module file for change tracking."""
        try:
            with open(filepath, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()[:16]
        except FileNotFoundError:
            return "NEW_MODULE"

    def _find_record(self, record_id: str) -> Optional[DesignControlRecord]:
        return next((r for r in self._records if r.record_id == record_id), None)


# ─────────────────────────────────────────────────────────────────────────────
# L0-2: RISK MANAGEMENT FRAMEWORK (ISO 14971)
# ─────────────────────────────────────────────────────────────────────────────

class SeverityLevel(str, Enum):
    NEGLIGIBLE  = "negligible"     # Score 1
    MINOR       = "minor"          # Score 2
    SERIOUS     = "serious"        # Score 3
    CRITICAL    = "critical"       # Score 4
    CATASTROPHIC = "catastrophic"  # Score 5


class ProbabilityLevel(str, Enum):
    INCREDIBLE  = "incredible"     # Score 1
    IMPROBABLE  = "improbable"     # Score 2
    REMOTE      = "remote"         # Score 3
    OCCASIONAL  = "occasional"     # Score 4
    FREQUENT    = "frequent"       # Score 5


class RiskAcceptability(str, Enum):
    ACCEPTABLE  = "acceptable"         # Risk score 1-4
    ALARP       = "alarp"              # As Low As Reasonably Practicable 5-9
    UNACCEPTABLE = "unacceptable"      # Risk score 10+


@dataclass
class RiskAssessment:
    """ISO 14971 risk assessment for a hazardous situation."""
    risk_id: str = field(default_factory=lambda: f"RISK-{uuid4().hex[:8].upper()}")
    hazard: str = ""
    hazardous_situation: str = ""
    harm: str = ""
    severity: SeverityLevel = SeverityLevel.MINOR
    probability: ProbabilityLevel = ProbabilityLevel.REMOTE
    risk_score: int = 0
    acceptability: RiskAcceptability = RiskAcceptability.ACCEPTABLE
    mitigation_modules: list[str] = field(default_factory=list)
    residual_severity: Optional[SeverityLevel] = None
    residual_probability: Optional[ProbabilityLevel] = None
    residual_score: int = 0
    verified: bool = False


class RiskManagementFramework:
    """
    L0-2: ISO 14971 risk management for CURANIQ.

    Bounded-risk output classification:
    - Severity × Probability = Risk Score
    - Unacceptable risks MUST be mitigated before release
    - ALARP risks require documented justification
    """

    _SEVERITY_SCORES = {
        SeverityLevel.NEGLIGIBLE: 1,
        SeverityLevel.MINOR: 2,
        SeverityLevel.SERIOUS: 3,
        SeverityLevel.CRITICAL: 4,
        SeverityLevel.CATASTROPHIC: 5,
    }

    _PROBABILITY_SCORES = {
        ProbabilityLevel.INCREDIBLE: 1,
        ProbabilityLevel.IMPROBABLE: 2,
        ProbabilityLevel.REMOTE: 3,
        ProbabilityLevel.OCCASIONAL: 4,
        ProbabilityLevel.FREQUENT: 5,
    }

    # Pre-defined risk register for CURANIQ top failure modes
    KNOWN_HAZARDS: list[dict] = [
        {
            "hazard": "Hallucinated clinical claim",
            "harm": "Direct patient harm from incorrect treatment",
            "severity": SeverityLevel.CATASTROPHIC,
            "probability": ProbabilityLevel.OCCASIONAL,
            "mitigation": ["L4-3", "L4-4", "L4-12", "L5-3"],
        },
        {
            "hazard": "Wrong dose calculation",
            "harm": "Overdose or subtherapeutic dosing",
            "severity": SeverityLevel.CATASTROPHIC,
            "probability": ProbabilityLevel.REMOTE,
            "mitigation": ["L3-1", "L5-12", "L5-17"],
        },
        {
            "hazard": "Missed drug-drug interaction",
            "harm": "Adverse drug reaction, potential death",
            "severity": SeverityLevel.CRITICAL,
            "probability": ProbabilityLevel.REMOTE,
            "mitigation": ["L3-1", "L3-2", "L5-11"],
        },
        {
            "hazard": "Retracted evidence cited",
            "harm": "Clinical decision based on invalidated evidence",
            "severity": SeverityLevel.SERIOUS,
            "probability": ProbabilityLevel.REMOTE,
            "mitigation": ["L2-7", "L5-7"],
        },
        {
            "hazard": "Prompt injection",
            "harm": "EHR data exfiltration or manipulated output",
            "severity": SeverityLevel.CRITICAL,
            "probability": ProbabilityLevel.OCCASIONAL,
            "mitigation": ["L6-1", "L6-2"],
        },
        {
            "hazard": "Patient receives dosing information",
            "harm": "Self-medication without professional oversight",
            "severity": SeverityLevel.SERIOUS,
            "probability": ProbabilityLevel.OCCASIONAL,
            "mitigation": ["L5-14", "L8-4"],
        },
        {
            "hazard": "Translation negation error",
            "harm": "'Do NOT take' becomes 'Do take' in translation",
            "severity": SeverityLevel.CATASTROPHIC,
            "probability": ProbabilityLevel.REMOTE,
            "mitigation": ["L8-5", "L8-12"],
        },
        {
            "hazard": "Stale drug safety alert",
            "harm": "Newly contraindicated drug still recommended",
            "severity": SeverityLevel.CRITICAL,
            "probability": ProbabilityLevel.IMPROBABLE,
            "mitigation": ["L1-4", "L1-5", "L1-16"],
        },
        {
            "hazard": "Edge-case patient not detected",
            "harm": "Standard evidence applied to non-standard patient",
            "severity": SeverityLevel.SERIOUS,
            "probability": ProbabilityLevel.OCCASIONAL,
            "mitigation": ["L5-9"],
        },
        {
            "hazard": "PHI leak to LLM provider",
            "harm": "HIPAA/GDPR violation, patient privacy breach",
            "severity": SeverityLevel.CRITICAL,
            "probability": ProbabilityLevel.REMOTE,
            "mitigation": ["L6-2"],
        },
    ]

    def __init__(self):
        self._assessments: list[RiskAssessment] = []
        self._initialize_known_risks()

    def _initialize_known_risks(self):
        """Load the pre-defined risk register."""
        for hazard_def in self.KNOWN_HAZARDS:
            assessment = self.assess_risk(
                hazard=hazard_def["hazard"],
                hazardous_situation=f"CURANIQ generates output related to: {hazard_def['hazard'].lower()}",
                harm=hazard_def["harm"],
                severity=hazard_def["severity"],
                probability=hazard_def["probability"],
                mitigation_modules=hazard_def["mitigation"],
            )
            # With mitigation, probability drops by at least 2 levels
            self._apply_mitigation(assessment)

    def assess_risk(
        self,
        hazard: str,
        hazardous_situation: str,
        harm: str,
        severity: SeverityLevel,
        probability: ProbabilityLevel,
        mitigation_modules: Optional[list[str]] = None,
    ) -> RiskAssessment:
        """Create a new risk assessment with ISO 14971 scoring."""
        sev_score = self._SEVERITY_SCORES[severity]
        prob_score = self._PROBABILITY_SCORES[probability]
        risk_score = sev_score * prob_score

        if risk_score <= 4:
            acceptability = RiskAcceptability.ACCEPTABLE
        elif risk_score <= 9:
            acceptability = RiskAcceptability.ALARP
        else:
            acceptability = RiskAcceptability.UNACCEPTABLE

        assessment = RiskAssessment(
            hazard=hazard,
            hazardous_situation=hazardous_situation,
            harm=harm,
            severity=severity,
            probability=probability,
            risk_score=risk_score,
            acceptability=acceptability,
            mitigation_modules=mitigation_modules or [],
        )
        self._assessments.append(assessment)
        return assessment

    def _apply_mitigation(self, assessment: RiskAssessment):
        """Estimate residual risk after mitigation modules are active."""
        if not assessment.mitigation_modules:
            return

        # Each mitigation module reduces probability by approximately 1 level
        mitigation_power = min(len(assessment.mitigation_modules), 3)
        prob_score = self._PROBABILITY_SCORES[assessment.probability]
        residual_prob_score = max(1, prob_score - mitigation_power)

        prob_levels = list(self._PROBABILITY_SCORES.items())
        assessment.residual_probability = next(
            level for level, score in prob_levels if score == residual_prob_score
        )
        assessment.residual_severity = assessment.severity
        sev_score = self._SEVERITY_SCORES[assessment.severity]
        assessment.residual_score = sev_score * residual_prob_score

    def get_unacceptable_risks(self) -> list[RiskAssessment]:
        """Return all risks that remain unacceptable after mitigation."""
        return [
            a for a in self._assessments
            if a.residual_score and a.residual_score > 9
        ]

    def get_risk_matrix(self) -> dict[str, Any]:
        """Generate a summary risk matrix for regulatory submission."""
        return {
            "total_risks": len(self._assessments),
            "unacceptable_pre_mitigation": sum(
                1 for a in self._assessments
                if a.acceptability == RiskAcceptability.UNACCEPTABLE
            ),
            "unacceptable_post_mitigation": len(self.get_unacceptable_risks()),
            "risk_register": [
                {
                    "id": a.risk_id,
                    "hazard": a.hazard,
                    "pre_score": a.risk_score,
                    "post_score": a.residual_score,
                    "mitigations": a.mitigation_modules,
                }
                for a in self._assessments
            ],
        }


# ─────────────────────────────────────────────────────────────────────────────
# L0-5: VALIDATION PROGRAMME DESIGN
# ─────────────────────────────────────────────────────────────────────────────

class ValidationStage(str, Enum):
    OFFLINE_ANALYTICAL = "stage_1_offline"
    SHADOW_PILOT       = "stage_2_shadow"
    DECIDE_AI          = "stage_3_decide_ai"
    PROSPECTIVE_TRIAL  = "stage_4_prospective"


@dataclass
class ValidationTestCase:
    """A single test case in the validation programme."""
    test_id: str
    stage: ValidationStage
    domain: str
    input_query: str
    expected_output_contains: list[str]
    expected_output_excludes: list[str] = field(default_factory=list)
    expected_safety_flags: list[str] = field(default_factory=list)
    gold_standard_source: str = ""
    passed: Optional[bool] = None
    actual_output: Optional[str] = None
    failure_reason: Optional[str] = None


class ValidationProgramme:
    """
    L0-5: Staged evaluation design for CURANIQ.

    Stage 1 — Analytical (Offline): Retrieval correctness, citation accuracy,
              medication safety rules, adversarial robustness.
    Stage 2 — Shadow Pilot: Silent predictions vs human clinician decisions.
    Stage 3 — DECIDE-AI: Clinical evaluation with human factors.
    Stage 4 — Prospective Trials: SPIRIT-AI, CONSORT-AI, TRIPOD-LLM.
    """

    def __init__(self):
        self._test_cases: list[ValidationTestCase] = []
        self._results: dict[str, list[bool]] = {}

    def register_test_case(self, test_case: ValidationTestCase):
        """Add a test case to the validation programme."""
        self._test_cases.append(test_case)

    def run_offline_test(
        self,
        test_id: str,
        actual_output: str,
        actual_safety_flags: list[str],
    ) -> bool:
        """Execute a Stage 1 offline analytical test."""
        test = next((t for t in self._test_cases if t.test_id == test_id), None)
        if not test:
            return False

        test.actual_output = actual_output
        output_lower = actual_output.lower()

        # Check required content present
        contains_pass = all(
            term.lower() in output_lower
            for term in test.expected_output_contains
        )

        # Check excluded content absent
        excludes_pass = all(
            term.lower() not in output_lower
            for term in test.expected_output_excludes
        )

        # Check safety flags
        flags_pass = all(
            flag in actual_safety_flags
            for flag in test.expected_safety_flags
        )

        test.passed = contains_pass and excludes_pass and flags_pass
        if not test.passed:
            reasons = []
            if not contains_pass:
                reasons.append("missing expected content")
            if not excludes_pass:
                reasons.append("contains excluded content")
            if not flags_pass:
                reasons.append("missing safety flags")
            test.failure_reason = "; ".join(reasons)

        return test.passed

    def get_stage_report(self, stage: ValidationStage) -> dict[str, Any]:
        """Generate pass/fail report for a validation stage."""
        stage_tests = [t for t in self._test_cases if t.stage == stage]
        executed = [t for t in stage_tests if t.passed is not None]
        passed = [t for t in executed if t.passed]

        return {
            "stage": stage.value,
            "total_tests": len(stage_tests),
            "executed": len(executed),
            "passed": len(passed),
            "failed": len(executed) - len(passed),
            "pass_rate": len(passed) / len(executed) if executed else 0.0,
            "failures": [
                {"test_id": t.test_id, "reason": t.failure_reason}
                for t in executed if not t.passed
            ],
        }
