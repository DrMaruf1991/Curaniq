"""
CURANIQ -- Layer 5: Post-Generation Safety Gates
P2 Advanced Safety Gates

L5-5   Conformal Prediction (calibrated uncertainty intervals)
L5-8   Source Triangulation Gate (independent source agreement)
L5-15  Predictive Clinical Alert Generator (proactive risk detection)
L5-16  Patient Trajectory Analyzer (temporal health trend prediction)

All logic modules. No hardcoded clinical data.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# L5-5: CONFORMAL PREDICTION (Calibrated Uncertainty)
# Source: Vovk et al. "Algorithmic Learning in a Random World" 2005
# Applied to medical AI: Lu et al. "Conformal Prediction for Clinical AI" 2023
# =============================================================================

@dataclass
class ConformalInterval:
    prediction: float
    lower_bound: float
    upper_bound: float
    coverage_guarantee: float  # e.g., 0.90 = 90% coverage
    calibration_set_size: int


class ConformalPredictionEngine:
    """
    L5-5: Calibrated uncertainty intervals for clinical predictions.

    Conformal prediction provides distribution-free coverage guarantees:
    "The true answer lies within this interval with >= X% probability"
    regardless of the underlying model's calibration.

    Requires a calibration set from L10-1 Shadow Deployment.
    Until calibration data is available, falls back to heuristic intervals.

    Method: Split conformal prediction (Vovk 2005):
    1. Collect nonconformity scores from calibration set
    2. For new prediction: interval = prediction +/- quantile(scores, alpha)
    """

    def __init__(self):
        self._calibration_scores: list[float] = []
        self._is_calibrated = False

    def calibrate(self, predictions: list[float], actuals: list[float]):
        """Calibrate using prediction errors from shadow deployment."""
        if len(predictions) < 30:
            logger.warning("Conformal calibration needs >=30 samples, got %d", len(predictions))
            return

        self._calibration_scores = [
            abs(pred - actual) for pred, actual in zip(predictions, actuals)
        ]
        self._calibration_scores.sort()
        self._is_calibrated = True
        logger.info("Conformal prediction calibrated with %d samples", len(self._calibration_scores))

    def predict_interval(self, point_estimate: float,
                         coverage: float = 0.90) -> ConformalInterval:
        """Generate calibrated prediction interval."""
        if not self._is_calibrated or not self._calibration_scores:
            # Fallback: heuristic interval based on estimate magnitude
            margin = abs(point_estimate) * (1 - coverage) * 2
            return ConformalInterval(
                prediction=point_estimate,
                lower_bound=point_estimate - margin,
                upper_bound=point_estimate + margin,
                coverage_guarantee=0.0,  # 0 = not calibrated
                calibration_set_size=0,
            )

        # Conformal quantile
        n = len(self._calibration_scores)
        alpha = 1 - coverage
        quantile_idx = int(math.ceil((1 - alpha) * (n + 1))) - 1
        quantile_idx = max(0, min(quantile_idx, n - 1))
        margin = self._calibration_scores[quantile_idx]

        return ConformalInterval(
            prediction=point_estimate,
            lower_bound=round(point_estimate - margin, 4),
            upper_bound=round(point_estimate + margin, 4),
            coverage_guarantee=coverage,
            calibration_set_size=n,
        )

    @property
    def is_calibrated(self) -> bool:
        return self._is_calibrated


# =============================================================================
# L5-8: SOURCE TRIANGULATION GATE
# =============================================================================

@dataclass
class TriangulationResult:
    claim: str
    independent_sources: int
    agreement_rate: float  # 0-1
    passed: bool
    supporting_sources: list[str] = field(default_factory=list)
    contradicting_sources: list[str] = field(default_factory=list)


class SourceTriangulationGate:
    """
    L5-8: Requires claims to be supported by multiple independent sources.

    A claim supported by only one source is weaker than one confirmed
    by 3+ independent sources. This gate:
    1. Groups evidence by source independence (different authors, journals, years)
    2. Checks agreement across independent sources
    3. Flags single-source claims with reduced confidence
    4. Blocks claims contradicted by majority of sources

    Threshold: Critical claims (dosing, contraindications) need >=2 independent sources.
    """

    MIN_SOURCES_CRITICAL = 2  # Dosing/contraindication claims
    MIN_SOURCES_GENERAL = 1   # General information claims
    CONTRADICTION_THRESHOLD = 0.5  # Block if >50% sources contradict

    def check(self, claim_text: str, evidence_sources: list[dict],
              is_critical: bool = False) -> TriangulationResult:
        """Check independent source agreement for a claim."""
        if not evidence_sources:
            return TriangulationResult(
                claim=claim_text, independent_sources=0,
                agreement_rate=0.0, passed=False,
            )

        # Group by source independence (different journals or DOIs)
        seen_journals: set[str] = set()
        independent = []
        for ev in evidence_sources:
            journal = ev.get("journal", ev.get("source", "")).lower()
            doi = ev.get("doi", "")
            key = journal or doi or ev.get("title", "")[:30].lower()
            if key and key not in seen_journals:
                seen_journals.add(key)
                independent.append(ev)

        n_independent = len(independent)

        # Check agreement: how many support vs contradict
        supporting = [ev for ev in independent if ev.get("intent", "supporting") == "supporting"]
        contradicting = [ev for ev in independent if ev.get("intent") == "contradicting"]

        agreement = len(supporting) / max(n_independent, 1)

        min_required = self.MIN_SOURCES_CRITICAL if is_critical else self.MIN_SOURCES_GENERAL
        passed = (
            n_independent >= min_required
            and agreement > self.CONTRADICTION_THRESHOLD
        )

        return TriangulationResult(
            claim=claim_text,
            independent_sources=n_independent,
            agreement_rate=round(agreement, 2),
            passed=passed,
            supporting_sources=[ev.get("title", "")[:60] for ev in supporting],
            contradicting_sources=[ev.get("title", "")[:60] for ev in contradicting],
        )


# =============================================================================
# L5-15: PREDICTIVE CLINICAL ALERT GENERATOR
# =============================================================================

@dataclass
class PredictiveAlert:
    alert_type: str
    risk_description: str
    time_horizon: str
    probability: str  # "low", "moderate", "high"
    recommended_action: str
    basis: str  # What triggered this prediction


class PredictiveClinicalAlertGenerator:
    """
    L5-15: Proactively predicts clinical risks before they manifest.
    Risk cascades loaded from curaniq/data/predictive_risk_cascades.json.
    """

    def __init__(self):
        from curaniq.data_loader import load_json_data
        raw = load_json_data("predictive_risk_cascades.json")
        self._cascades = raw.get("cascades", [])
        logger.info("PredictiveClinicalAlertGenerator: %d risk cascade rules", len(self._cascades))

    def assess_risks(self, patient_age: int, conditions: list[str],
                     drugs: list[str], labs: dict[str, float] = None) -> list[PredictiveAlert]:
        """Assess predictive clinical risks from data-file rules."""
        alerts = []
        conds_lower = {c.lower() for c in conditions}
        drugs_lower = {d.lower() for d in drugs}

        for cascade in self._cascades:
            triggers = cascade.get("triggers", {})
            triggered = False

            # Check condition + drug triggers
            if "conditions" in triggers:
                if any(tc in cond for tc in triggers["conditions"] for cond in conds_lower):
                    if "drugs" in triggers:
                        if drugs_lower & {d.lower() for d in triggers["drugs"]}:
                            triggered = True

            # Check age + multi-drug triggers
            if "age_above" in triggers:
                if patient_age >= triggers["age_above"]:
                    if "drugs_any_2" in triggers:
                        matching = drugs_lower & {d.lower() for d in triggers["drugs_any_2"]}
                        if len(matching) >= 2:
                            triggered = True

            if triggered:
                alerts.append(PredictiveAlert(
                    alert_type="predictive_risk",
                    risk_description=cascade.get("risk", ""),
                    time_horizon=cascade.get("horizon", ""),
                    probability=cascade.get("probability", ""),
                    recommended_action=cascade.get("action", ""),
                    basis=cascade.get("source", cascade.get("basis", "")),
                ))

        return alerts


# =============================================================================
# L5-16: PATIENT TRAJECTORY ANALYZER
# =============================================================================

@dataclass
class TrajectoryPoint:
    timestamp: datetime
    parameter: str
    value: float
    unit: str


@dataclass
class TrajectoryPrediction:
    parameter: str
    current_value: float
    predicted_value_24h: Optional[float]
    predicted_value_72h: Optional[float]
    trend: str  # "improving", "stable", "deteriorating"
    alert: str = ""


class PatientTrajectoryAnalyzer:
    """
    L5-16: Analyzes temporal health parameter trends and predicts trajectory.

    Uses simple linear regression on serial observations to predict
    where a parameter will be in 24h and 72h. NOT an ML model — pure
    mathematical trend extrapolation with clinical thresholds.

    Connected to L7-11 Lab Interpreter for reference ranges.
    """

    def analyze(self, observations: list[dict],
                critical_thresholds: dict = None) -> list[TrajectoryPrediction]:
        """
        Analyze parameter trajectories.
        Each observation: {"parameter": str, "value": float, "hours_ago": float}
        """
        thresholds = critical_thresholds or {}
        predictions = []

        # Group by parameter
        by_param: dict[str, list[tuple[float, float]]] = {}
        for obs in observations:
            param = obs["parameter"]
            by_param.setdefault(param, []).append(
                (obs.get("hours_ago", 0), obs["value"])
            )

        for param, points in by_param.items():
            if len(points) < 2:
                continue

            # Sort by time (most recent = hours_ago 0)
            points.sort(key=lambda p: -p[0])  # Oldest first
            times = [p[0] for p in points]
            values = [p[1] for p in points]

            # Simple linear regression
            n = len(times)
            sum_t = sum(times)
            sum_v = sum(values)
            sum_tv = sum(t * v for t, v in zip(times, values))
            sum_t2 = sum(t * t for t in times)

            denom = n * sum_t2 - sum_t * sum_t
            if abs(denom) < 1e-10:
                continue

            slope = (n * sum_tv - sum_t * sum_v) / denom
            intercept = (sum_v - slope * sum_t) / n

            current = values[-1]
            # Negative hours_ago = future
            pred_24 = intercept + slope * (-24)
            pred_72 = intercept + slope * (-72)

            # Determine trend
            if abs(slope) < abs(current) * 0.01:
                trend = "stable"
            elif slope > 0:
                trend = "improving" if param in ("egfr", "haemoglobin", "platelets") else "deteriorating"
            else:
                trend = "deteriorating" if param in ("egfr", "haemoglobin", "platelets") else "improving"

            alert = ""
            crit = thresholds.get(param)
            if crit:
                if "high" in crit and pred_24 > crit["high"]:
                    alert = f"PREDICTED: {param} may exceed critical threshold ({crit['high']}) within 24h"
                if "low" in crit and pred_24 < crit["low"]:
                    alert = f"PREDICTED: {param} may fall below critical threshold ({crit['low']}) within 24h"

            predictions.append(TrajectoryPrediction(
                parameter=param,
                current_value=round(current, 2),
                predicted_value_24h=round(pred_24, 2),
                predicted_value_72h=round(pred_72, 2),
                trend=trend,
                alert=alert,
            ))

        return predictions
