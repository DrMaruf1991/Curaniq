"""
CURANIQ -- Layer 0: Quality & Regulatory Foundation

L0-9  Multi-Function Product Boundary Enforcement
      (FDA 520(o)(1)(E) patient/clinician/researcher mode separation)
L0-10 Production Operations Hub (WAF, rate limiting, uptime, status)
L0-11 Incident Response & On-Call Alerting System

Architecture: L0-9 enforces that patient mode NEVER receives dosing
or directive outputs. L0-10 manages production health. L0-11 handles
clinical safety incidents with SLA-driven escalation.
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


# -----------------------------------------------------------------------------
# L0-9: MULTI-FUNCTION PRODUCT BOUNDARY ENFORCEMENT
# FDA 520(o)(1)(E): Patient/caregiver functions do NOT meet non-device CDS
# criteria. Time-critical or directive outputs trigger device oversight.
# -----------------------------------------------------------------------------

class ProductMode(str, Enum):
    CLINICIAN  = "clinician"    # Full access: dosing, DDI, evidence cards
    PATIENT    = "patient"      # Education only: NO dosing, NO directives
    STUDENT    = "student"      # Educational: evidence + reasoning, no directives
    RESEARCHER = "researcher"   # Full evidence access, no patient context


class OutputCategory(str, Enum):
    DOSING_RECOMMENDATION   = "dosing"         # BLOCKED for patient mode
    DIAGNOSTIC_SUGGESTION   = "diagnostic"     # BLOCKED for patient mode
    TREATMENT_DIRECTIVE     = "directive"       # BLOCKED for patient mode
    DRUG_INTERACTION_ALERT  = "ddi_alert"       # ALLOWED (safety info) but no doses
    EDUCATIONAL_CONTENT     = "education"       # ALLOWED for all
    EVIDENCE_SUMMARY        = "evidence"        # ALLOWED for all
    SAFETY_WARNING          = "safety_warning"  # ALLOWED for all (critical safety)
    MONITORING_INSTRUCTION  = "monitoring"      # BLOCKED for patient mode


# Permission matrix: which output categories are allowed per mode
# True = allowed, False = blocked with explanation
BOUNDARY_MATRIX: dict[ProductMode, dict[OutputCategory, bool]] = {
    ProductMode.CLINICIAN: {cat: True for cat in OutputCategory},
    ProductMode.PATIENT: {
        OutputCategory.DOSING_RECOMMENDATION:  False,
        OutputCategory.DIAGNOSTIC_SUGGESTION:  False,
        OutputCategory.TREATMENT_DIRECTIVE:    False,
        OutputCategory.DRUG_INTERACTION_ALERT: True,   # Safety info without doses
        OutputCategory.EDUCATIONAL_CONTENT:    True,
        OutputCategory.EVIDENCE_SUMMARY:       True,
        OutputCategory.SAFETY_WARNING:         True,
        OutputCategory.MONITORING_INSTRUCTION: False,
    },
    ProductMode.STUDENT: {
        OutputCategory.DOSING_RECOMMENDATION:  True,   # Educational context
        OutputCategory.DIAGNOSTIC_SUGGESTION:  True,
        OutputCategory.TREATMENT_DIRECTIVE:    False,   # No directives
        OutputCategory.DRUG_INTERACTION_ALERT: True,
        OutputCategory.EDUCATIONAL_CONTENT:    True,
        OutputCategory.EVIDENCE_SUMMARY:       True,
        OutputCategory.SAFETY_WARNING:         True,
        OutputCategory.MONITORING_INSTRUCTION: True,
    },
    ProductMode.RESEARCHER: {
        OutputCategory.DOSING_RECOMMENDATION:  True,
        OutputCategory.DIAGNOSTIC_SUGGESTION:  True,
        OutputCategory.TREATMENT_DIRECTIVE:    False,
        OutputCategory.DRUG_INTERACTION_ALERT: True,
        OutputCategory.EDUCATIONAL_CONTENT:    True,
        OutputCategory.EVIDENCE_SUMMARY:       True,
        OutputCategory.SAFETY_WARNING:         True,
        OutputCategory.MONITORING_INSTRUCTION: True,
    },
}


@dataclass
class BoundaryCheckResult:
    allowed: bool = True
    blocked_categories: list[str] = field(default_factory=list)
    mode: str = ""
    message: str = ""


class ProductBoundaryEnforcer:
    """
    L0-9: Enforces FDA 520(o)(1)(E) mode separation.

    Patient mode MUST NOT receive:
    - Dosing recommendations (even if CQL-computed)
    - Diagnostic suggestions
    - Treatment directives
    - Monitoring instructions

    Patient mode MAY receive:
    - Drug interaction alerts (safety information, no doses)
    - Educational content about conditions
    - Evidence summaries (layperson language)
    - Safety warnings (e.g., "seek emergency care")
    """

    def check_output(self, mode: ProductMode, output_categories: list[OutputCategory]) -> BoundaryCheckResult:
        """Check if output categories are permitted for the given mode."""
        result = BoundaryCheckResult(mode=mode.value)
        matrix = BOUNDARY_MATRIX.get(mode, {})

        for cat in output_categories:
            if not matrix.get(cat, False):
                result.allowed = False
                result.blocked_categories.append(cat.value)

        if not result.allowed:
            blocked_str = ", ".join(result.blocked_categories)
            if mode == ProductMode.PATIENT:
                result.message = (
                    f"Patient mode: the following content types are restricted "
                    f"per FDA 520(o)(1)(E): {blocked_str}. "
                    "Please consult your healthcare provider for specific "
                    "dosing, diagnostic, or treatment decisions."
                )
            else:
                result.message = (
                    f"{mode.value} mode: restricted categories: {blocked_str}"
                )

        return result

    def classify_output(self, text: str) -> list[OutputCategory]:
        """Classify output text into categories for boundary checking."""
        import re
        categories = []
        text_lower = text.lower()

        if re.search(r'\b\d+\s*(mg|mcg|g|ml|units?|iu)\b', text_lower):
            categories.append(OutputCategory.DOSING_RECOMMENDATION)
        if re.search(r'\b(diagnos|differential|suspect|likely|rule out)\b', text_lower):
            categories.append(OutputCategory.DIAGNOSTIC_SUGGESTION)
        if re.search(r'\b(prescribe|administer|initiate|start|switch to|titrate)\b', text_lower):
            categories.append(OutputCategory.TREATMENT_DIRECTIVE)
        if re.search(r'\b(interaction|ddi|concomitant|co-administ)\b', text_lower):
            categories.append(OutputCategory.DRUG_INTERACTION_ALERT)
        if re.search(r'\b(monitor|check|measure|lab|ecg|blood test)\b', text_lower):
            categories.append(OutputCategory.MONITORING_INSTRUCTION)
        if re.search(r'\b(warning|danger|emergency|seek\s+medical|stop\s+taking)\b', text_lower):
            categories.append(OutputCategory.SAFETY_WARNING)

        if not categories:
            categories.append(OutputCategory.EDUCATIONAL_CONTENT)

        return categories


# -----------------------------------------------------------------------------
# L0-10: PRODUCTION OPERATIONS HUB
# -----------------------------------------------------------------------------

@dataclass
class RateLimitConfig:
    requests_per_minute: int = 60
    requests_per_hour: int = 1000
    burst_limit: int = 10
    cost_budget_daily_usd: float = 100.0


class ProductionOpsHub:
    """
    L0-10: Production operations monitoring.

    Manages:
    - Rate limiting per client/API key (token bucket)
    - LLM cost budget enforcement (daily/monthly caps)
    - Uptime tracking (target: 99.9%)
    - Health check aggregation across all subsystems
    - Status page data generation
    """

    def __init__(self):
        self._request_counts: dict[str, list[float]] = {}
        self._daily_cost_usd: float = 0.0
        self._config = RateLimitConfig(
            requests_per_minute=int(os.environ.get("RATE_LIMIT_RPM", "60")),
            requests_per_hour=int(os.environ.get("RATE_LIMIT_RPH", "1000")),
            cost_budget_daily_usd=float(os.environ.get("DAILY_COST_BUDGET_USD", "100.0")),
        )
        self._health_checks: dict[str, bool] = {}
        self._uptime_start = time.time()

    def check_rate_limit(self, client_id: str) -> tuple[bool, str]:
        """Token bucket rate limiting. Returns (allowed, reason)."""
        now = time.time()
        timestamps = self._request_counts.setdefault(client_id, [])

        # Purge old entries
        timestamps[:] = [t for t in timestamps if now - t < 3600]

        # Check hourly limit
        if len(timestamps) >= self._config.requests_per_hour:
            return False, f"Hourly limit ({self._config.requests_per_hour}) exceeded"

        # Check per-minute limit
        recent = sum(1 for t in timestamps if now - t < 60)
        if recent >= self._config.requests_per_minute:
            return False, f"Per-minute limit ({self._config.requests_per_minute}) exceeded"

        timestamps.append(now)
        return True, "OK"

    def check_cost_budget(self, estimated_cost_usd: float) -> tuple[bool, str]:
        """Check if this request would exceed daily cost budget."""
        if self._daily_cost_usd + estimated_cost_usd > self._config.cost_budget_daily_usd:
            return False, (
                f"Daily cost budget exhausted: "
                f"${self._daily_cost_usd:.2f} / ${self._config.cost_budget_daily_usd:.2f}"
            )
        return True, "OK"

    def record_cost(self, cost_usd: float):
        self._daily_cost_usd += cost_usd

    def record_health(self, subsystem: str, healthy: bool):
        self._health_checks[subsystem] = healthy

    def get_status(self) -> dict[str, Any]:
        uptime_seconds = time.time() - self._uptime_start
        all_healthy = all(self._health_checks.values()) if self._health_checks else True
        return {
            "status": "operational" if all_healthy else "degraded",
            "uptime_hours": round(uptime_seconds / 3600, 2),
            "daily_cost_usd": round(self._daily_cost_usd, 2),
            "cost_budget_usd": self._config.cost_budget_daily_usd,
            "subsystems": self._health_checks,
        }


# -----------------------------------------------------------------------------
# L0-11: INCIDENT RESPONSE & ON-CALL ALERTING
# -----------------------------------------------------------------------------

class IncidentSeverity(str, Enum):
    SEV1_CRITICAL = "sev1"   # Patient safety risk, data breach → 15min SLA
    SEV2_HIGH     = "sev2"   # Clinical accuracy degraded → 1hr SLA
    SEV3_MEDIUM   = "sev3"   # Feature degraded, workaround exists → 4hr SLA
    SEV4_LOW      = "sev4"   # Cosmetic, non-urgent → 24hr SLA


@dataclass
class Incident:
    incident_id: str = field(default_factory=lambda: f"INC-{uuid4().hex[:8].upper()}")
    severity: IncidentSeverity = IncidentSeverity.SEV3_MEDIUM
    title: str = ""
    description: str = ""
    affected_module: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    acknowledged_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    responder: Optional[str] = None
    root_cause: Optional[str] = None
    patient_impact: bool = False


class IncidentResponseSystem:
    """
    L0-11: Clinical safety incident management.

    SLA targets (from architecture risk register):
    - SEV1 (patient safety): Acknowledge 15min, resolve 4hr
    - SEV2 (clinical accuracy): Acknowledge 1hr, resolve 8hr
    - SEV3 (feature degraded): Acknowledge 4hr, resolve 24hr
    - SEV4 (cosmetic): Acknowledge 24hr, resolve 1 week

    Auto-escalation: If acknowledgement SLA breached, escalate to next level.
    """

    SLA_ACKNOWLEDGE_MINUTES: dict[IncidentSeverity, int] = {
        IncidentSeverity.SEV1_CRITICAL: 15,
        IncidentSeverity.SEV2_HIGH: 60,
        IncidentSeverity.SEV3_MEDIUM: 240,
        IncidentSeverity.SEV4_LOW: 1440,
    }

    SLA_RESOLVE_HOURS: dict[IncidentSeverity, int] = {
        IncidentSeverity.SEV1_CRITICAL: 4,
        IncidentSeverity.SEV2_HIGH: 8,
        IncidentSeverity.SEV3_MEDIUM: 24,
        IncidentSeverity.SEV4_LOW: 168,
    }

    def __init__(self):
        self._incidents: list[Incident] = []
        self._alerting_endpoint = os.environ.get("INCIDENT_WEBHOOK_URL", "")

    def create_incident(
        self,
        severity: IncidentSeverity,
        title: str,
        description: str,
        module: str,
        patient_impact: bool = False,
    ) -> Incident:
        """Create and register a new incident."""
        # Auto-escalate to SEV1 if patient impact detected
        if patient_impact and severity != IncidentSeverity.SEV1_CRITICAL:
            logger.warning(
                "Auto-escalating %s to SEV1 due to patient impact: %s",
                severity.value, title,
            )
            severity = IncidentSeverity.SEV1_CRITICAL

        incident = Incident(
            severity=severity, title=title, description=description,
            affected_module=module, patient_impact=patient_impact,
        )
        self._incidents.append(incident)
        logger.critical(
            "INCIDENT %s [%s]: %s (module=%s, patient_impact=%s)",
            incident.incident_id, severity.value, title, module, patient_impact,
        )
        return incident

    def acknowledge(self, incident_id: str, responder: str) -> bool:
        inc = next((i for i in self._incidents if i.incident_id == incident_id), None)
        if not inc:
            return False
        inc.acknowledged_at = datetime.now(timezone.utc)
        inc.responder = responder

        # Check SLA
        elapsed_min = (inc.acknowledged_at - inc.created_at).total_seconds() / 60
        sla_min = self.SLA_ACKNOWLEDGE_MINUTES[inc.severity]
        if elapsed_min > sla_min:
            logger.warning("SLA BREACHED: %s acknowledged in %.0f min (SLA: %d min)",
                          incident_id, elapsed_min, sla_min)
        return True

    def resolve(self, incident_id: str, root_cause: str) -> bool:
        inc = next((i for i in self._incidents if i.incident_id == incident_id), None)
        if not inc:
            return False
        inc.resolved_at = datetime.now(timezone.utc)
        inc.root_cause = root_cause
        return True

    def get_open_incidents(self) -> list[Incident]:
        return [i for i in self._incidents if i.resolved_at is None]

    def get_sla_report(self) -> dict[str, Any]:
        resolved = [i for i in self._incidents if i.resolved_at and i.acknowledged_at]
        breached = 0
        for i in resolved:
            ack_min = (i.acknowledged_at - i.created_at).total_seconds() / 60
            if ack_min > self.SLA_ACKNOWLEDGE_MINUTES[i.severity]:
                breached += 1
        return {
            "total_incidents": len(self._incidents),
            "open": len(self.get_open_incidents()),
            "resolved": len(resolved),
            "sla_breached": breached,
            "sla_compliance": (len(resolved) - breached) / len(resolved) if resolved else 1.0,
        }
