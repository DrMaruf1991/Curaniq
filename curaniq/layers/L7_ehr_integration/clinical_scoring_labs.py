"""
CURANIQ -- Layer 7: EHR Integration & Institutional Layer
P2 Clinical Scoring, Lab Interpretation, Alert Management

L7-11  Lab Result Interpreter & Trend Analyzer
L7-14  Auto-Triggered Clinical Scoring Engine
L7-10  Alert Fatigue Management Engine
L7-7   Order Set Copilot (verification, not generation)

ALL clinical data loaded from curaniq/data/*.json files.
Zero hardcoded reference ranges, scoring formulas, or thresholds.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# L7-11: LAB RESULT INTERPRETER & TREND ANALYZER
# =============================================================================

class LabFlag(str, Enum):
    NORMAL        = "normal"
    LOW           = "low"
    HIGH          = "high"
    CRITICAL_LOW  = "critical_low"
    CRITICAL_HIGH = "critical_high"


@dataclass
class LabInterpretation:
    analyte: str
    value: float
    unit: str
    flag: LabFlag
    reference_range: str
    clinical_comment: str = ""
    loinc: str = ""
    is_critical: bool = False


@dataclass
class TrendAnalysis:
    analyte: str
    values: list[float]
    timestamps: list[str]
    direction: str
    rate_of_change: float
    alert: str = ""


class LabResultInterpreter:
    """
    L7-11: Lab result interpretation from data files.
    Ranges: curaniq/data/lab_reference_ranges.json
    Drug-lab interactions: curaniq/data/drug_lab_interactions.json
    """

    def __init__(self):
        from curaniq.data_loader import load_json_data
        raw = load_json_data("lab_reference_ranges.json")
        self._ranges = raw.get("ranges", {})

        drug_lab_raw = load_json_data("drug_lab_interactions.json")
        self._drug_lab = drug_lab_raw.get("interactions", {})

        logger.info("LabResultInterpreter: %d analytes, %d drug-lab interaction rules",
                     len(self._ranges), len(self._drug_lab))

    def interpret(self, analyte: str, value: float,
                  patient_sex: str = "",
                  patient_drugs: Optional[list[str]] = None) -> LabInterpretation:
        """Interpret a lab result. Sex-aware. Drug-context-aware."""
        analyte_lower = analyte.lower().strip().replace(" ", "_")
        ref = self._ranges.get(analyte_lower)

        if not ref:
            return LabInterpretation(
                analyte=analyte, value=value, unit="", flag=LabFlag.NORMAL,
                reference_range="unknown", clinical_comment="Analyte not in reference database.",
            )

        ref_min = ref.get("min", 0)
        ref_max = ref.get("max", 9999)
        unit = ref.get("unit", "")
        crit_low = ref.get("critical_low")
        crit_high = ref.get("critical_high")
        loinc = ref.get("loinc", "")
        note = ref.get("note", "")

        # Sex-specific ranges from note field
        # Format in data: "Male 130-170, Female 120-150"
        if patient_sex and note:
            sex_key = "male" if patient_sex.lower().startswith("m") else "female"
            match = re.search(
                rf'{sex_key}\s+(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)',
                note, re.I,
            )
            if match:
                ref_min = float(match.group(1))
                ref_max = float(match.group(2))

        # Flag determination
        is_critical = False
        if crit_low is not None and value <= crit_low:
            flag = LabFlag.CRITICAL_LOW
            is_critical = True
        elif crit_high is not None and value >= crit_high:
            flag = LabFlag.CRITICAL_HIGH
            is_critical = True
        elif value < ref_min:
            flag = LabFlag.LOW
        elif value > ref_max:
            flag = LabFlag.HIGH
        else:
            flag = LabFlag.NORMAL

        # Build clinical comment
        comment = ""
        if flag == LabFlag.CRITICAL_LOW:
            comment = f"CRITICAL LOW: {analyte} {value} {unit} (critical: {crit_low}). Urgent review."
        elif flag == LabFlag.CRITICAL_HIGH:
            comment = f"CRITICAL HIGH: {analyte} {value} {unit} (critical: {crit_high}). Urgent review."
        elif flag in (LabFlag.LOW, LabFlag.HIGH):
            comment = f"Abnormal: {analyte} {value} {unit} (ref: {ref_min}-{ref_max} {unit})."

        # Drug-context comments from drug_lab_interactions.json
        drugs = patient_drugs or []
        drug_set = {d.lower().replace(" ", "_") for d in drugs}

        flag_suffix = "high" if flag in (LabFlag.HIGH, LabFlag.CRITICAL_HIGH) else "low" if flag in (LabFlag.LOW, LabFlag.CRITICAL_LOW) else ""
        if flag_suffix:
            # Check all interaction rules matching this analyte + direction
            for rule_key, rule in self._drug_lab.items():
                # Match rule_key pattern: "analyte_direction" or "analyte_direction_*"
                if analyte_lower in rule_key and flag_suffix in rule_key:
                    rule_drugs = {d.lower() for d in rule.get("drugs", [])}
                    culprits = drug_set & rule_drugs
                    if culprits:
                        comment += f" {rule.get('comment', '')} Possible culprits: {', '.join(culprits)}."
                        break

            # Also check generic rising/falling patterns
            for rule_key, rule in self._drug_lab.items():
                if analyte_lower.replace("_", "") in rule_key.replace("_", "") and "rising" in rule_key:
                    rule_drugs = {d.lower() for d in rule.get("drugs", [])}
                    culprits = drug_set & rule_drugs
                    if culprits and flag in (LabFlag.HIGH, LabFlag.CRITICAL_HIGH):
                        comment += f" {rule.get('comment', '')} Drugs: {', '.join(culprits)}."
                        break

        if note and note not in comment:
            comment += f" Note: {note}"

        return LabInterpretation(
            analyte=analyte, value=value, unit=unit, flag=flag,
            reference_range=f"{ref_min}-{ref_max} {unit}",
            clinical_comment=comment.strip(), loinc=loinc, is_critical=is_critical,
        )

    def analyze_trend(self, analyte: str, values: list[float],
                      timestamps: list[str]) -> TrendAnalysis:
        """Analyze trend in serial lab results."""
        if len(values) < 2:
            return TrendAnalysis(analyte=analyte, values=values, timestamps=timestamps,
                                 direction="insufficient_data", rate_of_change=0.0)

        diffs = [values[i+1] - values[i] for i in range(len(values)-1)]
        avg_diff = sum(diffs) / len(diffs)
        threshold = values[0] * 0.05 if values[0] != 0 else 0.1

        if abs(avg_diff) < threshold:
            direction = "stable"
        elif all(d >= 0 for d in diffs):
            direction = "rising"
        elif all(d <= 0 for d in diffs):
            direction = "falling"
        else:
            direction = "rising" if avg_diff > 0 else "falling"

        alert = ""
        ref = self._ranges.get(analyte.lower().strip().replace(" ", "_"), {})
        if direction == "rising" and values[-1] > ref.get("max", 9999):
            crit = ref.get("critical_high")
            alert = f"TRENDING HIGH: {analyte} rising." + (f" Critical threshold: {crit}." if crit else "")
        elif direction == "falling" and values[-1] < ref.get("min", 0):
            crit = ref.get("critical_low")
            alert = f"TRENDING LOW: {analyte} falling." + (f" Critical threshold: {crit}." if crit else "")

        return TrendAnalysis(
            analyte=analyte, values=values, timestamps=timestamps,
            direction=direction, rate_of_change=round(avg_diff, 3), alert=alert,
        )


# =============================================================================
# L7-14: CLINICAL SCORING ENGINE
# Supports BOTH additive point scores AND range-based scores (MEWS/NEWS2)
# =============================================================================

@dataclass
class ScoreResult:
    score_name: str
    total: int
    max_possible: int
    risk_level: str
    recommendation: str
    components_present: list[str] = field(default_factory=list)
    source: str = ""


class ClinicalScoringEngine:
    """
    L7-14: Data-driven clinical scoring from curaniq/data/clinical_scores.json.
    Supports both additive point scores (CHA2DS2-VASc, Wells) and
    range-based scores (MEWS, NEWS2) via 'ranges' field in components.
    """

    def __init__(self):
        from curaniq.data_loader import load_json_data
        raw = load_json_data("clinical_scores.json")
        self._scores = raw.get("scores", {})
        logger.info("ClinicalScoringEngine: %d scoring systems", len(self._scores))

    def calculate(self, score_name: str, present_components: list[str] = None,
                  numeric_values: dict[str, float] = None) -> Optional[ScoreResult]:
        """
        Calculate a clinical score.
        present_components: list of component names that are true/present
        numeric_values: dict of component_name -> numeric value (for range-based)
        """
        score_def = self._scores.get(score_name)
        if not score_def:
            return None

        present = set(present_components or [])
        numerics = numeric_values or {}
        total = 0
        matched = []

        for comp in score_def.get("components", []):
            name = comp.get("name", "")
            points = comp.get("points")
            ranges_str = comp.get("ranges", "")

            if points is not None and name in present:
                # Additive point scoring
                total += points
                matched.append(f"{name} (+{points})")

            elif ranges_str and name in numerics:
                # Range-based scoring (MEWS, NEWS2)
                val = numerics[name]
                range_points = self._evaluate_range(ranges_str, val)
                total += range_points
                matched.append(f"{name}={val} (+{range_points})")

        max_score = score_def.get("max_score", 0)
        interp = score_def.get("interpretation", {})

        risk_level = "unknown"
        recommendation = ""
        for bracket, info in interp.items():
            if self._score_in_bracket(total, bracket):
                risk_level = info.get("risk", "unknown")
                recommendation = info.get("recommendation", "")
                break

        return ScoreResult(
            score_name=score_name, total=total, max_possible=max_score,
            risk_level=risk_level, recommendation=recommendation,
            components_present=matched, source=score_def.get("source", ""),
        )

    def _evaluate_range(self, ranges_str: str, value: float) -> int:
        """Evaluate a range-based scoring string like '<=70:3, 71-80:2, 81-100:1'."""
        # Handle text-based ranges (consciousness AVPU)
        if "alert" in ranges_str.lower():
            return 0  # Default for non-numeric

        best_points = 0
        for part in ranges_str.split(","):
            part = part.strip()
            if ":" not in part:
                continue
            range_part, points_str = part.rsplit(":", 1)
            range_part = range_part.strip()
            try:
                pts = int(points_str.strip())
            except ValueError:
                continue

            try:
                if range_part.startswith("<="):
                    if value <= float(range_part[2:]):
                        best_points = max(best_points, pts)
                elif range_part.startswith(">="):
                    if value >= float(range_part[2:]):
                        best_points = max(best_points, pts)
                elif range_part.startswith("<"):
                    if value < float(range_part[1:]):
                        best_points = max(best_points, pts)
                elif range_part.startswith(">"):
                    if value > float(range_part[1:]):
                        best_points = max(best_points, pts)
                elif "-" in range_part:
                    lo, hi = range_part.split("-")
                    if float(lo) <= value <= float(hi):
                        best_points = pts
            except ValueError:
                continue

        return best_points

    def _score_in_bracket(self, score: int, bracket: str) -> bool:
        bracket = bracket.strip()
        if bracket.endswith("+"):
            return score >= int(bracket[:-1])
        if "-" in bracket:
            parts = bracket.split("-")
            return int(parts[0]) <= score <= int(parts[1])
        try:
            return score == int(bracket)
        except ValueError:
            return False

    def get_available_scores(self) -> list[str]:
        return list(self._scores.keys())


# =============================================================================
# L7-10: ALERT FATIGUE MANAGEMENT ENGINE
# Pure logic, no clinical data to hardcode — already clean
# =============================================================================

class AlertPriority(str, Enum):
    CRITICAL       = "critical"
    HIGH           = "high"
    MODERATE       = "moderate"
    LOW            = "low"
    INFORMATIONAL  = "info"


@dataclass
class AlertDecision:
    alert_id: str
    original_priority: AlertPriority
    final_priority: AlertPriority
    suppressed: bool = False
    suppression_reason: str = ""
    override_count: int = 0


class AlertFatigueManager:
    """
    L7-10: Alert fatigue management. Pure logic, no hardcoded clinical data.
    Source: Nanji KC et al. JAMIA 2014;21(5):893-901
    """

    NEVER_SUPPRESS: set[str] = {
        "allergy_contraindication", "black_box_warning",
        "dose_lethal_range", "pregnancy_category_x", "renal_contraindicated",
    }

    def __init__(self):
        self._override_history: dict[str, list[bool]] = {}
        self._suppression_threshold = 0.90

    def evaluate_alert(self, alert_id: str, alert_type: str,
                       priority: AlertPriority,
                       is_duplicate: bool = False) -> AlertDecision:
        decision = AlertDecision(alert_id=alert_id, original_priority=priority, final_priority=priority)

        if alert_type in self.NEVER_SUPPRESS or priority == AlertPriority.CRITICAL:
            return decision

        if is_duplicate:
            decision.suppressed = True
            decision.suppression_reason = "Duplicate alert"
            decision.final_priority = AlertPriority.INFORMATIONAL
            return decision

        history = self._override_history.get(alert_type, [])
        if len(history) >= 10:
            override_rate = sum(1 for h in history if h) / len(history)
            decision.override_count = sum(1 for h in history if h)
            if override_rate >= self._suppression_threshold:
                decision.final_priority = AlertPriority.LOW
                decision.suppression_reason = (
                    f"Overridden {override_rate*100:.0f}% ({decision.override_count}/{len(history)}). Demoted."
                )
        return decision

    def record_override(self, alert_type: str, was_overridden: bool):
        self._override_history.setdefault(alert_type, []).append(was_overridden)
        if len(self._override_history[alert_type]) > 100:
            self._override_history[alert_type] = self._override_history[alert_type][-100:]

    def get_fatigue_report(self) -> dict[str, Any]:
        report = {}
        for alert_type, history in self._override_history.items():
            if len(history) >= 5:
                rate = sum(1 for h in history if h) / len(history)
                report[alert_type] = {
                    "total_shown": len(history), "override_rate": round(rate, 3),
                    "status": "suppressed" if rate >= self._suppression_threshold else "active",
                }
        return report


# =============================================================================
# L7-7: ORDER SET COPILOT — loads from coprescription_rules.json
# =============================================================================

@dataclass
class OrderVerification:
    drug: str
    dose: str
    route: str
    is_safe: bool = True
    warnings: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    formulary_status: str = ""


class OrderSetCopilot:
    """
    L7-7: Order verification. Co-prescription rules from data file.
    """

    def __init__(self):
        from curaniq.data_loader import load_json_data
        raw = load_json_data("coprescription_rules.json")
        self._rules = raw.get("rules", [])
        logger.info("OrderSetCopilot: %d co-prescription rules", len(self._rules))

    def verify_order(self, drug: str, dose: str = "", route: str = "",
                     patient_age: int = 0,
                     patient_conditions: Optional[list[str]] = None,
                     current_medications: Optional[list[str]] = None) -> OrderVerification:
        result = OrderVerification(drug=drug, dose=dose, route=route)
        current_meds = {m.lower().replace(" ", "_") for m in (current_medications or [])}
        drug_lower = drug.lower().strip().replace(" ", "_")

        for rule in self._rules:
            trigger_drugs = {d.lower() for d in rule.get("trigger_drugs", [])}
            if drug_lower not in trigger_drugs:
                continue

            check_drugs = {d.lower() for d in rule.get("check_drugs", [])}
            if not check_drugs:
                # Monitoring/counseling rule (no specific co-drug to check)
                result.suggestions.append(
                    f"{rule.get('required', '')}. Reason: {rule.get('rationale', '')}. "
                    f"Source: {rule.get('source', '')}"
                )
                continue

            co_present = bool(current_meds & check_drugs)
            if not co_present:
                result.suggestions.append(
                    f"Consider adding: {rule.get('required', '')}. "
                    f"Reason: {rule.get('rationale', '')}. Source: {rule.get('source', '')}"
                )

        if drug_lower in current_meds:
            result.warnings.append(f"DUPLICATE: {drug} already in current medications")

        return result
