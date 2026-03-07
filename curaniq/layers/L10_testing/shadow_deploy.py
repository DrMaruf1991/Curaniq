"""
CURANIQ — Medical Evidence Operating System
L10-1: Shadow Deployment Mode

Architecture spec:
  'Silent predictions on live patient data. Validates against clinician
  decisions. DECIDE-AI evaluation.'

Shadow mode is the CRITICAL validation step before any clinical pilot:
  - CURANIQ processes real patient data in the background
  - Generates recommendations but NEVER shows them to clinicians
  - Compares CURANIQ recommendations vs actual clinician decisions
  - Measures agreement rate, safety catch rate, false alarm rate
  - Produces DECIDE-AI evaluation metrics for regulatory submission

Metrics tracked:
  1. Agreement Rate: % where CURANIQ recommendation matches clinician action
  2. Safety Catch Rate: % of safety issues CURANIQ detected that clinicians missed
  3. False Alarm Rate: % of CURANIQ alerts that clinicians correctly ignored
  4. Response Time: latency of CURANIQ vs clinical decision time
  5. Confidence Calibration: do confidence scores predict actual correctness?
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# SHADOW SESSION — one patient encounter
# ─────────────────────────────────────────────────────────────────

class ComparisonOutcome(str, Enum):
    """Result of comparing CURANIQ vs clinician decision."""
    AGREE = "agree"                     # Both chose same action
    CURANIQ_SAFER = "curaniq_safer"     # CURANIQ caught safety issue clinician missed
    CLINICIAN_OVERRIDE = "clinician_override"  # Clinician chose different, likely valid
    CURANIQ_FALSE_ALARM = "false_alarm"        # CURANIQ flagged unnecessarily
    INCONCLUSIVE = "inconclusive"       # Can't determine who was right


@dataclass
class ShadowPrediction:
    """CURANIQ's silent prediction for one clinical question."""
    prediction_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    patient_id_hash: str = ""          # SHA-256 hash, never raw ID
    query_text: str = ""
    # CURANIQ output (never shown to clinician)
    curaniq_recommendation: str = ""
    curaniq_safety_flags: list[str] = field(default_factory=list)
    curaniq_confidence: Optional[float] = None
    curaniq_drugs_flagged: list[str] = field(default_factory=list)
    curaniq_evidence_ids: list[str] = field(default_factory=list)
    curaniq_latency_ms: float = 0.0
    curaniq_mode: str = ""
    curaniq_refused: bool = False
    # Clinician actual decision (recorded after the fact)
    clinician_action: Optional[str] = None
    clinician_drugs_prescribed: list[str] = field(default_factory=list)
    clinician_decision_time: Optional[str] = None
    # Comparison
    outcome: Optional[ComparisonOutcome] = None
    outcome_notes: Optional[str] = None
    # Clinical outcome (long-term, when available)
    patient_outcome_30d: Optional[str] = None  # "improved", "adverse_event", "readmission"


@dataclass
class ShadowSession:
    """A shadow mode session for a clinical encounter."""
    session_id: str = field(default_factory=lambda: str(uuid4()))
    tenant_id: str = ""
    department: Optional[str] = None
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    predictions: list[ShadowPrediction] = field(default_factory=list)
    active: bool = True


# ─────────────────────────────────────────────────────────────────
# SHADOW METRICS — DECIDE-AI evaluation framework
# ─────────────────────────────────────────────────────────────────

@dataclass
class ShadowMetrics:
    """
    Aggregated shadow mode metrics per evaluation period.
    Maps to DECIDE-AI framework requirements.
    """
    period_start: str = ""
    period_end: str = ""
    tenant_id: str = ""

    # Volume
    total_predictions: int = 0
    total_with_clinician_comparison: int = 0

    # Agreement metrics
    agreement_count: int = 0
    agreement_rate: float = 0.0

    # Safety metrics (the most important ones)
    safety_catches: int = 0         # CURANIQ caught issue clinician missed
    safety_catch_rate: float = 0.0
    false_alarms: int = 0
    false_alarm_rate: float = 0.0

    # Override analysis
    clinician_overrides: int = 0
    override_rate: float = 0.0

    # Performance
    avg_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    refusal_rate: float = 0.0

    # Confidence calibration
    # Bucket: confidence_range → actual_correctness_rate
    confidence_calibration: dict[str, float] = field(default_factory=dict)

    # Inconclusive
    inconclusive_count: int = 0

    def to_dict(self) -> dict:
        return {
            "period": f"{self.period_start} to {self.period_end}",
            "tenant_id": self.tenant_id,
            "volume": {
                "total_predictions": self.total_predictions,
                "compared": self.total_with_clinician_comparison,
            },
            "agreement": {
                "rate": round(self.agreement_rate, 4),
                "count": self.agreement_count,
            },
            "safety": {
                "catches": self.safety_catches,
                "catch_rate": round(self.safety_catch_rate, 4),
                "false_alarms": self.false_alarms,
                "false_alarm_rate": round(self.false_alarm_rate, 4),
            },
            "overrides": {
                "count": self.clinician_overrides,
                "rate": round(self.override_rate, 4),
            },
            "performance": {
                "avg_latency_ms": round(self.avg_latency_ms, 1),
                "p95_latency_ms": round(self.p95_latency_ms, 1),
                "refusal_rate": round(self.refusal_rate, 4),
            },
            "confidence_calibration": self.confidence_calibration,
        }


# ─────────────────────────────────────────────────────────────────
# SHADOW DEPLOYMENT ENGINE
# ─────────────────────────────────────────────────────────────────

class ShadowDeploymentEngine:
    """
    L10-1: Shadow Deployment Mode.
    
    Runs CURANIQ in silent mode alongside clinical workflow.
    Collects predictions, compares with clinician decisions,
    and generates DECIDE-AI evaluation metrics.
    
    Architecture contract:
    - CURANIQ NEVER shows predictions to clinicians in shadow mode
    - Patient data is hashed (SHA-256) for privacy in analysis
    - All comparisons stored for regulatory audit (L9-1)
    - Minimum 1,000 encounters before going live (configurable)
    """

    def __init__(
        self,
        tenant_id: str,
        min_encounters_for_live: int = 1000,
    ) -> None:
        self.tenant_id = tenant_id
        self.min_encounters_for_live = min_encounters_for_live
        self._sessions: dict[str, ShadowSession] = {}
        self._all_predictions: list[ShadowPrediction] = []
        self._active = True

    @property
    def is_active(self) -> bool:
        return self._active

    def start_session(self, department: Optional[str] = None) -> ShadowSession:
        """Start a new shadow session for a clinical encounter."""
        session = ShadowSession(tenant_id=self.tenant_id, department=department)
        self._sessions[session.session_id] = session
        return session

    def record_prediction(
        self,
        session_id: str,
        patient_id_hash: str,
        query_text: str,
        curaniq_recommendation: str,
        curaniq_safety_flags: list[str],
        curaniq_confidence: Optional[float],
        curaniq_drugs_flagged: list[str],
        curaniq_evidence_ids: list[str],
        curaniq_latency_ms: float,
        curaniq_mode: str = "",
        curaniq_refused: bool = False,
    ) -> ShadowPrediction:
        """
        Record a CURANIQ silent prediction.
        Called by the pipeline when shadow mode is active.
        """
        prediction = ShadowPrediction(
            patient_id_hash=patient_id_hash,
            query_text=query_text,
            curaniq_recommendation=curaniq_recommendation,
            curaniq_safety_flags=curaniq_safety_flags,
            curaniq_confidence=curaniq_confidence,
            curaniq_drugs_flagged=curaniq_drugs_flagged,
            curaniq_evidence_ids=curaniq_evidence_ids,
            curaniq_latency_ms=curaniq_latency_ms,
            curaniq_mode=curaniq_mode,
            curaniq_refused=curaniq_refused,
        )

        session = self._sessions.get(session_id)
        if session:
            session.predictions.append(prediction)
        self._all_predictions.append(prediction)

        return prediction

    def record_clinician_decision(
        self,
        prediction_id: str,
        clinician_action: str,
        clinician_drugs_prescribed: list[str],
    ) -> Optional[ShadowPrediction]:
        """
        Record what the clinician actually did (captured from EHR/CDS Hooks).
        Triggers automatic comparison.
        """
        for pred in reversed(self._all_predictions):
            if pred.prediction_id == prediction_id:
                pred.clinician_action = clinician_action
                pred.clinician_drugs_prescribed = clinician_drugs_prescribed
                pred.clinician_decision_time = datetime.now(timezone.utc).isoformat()

                # Auto-compare
                pred.outcome = self._compare(pred)
                return pred
        return None

    def _compare(self, prediction: ShadowPrediction) -> ComparisonOutcome:
        """
        Compare CURANIQ prediction vs clinician decision.
        This is statistical pattern matching, not LLM inference.
        """
        if not prediction.clinician_action:
            return ComparisonOutcome.INCONCLUSIVE

        has_safety_flags = bool(prediction.curaniq_safety_flags)
        clinician_acted_on_flags = any(
            flag_drug.lower() not in [d.lower() for d in prediction.clinician_drugs_prescribed]
            for flag_drug in prediction.curaniq_drugs_flagged
        ) if prediction.curaniq_drugs_flagged else False

        # Did CURANIQ refuse and clinician also declined?
        if prediction.curaniq_refused:
            if "declined" in prediction.clinician_action.lower() or "not prescribed" in prediction.clinician_action.lower():
                return ComparisonOutcome.AGREE
            return ComparisonOutcome.CLINICIAN_OVERRIDE

        # Drug overlap check
        curaniq_drugs_lower = {d.lower() for d in prediction.curaniq_drugs_flagged}
        clinician_drugs_lower = {d.lower() for d in prediction.clinician_drugs_prescribed}

        if has_safety_flags and clinician_acted_on_flags:
            return ComparisonOutcome.CURANIQ_SAFER
        elif has_safety_flags and not clinician_acted_on_flags:
            # CURANIQ flagged but clinician went ahead — could be false alarm or override
            if curaniq_drugs_lower & clinician_drugs_lower:
                return ComparisonOutcome.CLINICIAN_OVERRIDE
            return ComparisonOutcome.CURANIQ_FALSE_ALARM
        elif not has_safety_flags:
            # No safety flags — check general agreement
            if curaniq_drugs_lower & clinician_drugs_lower:
                return ComparisonOutcome.AGREE
            return ComparisonOutcome.CLINICIAN_OVERRIDE

        return ComparisonOutcome.INCONCLUSIVE

    def compute_metrics(
        self,
        period_start: Optional[str] = None,
        period_end: Optional[str] = None,
    ) -> ShadowMetrics:
        """
        Compute DECIDE-AI evaluation metrics for a period.
        Returns aggregated metrics for regulatory submission.
        """
        preds = self._all_predictions
        if period_start:
            preds = [p for p in preds if p.timestamp >= period_start]
        if period_end:
            preds = [p for p in preds if p.timestamp <= period_end]

        metrics = ShadowMetrics(
            period_start=period_start or (preds[0].timestamp if preds else ""),
            period_end=period_end or (preds[-1].timestamp if preds else ""),
            tenant_id=self.tenant_id,
            total_predictions=len(preds),
        )

        # Only count predictions with clinician comparison
        compared = [p for p in preds if p.outcome is not None]
        metrics.total_with_clinician_comparison = len(compared)

        if compared:
            outcomes = [p.outcome for p in compared]
            metrics.agreement_count = outcomes.count(ComparisonOutcome.AGREE)
            metrics.safety_catches = outcomes.count(ComparisonOutcome.CURANIQ_SAFER)
            metrics.false_alarms = outcomes.count(ComparisonOutcome.CURANIQ_FALSE_ALARM)
            metrics.clinician_overrides = outcomes.count(ComparisonOutcome.CLINICIAN_OVERRIDE)
            metrics.inconclusive_count = outcomes.count(ComparisonOutcome.INCONCLUSIVE)

            n = len(compared)
            metrics.agreement_rate = metrics.agreement_count / n
            metrics.safety_catch_rate = metrics.safety_catches / n if n else 0
            metrics.false_alarm_rate = metrics.false_alarms / n if n else 0
            metrics.override_rate = metrics.clinician_overrides / n if n else 0

        # Latency statistics
        latencies = [p.curaniq_latency_ms for p in preds if p.curaniq_latency_ms > 0]
        if latencies:
            metrics.avg_latency_ms = sum(latencies) / len(latencies)
            sorted_lat = sorted(latencies)
            p95_idx = int(len(sorted_lat) * 0.95)
            metrics.p95_latency_ms = sorted_lat[min(p95_idx, len(sorted_lat) - 1)]

        # Refusal rate
        refused = sum(1 for p in preds if p.curaniq_refused)
        metrics.refusal_rate = refused / len(preds) if preds else 0

        # Confidence calibration (bucket into 0.0-0.5, 0.5-0.7, 0.7-0.85, 0.85-1.0)
        buckets = {"0.00-0.50": [], "0.50-0.70": [], "0.70-0.85": [], "0.85-1.00": []}
        for p in compared:
            if p.curaniq_confidence is None:
                continue
            correct = p.outcome in (ComparisonOutcome.AGREE, ComparisonOutcome.CURANIQ_SAFER)
            c = p.curaniq_confidence
            if c < 0.50:
                buckets["0.00-0.50"].append(correct)
            elif c < 0.70:
                buckets["0.50-0.70"].append(correct)
            elif c < 0.85:
                buckets["0.70-0.85"].append(correct)
            else:
                buckets["0.85-1.00"].append(correct)

        for bucket_name, values in buckets.items():
            if values:
                metrics.confidence_calibration[bucket_name] = round(
                    sum(values) / len(values), 4
                )

        return metrics

    def readiness_assessment(self) -> dict[str, Any]:
        """
        Assess whether CURANIQ is ready to go live (exit shadow mode).
        Based on minimum encounter threshold + safety metrics.
        """
        metrics = self.compute_metrics()
        n = metrics.total_with_clinician_comparison

        checks = {
            "min_encounters_met": n >= self.min_encounters_for_live,
            "encounters": f"{n}/{self.min_encounters_for_live}",
            "agreement_rate_acceptable": metrics.agreement_rate >= 0.70,
            "agreement_rate": f"{metrics.agreement_rate:.1%}",
            "false_alarm_rate_acceptable": metrics.false_alarm_rate <= 0.20,
            "false_alarm_rate": f"{metrics.false_alarm_rate:.1%}",
            "safety_catch_rate": f"{metrics.safety_catch_rate:.1%}",
            "avg_latency_acceptable": metrics.avg_latency_ms <= 5000,
            "avg_latency_ms": f"{metrics.avg_latency_ms:.0f}",
        }

        all_passed = all(
            v for k, v in checks.items() if k.endswith("_met") or k.endswith("_acceptable")
        )
        checks["ready_for_live"] = all_passed
        checks["recommendation"] = (
            "READY for supervised live deployment"
            if all_passed
            else "CONTINUE shadow mode — criteria not yet met"
        )

        return checks

    @property
    def total_predictions(self) -> int:
        return len(self._all_predictions)
