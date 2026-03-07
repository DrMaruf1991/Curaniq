"""
CURANIQ — Medical Evidence Operating System
L7-2: CDS Hooks Service

Architecture spec:
  'Alert-style triggers at order-entry, prescription signing, admission.
  Evidence cards at the moment of decision.'

Implements the HL7 CDS Hooks specification (v1.1+):
  - Service Discovery (/cds-services)
  - Hook invocation (/cds-services/{id})
  - Card generation with evidence-backed suggestions
  - Prefetch templates for efficient FHIR data access
  - Feedback endpoint for clinician override tracking

Supported hooks:
  - patient-view: General medication safety check on chart open
  - order-sign: DDI/contraindication check before signing orders
  - medication-prescribe: Real-time safety check during prescribing
  - order-select: Early advisory during order selection

Every card traces back to evidence via L4-3 Claim Contract + L9-1 Audit Ledger.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# CDS HOOKS SPECIFICATION TYPES
# Per HL7 CDS Hooks v1.1 specification
# ─────────────────────────────────────────────────────────────────

class CDSHookType(str, Enum):
    """Supported CDS Hook trigger points."""
    PATIENT_VIEW = "patient-view"
    ORDER_SIGN = "order-sign"
    ORDER_SELECT = "order-select"
    MEDICATION_PRESCRIBE = "medication-prescribe"


class CardIndicator(str, Enum):
    """Visual urgency indicators per CDS Hooks spec."""
    INFO = "info"           # Blue — informational
    WARNING = "warning"     # Yellow — attention needed
    CRITICAL = "critical"   # Red — urgent action required
    HARD_STOP = "hard-stop" # CURANIQ extension — blocks order (life-threatening)


class CardSource:
    """Identifies the source system producing the card."""
    def __init__(
        self,
        label: str = "CURANIQ Medical Evidence OS",
        url: Optional[str] = None,
        icon: Optional[str] = None,
    ):
        self.label = label
        self.url = url or os.environ.get("CURANIQ_BASE_URL", "https://app.curaniq.com")
        self.icon = icon or f"{self.url}/static/curaniq-icon.png"

    def to_dict(self) -> dict:
        result = {"label": self.label}
        if self.url:
            result["url"] = self.url
        if self.icon:
            result["icon"] = self.icon
        return result


# ─────────────────────────────────────────────────────────────────
# CDS HOOKS CARD — the output unit
# Each card traces to evidence via source_evidence_ids
# ─────────────────────────────────────────────────────────────────

@dataclass
class CDSSuggestion:
    """
    Actionable suggestion within a card.
    Per spec: label, uuid, actions (FHIR resource updates).
    """
    label: str
    uuid: str = field(default_factory=lambda: str(uuid4()))
    is_recommended: bool = False
    actions: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        result = {"label": self.label, "uuid": self.uuid}
        if self.is_recommended:
            result["isRecommended"] = True
        if self.actions:
            result["actions"] = self.actions
        return result


@dataclass
class CDSLink:
    """External link within a card (e.g., to CURANIQ evidence deep-dive)."""
    label: str
    url: str
    link_type: str = "absolute"  # absolute | smart
    app_context: Optional[str] = None

    def to_dict(self) -> dict:
        result = {"label": self.label, "url": self.url, "type": self.link_type}
        if self.app_context:
            result["appContext"] = self.app_context
        return result


@dataclass
class CDSCard:
    """
    A CDS Hooks card — the primary output of the service.
    
    Every card maps to:
    - Evidence source(s) via source_evidence_ids → L9-3 Citation Provenance
    - Confidence score from L4-13
    - Safety gate results from L5 pipeline
    """
    uuid: str = field(default_factory=lambda: str(uuid4()))
    summary: str = ""           # One-line summary (≤140 chars per spec)
    detail: str = ""            # Markdown detail
    indicator: CardIndicator = CardIndicator.INFO
    source: CardSource = field(default_factory=CardSource)
    suggestions: list[CDSSuggestion] = field(default_factory=list)
    links: list[CDSLink] = field(default_factory=list)
    selection_behavior: Optional[str] = None   # "at-most-one" or None
    override_reasons: list[dict] = field(default_factory=list)

    # CURANIQ extensions (not in CDS Hooks spec, passed via extension field)
    curaniq_confidence: Optional[float] = None
    curaniq_evidence_ids: list[str] = field(default_factory=list)
    curaniq_query_id: Optional[str] = None
    curaniq_safety_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize to CDS Hooks JSON format."""
        result: dict[str, Any] = {
            "uuid": self.uuid,
            "summary": self.summary[:140],  # Spec limit
            "indicator": self.indicator.value,
            "source": self.source.to_dict(),
        }
        if self.detail:
            result["detail"] = self.detail
        if self.suggestions:
            result["suggestions"] = [s.to_dict() for s in self.suggestions]
        if self.links:
            result["links"] = [l.to_dict() for l in self.links]
        if self.selection_behavior:
            result["selectionBehavior"] = self.selection_behavior
        if self.override_reasons:
            result["overrideReasons"] = self.override_reasons

        # CURANIQ extensions
        extensions = {}
        if self.curaniq_confidence is not None:
            extensions["curaniq-confidence"] = self.curaniq_confidence
        if self.curaniq_evidence_ids:
            extensions["curaniq-evidence-ids"] = self.curaniq_evidence_ids
        if self.curaniq_query_id:
            extensions["curaniq-query-id"] = self.curaniq_query_id
        if self.curaniq_safety_flags:
            extensions["curaniq-safety-flags"] = self.curaniq_safety_flags
        if extensions:
            result["extension"] = extensions

        return result


# ─────────────────────────────────────────────────────────────────
# CDS HOOKS SERVICE DEFINITIONS — what hooks we support
# ─────────────────────────────────────────────────────────────────

@dataclass
class CDSServiceDefinition:
    """
    Describes a CDS Hook service endpoint.
    Published via GET /cds-services (service discovery).
    """
    hook: CDSHookType
    service_id: str
    title: str
    description: str
    prefetch: dict[str, str] = field(default_factory=dict)
    uses_fhir_authorization: bool = True

    def to_dict(self) -> dict:
        return {
            "hook": self.hook.value,
            "id": self.service_id,
            "title": self.title,
            "description": self.description,
            "prefetch": self.prefetch,
            "usesPatientData": True,
        }


# Service registry — all hooks CURANIQ exposes
SERVICE_REGISTRY: list[CDSServiceDefinition] = [
    CDSServiceDefinition(
        hook=CDSHookType.PATIENT_VIEW,
        service_id="curaniq-medication-review",
        title="CURANIQ Medication Safety Review",
        description=(
            "Comprehensive medication safety check when patient chart is opened. "
            "Checks DDIs, renal/hepatic dose adjustments, allergy cross-reactivity, "
            "and contraindications against evidence-backed guidelines."
        ),
        prefetch={
            "patient": "Patient/{{context.patientId}}",
            "medications": "MedicationRequest?patient={{context.patientId}}&status=active",
            "allergies": "AllergyIntolerance?patient={{context.patientId}}",
            "conditions": "Condition?patient={{context.patientId}}&clinical-status=active",
            "labs": "Observation?patient={{context.patientId}}&category=laboratory&_sort=-date&_count=50",
        },
    ),
    CDSServiceDefinition(
        hook=CDSHookType.ORDER_SIGN,
        service_id="curaniq-order-sign-check",
        title="CURANIQ Order Signing Safety Gate",
        description=(
            "Final safety check before order signing. Catches DDIs, "
            "duplicate therapy, dose errors, and contraindications "
            "that may have been missed during ordering."
        ),
        prefetch={
            "patient": "Patient/{{context.patientId}}",
            "medications": "MedicationRequest?patient={{context.patientId}}&status=active",
            "allergies": "AllergyIntolerance?patient={{context.patientId}}",
            "labs": "Observation?patient={{context.patientId}}&category=laboratory&_sort=-date&_count=20",
        },
    ),
    CDSServiceDefinition(
        hook=CDSHookType.MEDICATION_PRESCRIBE,
        service_id="curaniq-prescribe-check",
        title="CURANIQ Real-Time Prescribing Safety",
        description=(
            "Real-time medication safety check during prescribing. "
            "Provides dose recommendations, interaction warnings, "
            "and evidence-backed alternatives."
        ),
        prefetch={
            "patient": "Patient/{{context.patientId}}",
            "medications": "MedicationRequest?patient={{context.patientId}}&status=active",
            "allergies": "AllergyIntolerance?patient={{context.patientId}}",
            "labs": "Observation?patient={{context.patientId}}&category=laboratory&_sort=-date&_count=20",
        },
    ),
    CDSServiceDefinition(
        hook=CDSHookType.ORDER_SELECT,
        service_id="curaniq-order-select-advisory",
        title="CURANIQ Order Selection Advisory",
        description=(
            "Early advisory during order selection. Lightweight check "
            "for major contraindications and formulary availability."
        ),
        prefetch={
            "patient": "Patient/{{context.patientId}}",
            "allergies": "AllergyIntolerance?patient={{context.patientId}}",
        },
    ),
]


# ─────────────────────────────────────────────────────────────────
# CDS HOOKS SERVICE — main entry point
# ─────────────────────────────────────────────────────────────────

@dataclass
class CDSHookRequest:
    """Parsed CDS Hooks request from the EHR."""
    hook: CDSHookType
    hook_instance: str
    fhir_server: str
    fhir_authorization: Optional[dict] = None
    context: dict = field(default_factory=dict)
    prefetch: dict = field(default_factory=dict)

    @property
    def patient_id(self) -> Optional[str]:
        return self.context.get("patientId")

    @property
    def encounter_id(self) -> Optional[str]:
        return self.context.get("encounterId")

    @property
    def draft_orders(self) -> list[dict]:
        """For order-sign/order-select: the orders being signed."""
        orders_bundle = self.context.get("draftOrders", {})
        if isinstance(orders_bundle, dict):
            return [
                entry.get("resource", {})
                for entry in orders_bundle.get("entry", [])
            ]
        return []

    @property
    def medications_being_prescribed(self) -> list[str]:
        """Extract drug names from draft orders."""
        names = []
        for order in self.draft_orders:
            med_concept = order.get("medicationCodeableConcept", {})
            display = med_concept.get("text", "")
            if not display:
                for coding in med_concept.get("coding", []):
                    if coding.get("display"):
                        display = coding["display"]
                        break
            if display:
                names.append(display)
        return names

    @classmethod
    def from_json(cls, data: dict) -> "CDSHookRequest":
        """Parse a CDS Hooks request JSON body."""
        hook_str = data.get("hook", "")
        try:
            hook = CDSHookType(hook_str)
        except ValueError:
            hook = CDSHookType.PATIENT_VIEW

        return cls(
            hook=hook,
            hook_instance=data.get("hookInstance", str(uuid4())),
            fhir_server=data.get("fhirServer", ""),
            fhir_authorization=data.get("fhirAuthorization"),
            context=data.get("context", {}),
            prefetch=data.get("prefetch", {}),
        )


@dataclass
class FeedbackEntry:
    """Clinician feedback on a CDS card (override tracking for L7-16)."""
    card_uuid: str
    outcome: str            # "accepted" | "overridden"
    override_reason: Optional[str] = None
    clinician_id: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class CDSHooksService:
    """
    L7-2: CDS Hooks Service.
    
    Processes CDS Hook requests from EHRs and returns evidence-backed cards.
    
    Integration with CURANIQ pipeline:
    1. Parse CDS Hook request + prefetch data
    2. Map prefetch → FHIRPatientContext (via L7-3 gateway)
    3. Run CURANIQ pipeline (L3 CQL + L4 LLM + L5 safety gates)
    4. Map pipeline results → CDS Cards
    5. Log to L9-1 audit ledger
    
    This class handles steps 1, 4, 5. Steps 2-3 are delegated to the
    FHIR gateway and pipeline respectively.
    """

    def __init__(self) -> None:
        self._feedback_log: list[FeedbackEntry] = []

    def get_service_discovery(self) -> dict:
        """
        GET /cds-services
        Returns the service discovery document per CDS Hooks spec.
        """
        return {
            "services": [svc.to_dict() for svc in SERVICE_REGISTRY]
        }

    def get_service_by_id(self, service_id: str) -> Optional[CDSServiceDefinition]:
        """Look up a service definition by ID."""
        for svc in SERVICE_REGISTRY:
            if svc.service_id == service_id:
                return svc
        return None

    def build_cards_from_pipeline_result(
        self,
        pipeline_result: Any,
        hook_request: CDSHookRequest,
        draft_drug_names: Optional[list[str]] = None,
    ) -> list[CDSCard]:
        """
        Map CURANIQ pipeline output → CDS Cards.
        
        This is the bridge between CURANIQ's internal evidence model
        and the CDS Hooks card format that EHRs understand.
        
        Pipeline result contains:
        - claim_contract: verified claims with confidence scores
        - safety_suite: gate results (blocks, warnings, flags)
        - evidence_cards: structured evidence summaries
        - cql_results: deterministic medication safety outputs
        """
        cards: list[CDSCard] = []
        query_id = str(getattr(pipeline_result, "query_id", uuid4()))
        base_url = os.environ.get("CURANIQ_BASE_URL", "https://app.curaniq.com")

        # Card 1: CQL Safety Alerts (deterministic — highest priority)
        cql_cards = self._build_cql_safety_cards(pipeline_result, query_id, base_url)
        cards.extend(cql_cards)

        # Card 2: Claim-based evidence cards (from LLM + verification)
        evidence_cards = self._build_evidence_cards(pipeline_result, query_id, base_url)
        cards.extend(evidence_cards)

        # Card 3: Draft order specific checks (for order-sign / medication-prescribe)
        if draft_drug_names and hook_request.hook in (
            CDSHookType.ORDER_SIGN, CDSHookType.MEDICATION_PRESCRIBE
        ):
            order_cards = self._build_order_check_cards(
                pipeline_result, draft_drug_names, query_id, base_url
            )
            cards.extend(order_cards)

        # Card 4: Hard blocks from safety gates (L5)
        if hasattr(pipeline_result, "safety_suite") and pipeline_result.safety_suite:
            block_cards = self._build_safety_block_cards(
                pipeline_result, query_id, base_url
            )
            cards.extend(block_cards)

        return cards

    def _build_cql_safety_cards(
        self, result: Any, query_id: str, base_url: str
    ) -> list[CDSCard]:
        """Build cards from CQL deterministic safety outputs."""
        cards = []

        # Check for safety-critical CQL outputs
        if not hasattr(result, "cql_results"):
            return cards

        cql = getattr(result, "cql_results", None)
        if not cql or not isinstance(cql, dict):
            return cards

        # DDI alerts
        for ddi in cql.get("ddi_alerts", []):
            severity = ddi.get("severity", "moderate")
            indicator = (
                CardIndicator.CRITICAL if severity in ("major", "contraindicated")
                else CardIndicator.WARNING
            )
            cards.append(CDSCard(
                summary=f"Drug Interaction: {ddi.get('drug_a', '?')} + {ddi.get('drug_b', '?')}",
                detail=(
                    f"**{severity.upper()} interaction** — "
                    f"{ddi.get('description', 'See evidence for details.')}\n\n"
                    f"**Clinical significance:** {ddi.get('clinical_effect', 'Review required.')}\n\n"
                    f"*Source: CQL Deterministic Engine (L3-1)*"
                ),
                indicator=indicator,
                curaniq_query_id=query_id,
                curaniq_safety_flags=["DDI", severity],
                curaniq_confidence=1.0,  # Deterministic = 100% confidence
                links=[CDSLink(
                    label="View full evidence in CURANIQ",
                    url=f"{base_url}/evidence/{query_id}",
                )],
                override_reasons=[
                    {"code": "clinical-judgment", "display": "Clinical judgment — benefits outweigh risks"},
                    {"code": "already-monitoring", "display": "Already monitoring for this interaction"},
                    {"code": "will-adjust-dose", "display": "Will adjust dose accordingly"},
                ],
            ))

        # Renal dose adjustments
        for drug, adj in cql.get("renal_adjustments", {}).items():
            action = adj.get("action", "monitor")
            if action in ("reduce", "contraindicated", "avoid"):
                indicator = (
                    CardIndicator.CRITICAL if action == "contraindicated"
                    else CardIndicator.WARNING
                )
                cards.append(CDSCard(
                    summary=f"Renal Dose Adjustment: {drug} — {action.upper()}",
                    detail=(
                        f"**{drug}** requires dose adjustment based on renal function.\n\n"
                        f"**Action:** {adj.get('dose', 'See prescribing information')}\n\n"
                        f"**eGFR/CrCl:** {adj.get('egfr', 'N/A')} mL/min\n\n"
                        f"*Source: CQL Deterministic Engine (L3-1) — dose computed mathematically, not by AI*"
                    ),
                    indicator=indicator,
                    curaniq_query_id=query_id,
                    curaniq_safety_flags=["RENAL_DOSE"],
                    curaniq_confidence=1.0,
                ))

        # Allergy cross-reactivity
        for alert in cql.get("allergy_alerts", []):
            cards.append(CDSCard(
                summary=f"Allergy Alert: {alert.get('drug', '?')} — {alert.get('allergy', '?')}",
                detail=(
                    f"**Cross-reactivity risk** between {alert.get('drug', '?')} "
                    f"and documented allergy to {alert.get('allergy', '?')}.\n\n"
                    f"**Risk level:** {alert.get('risk', 'Review required')}\n\n"
                    f"*Source: CQL Allergy Kernel (L3-1)*"
                ),
                indicator=CardIndicator.CRITICAL,
                curaniq_query_id=query_id,
                curaniq_safety_flags=["ALLERGY"],
                curaniq_confidence=1.0,
            ))

        return cards

    def _build_evidence_cards(
        self, result: Any, query_id: str, base_url: str
    ) -> list[CDSCard]:
        """Build cards from evidence-backed claims."""
        cards = []
        evidence_cards_data = getattr(result, "evidence_cards", [])

        for ev_card in evidence_cards_data[:3]:  # Max 3 evidence cards
            if isinstance(ev_card, dict):
                summary = ev_card.get("summary", "Evidence available")[:140]
                detail = ev_card.get("detail", "")
                confidence = ev_card.get("confidence")
                evidence_ids = ev_card.get("evidence_ids", [])
            else:
                summary = str(ev_card)[:140]
                detail = ""
                confidence = None
                evidence_ids = []

            cards.append(CDSCard(
                summary=summary,
                detail=detail,
                indicator=CardIndicator.INFO,
                curaniq_query_id=query_id,
                curaniq_evidence_ids=evidence_ids,
                curaniq_confidence=confidence,
                links=[CDSLink(
                    label="View evidence details",
                    url=f"{base_url}/evidence/{query_id}",
                )],
            ))

        return cards

    def _build_order_check_cards(
        self, result: Any, draft_drugs: list[str], query_id: str, base_url: str
    ) -> list[CDSCard]:
        """Build cards specific to draft orders being signed."""
        cards = []

        # Check if any draft drug triggered safety flags
        safety_suite = getattr(result, "safety_suite", None)
        if safety_suite and hasattr(safety_suite, "gates"):
            failed_gates = [
                g for g in safety_suite.gates
                if not g.passed and g.message
            ]
            for gate in failed_gates[:2]:
                cards.append(CDSCard(
                    summary=f"Safety Gate: {gate.gate_name if hasattr(gate, 'gate_name') else 'Check failed'}",
                    detail=gate.message,
                    indicator=CardIndicator.WARNING,
                    curaniq_query_id=query_id,
                    curaniq_safety_flags=["SAFETY_GATE"],
                ))

        return cards

    def _build_safety_block_cards(
        self, result: Any, query_id: str, base_url: str
    ) -> list[CDSCard]:
        """Build hard-block cards from L5 safety gates."""
        cards = []
        safety_suite = getattr(result, "safety_suite", None)

        if safety_suite and getattr(safety_suite, "hard_block", False):
            cards.append(CDSCard(
                summary="CURANIQ Safety Block — Response withheld due to safety concern",
                detail=(
                    "The CURANIQ safety pipeline has withheld this response "
                    "because it could not be verified to meet clinical safety standards.\n\n"
                    "**Recommended action:** Consult official prescribing information "
                    "or clinical pharmacist.\n\n"
                    "*This is a fail-closed safety mechanism — CURANIQ refuses rather than risks harm.*"
                ),
                indicator=CardIndicator.CRITICAL,
                curaniq_query_id=query_id,
                curaniq_safety_flags=["HARD_BLOCK"],
            ))

        return cards

    def record_feedback(self, feedback: FeedbackEntry) -> None:
        """
        Record clinician feedback on a card.
        Per spec: POST /cds-services/{id}/feedback
        
        Feeds into L7-16 (Institutional Knowledge) for override pattern analysis
        and L10-8 (Clinician Feedback Loop Analytics).
        """
        self._feedback_log.append(feedback)
        logger.info(
            f"L7-2: CDS feedback: card={feedback.card_uuid[:8]}..., "
            f"outcome={feedback.outcome}, "
            f"reason={feedback.override_reason or 'none'}"
        )

    def get_override_stats(self) -> dict:
        """Override rate analytics for L7-16 and L10-11."""
        total = len(self._feedback_log)
        if total == 0:
            return {"total": 0, "override_rate": 0.0}

        overridden = sum(1 for f in self._feedback_log if f.outcome == "overridden")
        accepted = sum(1 for f in self._feedback_log if f.outcome == "accepted")

        # Group override reasons
        reasons: dict[str, int] = {}
        for f in self._feedback_log:
            if f.override_reason:
                reasons[f.override_reason] = reasons.get(f.override_reason, 0) + 1

        return {
            "total": total,
            "accepted": accepted,
            "overridden": overridden,
            "override_rate": round(overridden / total, 4) if total else 0.0,
            "top_override_reasons": dict(
                sorted(reasons.items(), key=lambda x: -x[1])[:5]
            ),
        }
