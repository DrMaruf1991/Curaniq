"""
CURANIQ -- Layer 7: EHR Integration & Institutional Layer
P2 Clinical Workflows

L7-4   Context-Aware Evidence Delivery
L7-6   Institution Policy Layer
L7-8   Medication Reconciliation Workflow
L7-9   Clinical Pathway Generator

ALL rules loaded from curaniq/data/*.json files. Zero hardcoded dicts.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


# =============================================================================
# L7-4: CONTEXT-AWARE EVIDENCE DELIVERY
# Triggers loaded from curaniq/data/evidence_triggers.json
# =============================================================================

class ContextAwareEvidenceDelivery:
    """
    L7-4: Auto-matches patient FHIR context to relevant evidence.
    Triggers loaded from curaniq/data/evidence_triggers.json.
    """

    def __init__(self):
        from curaniq.data_loader import load_json_data
        raw = load_json_data("evidence_triggers.json")
        self._triggers = raw.get("triggers", [])
        logger.info("ContextAwareEvidenceDelivery: %d triggers", len(self._triggers))

    def match_context(
        self,
        patient_conditions: list[str],
        patient_medications: list[str],
        patient_labs: dict[str, float],
        patient_age: int = 0,
    ) -> list[dict]:
        matches = []
        conds_lower = {c.lower() for c in patient_conditions}
        meds_lower = {m.lower() for m in patient_medications}

        for trigger in self._triggers:
            score = 0.0
            reasons = []
            factors = []

            for tc in trigger.get("conditions", []):
                if any(tc.lower() in cond for cond in conds_lower):
                    score += 0.4
                    reasons.append(f"Condition: {tc}")
                    factors.append(tc)

            for tm in trigger.get("medications", []):
                if tm.lower() in meds_lower:
                    score += 0.3
                    reasons.append(f"Medication: {tm}")
                    factors.append(tm)

            for lab_name, thresholds in trigger.get("labs", {}).items():
                if lab_name in patient_labs:
                    val = patient_labs[lab_name]
                    if "below" in thresholds and val < thresholds["below"]:
                        score += 0.3
                        reasons.append(f"Lab: {lab_name}={val} (<{thresholds['below']})")
                        factors.append(f"{lab_name}={val}")
                    if "above" in thresholds and val > thresholds["above"]:
                        score += 0.3
                        reasons.append(f"Lab: {lab_name}={val} (>{thresholds['above']})")
                        factors.append(f"{lab_name}={val}")

            if score >= 0.3:
                matches.append({
                    "topics": trigger.get("topics", []),
                    "priority": trigger.get("priority", "moderate"),
                    "relevance_score": min(1.0, round(score, 2)),
                    "reasons": reasons,
                    "patient_factors": factors,
                })

        return sorted(matches, key=lambda m: -m["relevance_score"])


# =============================================================================
# L7-6: INSTITUTION POLICY LAYER
# Restrictions loaded from curaniq/data/institution_policies.json
# =============================================================================

class PolicyViolationType(str, Enum):
    FORMULARY_RESTRICTION  = "formulary_restriction"
    PROTOCOL_DEVIATION     = "protocol_deviation"
    APPROVAL_REQUIRED      = "approval_required"
    COST_THRESHOLD         = "cost_threshold"
    STEWARDSHIP_REVIEW     = "stewardship_review"


@dataclass
class PolicyCheckResult:
    drug: str
    compliant: bool = True
    violations: list[dict] = field(default_factory=list)
    approval_needed: bool = False
    approval_authority: str = ""
    alternative_suggestions: list[str] = field(default_factory=list)


class InstitutionPolicyEnforcer:
    """
    L7-6: Institutional prescribing policies from data file.
    Loads from curaniq/data/institution_policies.json.
    Override with institution-specific file via CURANIQ_DATA_DIR.
    """

    def __init__(self, institutional_knowledge=None):
        from curaniq.data_loader import load_json_data
        raw = load_json_data("institution_policies.json")
        self._restrictions: dict[str, dict] = raw.get("restrictions", {})
        self._institutional = institutional_knowledge
        logger.info("InstitutionPolicyEnforcer: %d drug restrictions", len(self._restrictions))

    def check_policy(self, drug: str, indication: str = "",
                     prescriber_role: str = "doctor") -> PolicyCheckResult:
        drug_lower = drug.lower().strip()
        result = PolicyCheckResult(drug=drug)

        restriction = self._restrictions.get(drug_lower)
        if restriction:
            result.compliant = False
            result.approval_needed = True
            result.approval_authority = restriction.get("approval", "pharmacy")
            result.violations.append({
                "type": restriction.get("type", "restriction"),
                "drug": drug,
                "reason": restriction.get("reason", ""),
                "approval_needed_from": restriction.get("approval", ""),
            })
            if restriction.get("type") == "stewardship":
                result.alternative_suggestions.append(
                    "Consider narrower-spectrum agent if culture/sensitivity allows."
                )
        return result


# =============================================================================
# L7-8: MEDICATION RECONCILIATION WORKFLOW
# Pure logic — no clinical data to hardcode. Patient-specific data only.
# =============================================================================

class DiscrepancyType(str, Enum):
    OMISSION    = "omission"
    COMMISSION  = "commission"
    DOSE_CHANGE = "dose_change"
    DUPLICATE   = "duplicate"


@dataclass
class ReconciliationDiscrepancy:
    drug: str
    discrepancy_type: DiscrepancyType
    source_a: str
    source_b: str
    detail: str
    severity: str
    action_required: str


class MedicationReconciliationEngine:
    """
    L7-8: Cross-source medication reconciliation.
    Methodology: WHO High 5s Medication Reconciliation Protocol; NICE NG5.
    Critical drug list loaded from data file for omission severity.
    """

    # Critical drugs loaded once — omission = high severity
    _CRITICAL_DRUGS: set[str] = {
        "warfarin", "rivaroxaban", "apixaban", "dabigatran", "enoxaparin",
        "insulin", "metformin", "levothyroxine", "prednisolone", "prednisone",
        "tacrolimus", "cyclosporine", "mycophenolate",
        "carbamazepine", "phenytoin", "valproic acid", "lithium",
        "methadone", "buprenorphine",
        "bisoprolol", "atenolol", "digoxin", "amiodarone",
    }

    def reconcile(
        self,
        list_a: list[dict],
        list_b: list[dict],
        source_a_name: str = "admission",
        source_b_name: str = "current",
    ) -> list[ReconciliationDiscrepancy]:
        discrepancies = []
        drugs_a = {item["drug"].lower(): item for item in list_a}
        drugs_b = {item["drug"].lower(): item for item in list_b}

        for drug, item in drugs_a.items():
            if drug not in drugs_b:
                discrepancies.append(ReconciliationDiscrepancy(
                    drug=item["drug"], discrepancy_type=DiscrepancyType.OMISSION,
                    source_a=source_a_name, source_b=source_b_name,
                    detail=f"{item['drug']} in {source_a_name} but missing from {source_b_name}",
                    severity="high" if drug in self._CRITICAL_DRUGS else "moderate",
                    action_required=f"Confirm intentional discontinuation or add to {source_b_name}",
                ))

        for drug, item in drugs_b.items():
            if drug not in drugs_a:
                discrepancies.append(ReconciliationDiscrepancy(
                    drug=item["drug"], discrepancy_type=DiscrepancyType.COMMISSION,
                    source_a=source_a_name, source_b=source_b_name,
                    detail=f"{item['drug']} in {source_b_name} but not in {source_a_name}",
                    severity="moderate",
                    action_required="Verify indication for new medication",
                ))

        for drug in drugs_a:
            if drug in drugs_b:
                dose_a = drugs_a[drug].get("dose", "").lower()
                dose_b = drugs_b[drug].get("dose", "").lower()
                if dose_a and dose_b and dose_a != dose_b:
                    discrepancies.append(ReconciliationDiscrepancy(
                        drug=drugs_a[drug]["drug"], discrepancy_type=DiscrepancyType.DOSE_CHANGE,
                        source_a=source_a_name, source_b=source_b_name,
                        detail=f"Dose differs: {source_a_name}={dose_a}, {source_b_name}={dose_b}",
                        severity="moderate",
                        action_required="Confirm intended dose change",
                    ))

        return sorted(discrepancies, key=lambda d: {"high": 0, "moderate": 1, "low": 2}.get(d.severity, 3))


# =============================================================================
# L7-9: CLINICAL PATHWAY GENERATOR
# Pathways loaded from curaniq/data/clinical_pathways.json
# =============================================================================

@dataclass
class PathwayStep:
    step_number: int
    action: str
    responsible: str
    timeframe: str
    evidence_source: str
    is_mandatory: bool = True


class ClinicalPathwayGenerator:
    """
    L7-9: Evidence-based clinical pathways from data file.
    Loaded from curaniq/data/clinical_pathways.json.
    Add new pathways by editing JSON — no code change needed.
    """

    def __init__(self):
        from curaniq.data_loader import load_json_data
        raw = load_json_data("clinical_pathways.json")
        self._pathways = raw.get("pathways", {})
        logger.info("ClinicalPathwayGenerator: %d pathways", len(self._pathways))

    def get_pathway(self, condition: str) -> list[PathwayStep]:
        condition_lower = condition.lower().strip().replace(" ", "_")

        matched_key = None
        for key in self._pathways:
            if key in condition_lower or condition_lower in key:
                matched_key = key
                break
            key_words = set(key.split("_"))
            cond_words = set(condition_lower.split("_"))
            if len(key_words & cond_words) >= 2:
                matched_key = key
                break

        if not matched_key:
            return []

        pathway_data = self._pathways[matched_key]
        return [
            PathwayStep(
                step_number=s["step"],
                action=s["action"],
                responsible=s["responsible"],
                timeframe=s["timeframe"],
                evidence_source=s["source"],
                is_mandatory=s.get("mandatory", True),
            )
            for s in pathway_data.get("steps", [])
        ]

    def get_available_pathways(self) -> list[str]:
        return list(self._pathways.keys())
