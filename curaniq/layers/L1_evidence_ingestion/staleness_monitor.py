"""
CURANIQ — Medical Evidence Operating System
Layer 1: Evidence Ingestion

L1-4  Domain-Specific Staleness Scoring
L1-5  Staleness SLA Dashboard + Freshness Integrity
L1-16 Real-Time Evidence Monitor & Delta Detector

Architecture requirements:
- Drug safety alerts decay fast; landmark RCTs decay slow
- Public per-source timestamps: "PubMed: 2h ago. openFDA: 6h ago."
- Fail-closed for safety-critical sources when TTL expires
- Delta detection: flag ALL recent responses referencing superseded evidence
- Safety-critical deltas trigger IMMEDIATE clinical governance board alert
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Optional

from curaniq.models.evidence import (
    EvidenceChunk,
    EvidenceTier,
    FAIL_CLOSED_SOURCES,
    Jurisdiction,
    RetractionStatus,
    SourceAPI,
    StalenessStatus,
    STALENESS_TTL_HOURS,
)

logger = logging.getLogger(__name__)

PIPELINE_VERSION = "1.0.0"


# ─────────────────────────────────────────────────────────────────────────────
# L1-4: DOMAIN-SPECIFIC STALENESS SCORING
# Different clinical domains decay at different rates.
# Drug safety alerts: hours. Landmark RCTs: years.
# ─────────────────────────────────────────────────────────────────────────────

class ClinicalDomain(str, Enum):
    """Clinical domain for staleness decay profile selection."""
    DRUG_SAFETY_ALERT       = "drug_safety_alert"       # Black box, recalls, FAERS signals
    DRUG_LABEL              = "drug_label"              # Dosing, contraindications, interactions
    CLINICAL_GUIDELINE      = "clinical_guideline"      # NICE, AHA/ACC, WHO guidelines
    SYSTEMATIC_REVIEW       = "systematic_review"       # Cochrane, meta-analyses
    RCT                     = "rct"                     # Randomised controlled trials
    PHARMACOVIGILANCE       = "pharmacovigilance"       # FAERS, EudraVigilance signals
    DRUG_INTERACTION        = "drug_interaction"        # DDI databases
    PREGNANCY_LACTATION     = "pregnancy_lactation"     # LactMed, teratogenicity
    RETRACTION              = "retraction"              # Crossref, Retraction Watch
    PEDIATRIC_DOSING        = "pediatric_dosing"        # Age/weight-based dosing
    ONCOLOGY_PROTOCOL       = "oncology_protocol"       # Chemotherapy protocols (volatile)
    ANTIBIOTIC_RESISTANCE   = "antibiotic_resistance"   # Local resistance patterns (very volatile)
    VACCINATION             = "vaccination"             # Immunisation schedules
    LANDMARK_TRIAL          = "landmark_trial"          # UKPDS, ACCORD, SPRINT (stable)


# Domain-specific TTL override in hours
# Overrides source-level TTL with content-aware decay profiles
DOMAIN_TTL_HOURS: dict[ClinicalDomain, float] = {
    ClinicalDomain.DRUG_SAFETY_ALERT:       1.0,     # 1 hour — immediate patient safety
    ClinicalDomain.RETRACTION:              0.5,     # 30 min — patient safety critical
    ClinicalDomain.PHARMACOVIGILANCE:       6.0,     # 6 hours — emerging signals
    ClinicalDomain.DRUG_LABEL:              24.0,    # 24 hours — labelling changes
    ClinicalDomain.DRUG_INTERACTION:        24.0,    # 24 hours — DDI updates
    ClinicalDomain.ANTIBIOTIC_RESISTANCE:   24.0,    # 24 hours — local resistance volatile
    ClinicalDomain.PREGNANCY_LACTATION:     168.0,   # 7 days — LactMed updates
    ClinicalDomain.PEDIATRIC_DOSING:        168.0,   # 7 days — BNFc updates
    ClinicalDomain.ONCOLOGY_PROTOCOL:       168.0,   # 7 days — protocol versions
    ClinicalDomain.VACCINATION:             168.0,   # 7 days — schedule changes
    ClinicalDomain.CLINICAL_GUIDELINE:      720.0,   # 30 days — NICE/AHA/ACC cycles
    ClinicalDomain.SYSTEMATIC_REVIEW:       2160.0,  # 90 days — Cochrane update cycles
    ClinicalDomain.RCT:                     8760.0,  # 1 year — trial data stable
    ClinicalDomain.LANDMARK_TRIAL:          43800.0, # 5 years — UKPDS-level evidence stable
}

# Whether domain is fail-closed (TTL expiry → REFUSE, not just WARN)
DOMAIN_FAIL_CLOSED: set[ClinicalDomain] = {
    ClinicalDomain.DRUG_SAFETY_ALERT,
    ClinicalDomain.RETRACTION,
    ClinicalDomain.DRUG_LABEL,
    ClinicalDomain.DRUG_INTERACTION,
}


def classify_domain(chunk: EvidenceChunk) -> ClinicalDomain:
    """
    Classify evidence chunk into clinical domain for decay profile selection.
    Uses evidence tier + source API + content keywords.
    """
    content_lower = chunk.content.lower()
    source = chunk.provenance.source_api

    # Retraction signals — always highest priority
    if source in (SourceAPI.RETRACTION_WATCH, SourceAPI.CROSSREF):
        return ClinicalDomain.RETRACTION

    # FAERS/pharmacovigilance signals
    if source == SourceAPI.OPENFDA_FAERS:
        return ClinicalDomain.PHARMACOVIGILANCE

    # Black box / safety alerts from FDA
    if source == SourceAPI.OPENFDA_LABELS:
        if any(kw in content_lower for kw in ["boxed_warning", "black box", "recall", "safety alert"]):
            return ClinicalDomain.DRUG_SAFETY_ALERT
        return ClinicalDomain.DRUG_LABEL

    # DailyMed drug labels
    if source == SourceAPI.DAILYMED_SPL:
        return ClinicalDomain.DRUG_LABEL

    # LactMed
    if source == SourceAPI.LACTMED:
        return ClinicalDomain.PREGNANCY_LACTATION

    # NICE guidelines
    if source == SourceAPI.NICE_GUIDELINES:
        return ClinicalDomain.CLINICAL_GUIDELINE

    # PubMed — classify by evidence tier and content
    if source == SourceAPI.PUBMED:
        if chunk.evidence_tier == EvidenceTier.SYSTEMATIC_REVIEW:
            return ClinicalDomain.SYSTEMATIC_REVIEW
        if chunk.evidence_tier == EvidenceTier.RCT:
            # Landmark trial detection (UKPDS, ACCORD, SPRINT, HOPE, etc.)
            landmark_trials = [
                "ukpds", "accord", "sprint", "advance", "hope", "solvd",
                "charm", "emphasis", "paradigm", "dapa-hf", "emperor",
                "canvas", "declare", "credence", "dapa-ckd",
            ]
            if any(trial in content_lower for trial in landmark_trials):
                return ClinicalDomain.LANDMARK_TRIAL
            return ClinicalDomain.RCT
        if any(kw in content_lower for kw in ["antibiotic", "antimicrobial", "resistance", "susceptibility"]):
            return ClinicalDomain.ANTIBIOTIC_RESISTANCE
        if any(kw in content_lower for kw in ["chemotherapy", "oncology", "cancer protocol", "regimen"]):
            return ClinicalDomain.ONCOLOGY_PROTOCOL
        if any(kw in content_lower for kw in ["pregnancy", "lactation", "breastfeeding", "teratogen"]):
            return ClinicalDomain.PREGNANCY_LACTATION
        if any(kw in content_lower for kw in ["pediatric", "paediatric", "child", "neonatal", "infant"]):
            return ClinicalDomain.PEDIATRIC_DOSING
        if chunk.evidence_tier == EvidenceTier.GUIDELINE:
            return ClinicalDomain.CLINICAL_GUIDELINE

    return ClinicalDomain.CLINICAL_GUIDELINE  # Conservative default


def compute_domain_staleness(chunk: EvidenceChunk) -> StalenessStatus:
    """
    L1-4: Compute domain-specific staleness status.
    More granular than source-level TTL — accounts for clinical domain decay rates.
    """
    if not chunk.last_verified:
        return StalenessStatus.UNKNOWN

    domain = classify_domain(chunk)
    ttl_hours = DOMAIN_TTL_HOURS[domain]

    lv = chunk.last_verified
    if lv.tzinfo is None:
        lv = lv.replace(tzinfo=timezone.utc)

    age_hours = (datetime.now(timezone.utc) - lv).total_seconds() / 3600

    if age_hours <= ttl_hours:
        return StalenessStatus.FRESH

    # Expired — check if fail-closed
    if domain in DOMAIN_FAIL_CLOSED:
        return StalenessStatus.CRITICAL  # REFUSE

    return StalenessStatus.STALE  # WARN


# ─────────────────────────────────────────────────────────────────────────────
# L1-5: STALENESS SLA DASHBOARD + FRESHNESS INTEGRITY
# Public per-source timestamps. Red badge when stale. Fail-closed for critical.
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SourceSLAState:
    """
    State tracking for a single evidence source per L1-5 SLA Dashboard.
    Immutable snapshot — regenerated on every dashboard render.
    """
    source: SourceAPI
    last_successful_fetch: Optional[datetime]
    last_attempted_fetch: Optional[datetime]
    is_reachable: bool
    staleness_status: StalenessStatus
    ttl_hours: float
    age_hours: Optional[float]           # Hours since last successful fetch
    display_text: str                    # e.g., "PubMed: 2h ago"
    badge_color: str                     # "green" | "amber" | "red"
    fail_closed: bool                    # True = REFUSE when stale
    next_scheduled_fetch: Optional[datetime]
    consecutive_failures: int = 0


class StalenessSLADashboard:
    """
    L1-5: Per-source SLA tracking with public timestamps.
    
    Architecture: 'Public per-source timestamps. Source-unreachable detection.
    Last-successful-ingest tracking. Fail-closed for high-risk (recalls,
    black-box warnings). Red badge when stale.'
    
    This is a singleton that tracks fetch state for all governed sources.
    """

    def __init__(self) -> None:
        # Last successful fetch per source
        self._last_fetch: dict[SourceAPI, datetime] = {}
        self._last_attempt: dict[SourceAPI, datetime] = {}
        self._reachable: dict[SourceAPI, bool] = {s: True for s in SourceAPI}
        self._consecutive_failures: dict[SourceAPI, int] = {s: 0 for s in SourceAPI}
        self._alert_callbacks: list[Callable[[SourceAPI, str], None]] = []

    def record_successful_fetch(self, source: SourceAPI) -> None:
        """Record a successful fetch from a source."""
        now = datetime.now(timezone.utc)
        self._last_fetch[source] = now
        self._last_attempt[source] = now
        self._reachable[source] = True
        self._consecutive_failures[source] = 0
        logger.debug(f"SLA Dashboard: {source.value} — successful fetch at {now.isoformat()}")

    def record_failed_fetch(self, source: SourceAPI, error: str) -> None:
        """Record a failed fetch attempt. After 3 consecutive failures → source unreachable."""
        now = datetime.now(timezone.utc)
        self._last_attempt[source] = now
        self._consecutive_failures[source] = self._consecutive_failures.get(source, 0) + 1

        if self._consecutive_failures[source] >= 3:
            self._reachable[source] = False
            logger.warning(
                f"SLA Dashboard: {source.value} — UNREACHABLE "
                f"({self._consecutive_failures[source]} consecutive failures). Error: {error}"
            )
            self._trigger_alert(source, f"SOURCE UNREACHABLE: {source.value} — {error}")

    def get_source_state(self, source: SourceAPI) -> SourceSLAState:
        """Get the current SLA state for a single source."""
        ttl = STALENESS_TTL_HOURS.get(source, 24.0)
        last_fetch = self._last_fetch.get(source)
        last_attempt = self._last_attempt.get(source)
        is_reachable = self._reachable.get(source, True)

        if not last_fetch:
            return SourceSLAState(
                source=source,
                last_successful_fetch=None,
                last_attempted_fetch=last_attempt,
                is_reachable=is_reachable,
                staleness_status=StalenessStatus.UNKNOWN,
                ttl_hours=ttl,
                age_hours=None,
                display_text=f"{source.value}: never fetched",
                badge_color="red",
                fail_closed=source in FAIL_CLOSED_SOURCES,
                next_scheduled_fetch=None,
                consecutive_failures=self._consecutive_failures.get(source, 0),
            )

        lf = last_fetch
        if lf.tzinfo is None:
            lf = lf.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        age_hours = (now - lf).total_seconds() / 3600

        if not is_reachable:
            status = StalenessStatus.CRITICAL if source in FAIL_CLOSED_SOURCES else StalenessStatus.STALE
            badge = "red"
        elif age_hours <= ttl:
            status = StalenessStatus.FRESH
            badge = "green"
        elif source in FAIL_CLOSED_SOURCES:
            status = StalenessStatus.CRITICAL
            badge = "red"
        else:
            status = StalenessStatus.STALE
            badge = "amber"

        # Display text: "PubMed: 2h ago" / "openFDA: 45m ago" / "NICE: 4d ago"
        if age_hours < 1:
            age_str = f"{int(age_hours * 60)}m ago"
        elif age_hours < 48:
            age_str = f"{int(age_hours)}h ago"
        else:
            age_str = f"{int(age_hours / 24)}d ago"

        # ⚠ Red badge when stale
        stale_indicator = " ⚠ STALE" if status != StalenessStatus.FRESH else ""
        display = f"{source.value}: {age_str}{stale_indicator}"

        # Next scheduled fetch
        next_fetch = lf + timedelta(hours=ttl)

        return SourceSLAState(
            source=source,
            last_successful_fetch=last_fetch,
            last_attempted_fetch=last_attempt,
            is_reachable=is_reachable,
            staleness_status=status,
            ttl_hours=ttl,
            age_hours=age_hours,
            display_text=display,
            badge_color=badge,
            fail_closed=source in FAIL_CLOSED_SOURCES,
            next_scheduled_fetch=next_fetch,
            consecutive_failures=self._consecutive_failures.get(source, 0),
        )

    def render_dashboard(self) -> dict[str, Any]:
        """
        Render the full SLA dashboard for all governed sources.
        Returns structured data suitable for API response + frontend display.
        """
        sources_status = {}
        critical_sources = []
        stale_sources = []
        fresh_sources = []

        for source in SourceAPI:
            state = self.get_source_state(source)
            sources_status[source.value] = {
                "display": state.display_text,
                "badge": state.badge_color,
                "status": state.staleness_status.value,
                "last_fetch": state.last_successful_fetch.isoformat() if state.last_successful_fetch else None,
                "age_hours": round(state.age_hours, 2) if state.age_hours else None,
                "ttl_hours": state.ttl_hours,
                "is_reachable": state.is_reachable,
                "fail_closed": state.fail_closed,
                "next_fetch": state.next_scheduled_fetch.isoformat() if state.next_scheduled_fetch else None,
                "consecutive_failures": state.consecutive_failures,
            }

            if state.staleness_status == StalenessStatus.CRITICAL:
                critical_sources.append(source.value)
            elif state.staleness_status == StalenessStatus.STALE:
                stale_sources.append(source.value)
            elif state.staleness_status == StalenessStatus.FRESH:
                fresh_sources.append(source.value)

        # Human-readable summary line
        summary = " | ".join([
            sources_status[s]["display"]
            for s in [
                SourceAPI.PUBMED.value,
                SourceAPI.OPENFDA_LABELS.value,
                SourceAPI.NICE_GUIDELINES.value,
                SourceAPI.COCHRANE.value,
            ]
            if s in sources_status
        ])

        return {
            "summary": summary,
            "sources": sources_status,
            "critical_count": len(critical_sources),
            "stale_count": len(stale_sources),
            "fresh_count": len(fresh_sources),
            "critical_sources": critical_sources,
            "stale_sources": stale_sources,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "pipeline_version": PIPELINE_VERSION,
        }

    def is_source_usable(self, source: SourceAPI) -> tuple[bool, str]:
        """
        L1-5 fail-closed check: Can this source be used right now?
        Returns (usable, reason).
        """
        state = self.get_source_state(source)

        if not state.is_reachable:
            if source in FAIL_CLOSED_SOURCES:
                return False, (
                    f"{source.value} is UNREACHABLE. "
                    f"Fail-closed: cannot serve safety-critical data from unreachable source. "
                    f"Last successful fetch: {state.last_successful_fetch}"
                )
            return True, f"WARNING: {source.value} unreachable — using cached data (age: {state.age_hours:.1f}h)"

        if state.staleness_status == StalenessStatus.CRITICAL:
            return False, (
                f"{source.value} TTL EXPIRED (age: {state.age_hours:.1f}h, TTL: {state.ttl_hours}h). "
                f"Fail-closed: REFUSING to serve safety-critical stale data."
            )

        if state.staleness_status == StalenessStatus.STALE:
            return True, f"WARNING: {source.value} is stale (age: {state.age_hours:.1f}h, TTL: {state.ttl_hours}h)"

        return True, "OK"

    def register_alert_callback(self, callback: Callable[[SourceAPI, str], None]) -> None:
        """Register a callback for staleness/unreachability alerts."""
        self._alert_callbacks.append(callback)

    def _trigger_alert(self, source: SourceAPI, message: str) -> None:
        for cb in self._alert_callbacks:
            try:
                cb(source, message)
            except Exception as e:
                logger.error(f"Alert callback error: {e}")


# Singleton dashboard instance
_sla_dashboard: Optional[StalenessSLADashboard] = None


def get_sla_dashboard() -> StalenessSLADashboard:
    """Get or create the singleton SLA dashboard."""
    global _sla_dashboard
    if _sla_dashboard is None:
        _sla_dashboard = StalenessSLADashboard()
    return _sla_dashboard


# ─────────────────────────────────────────────────────────────────────────────
# L1-16: REAL-TIME EVIDENCE MONITOR & DELTA DETECTOR
# Continuous evidence freshness pipeline.
# (A) Scheduled polling, (B) Delta detection, (C) Staleness fail-closed
# ─────────────────────────────────────────────────────────────────────────────

class DeltaSeverity(str, Enum):
    """Severity of an evidence delta per L1-16 architecture."""
    CRITICAL    = "critical"    # New Black Box warning, new contraindication, retraction
    HIGH        = "high"        # New serious adverse event, major dose change
    MODERATE    = "moderate"    # Label update, guideline revision
    LOW         = "low"         # Minor wording update, new study supporting existing guidance
    INFORMATIONAL = "informational"  # New supporting evidence, no change to recommendations


@dataclass
class EvidenceDelta:
    """
    A detected change between a previous and current version of evidence.
    Created by L1-16 Delta Detector.
    """
    delta_id: str
    source: SourceAPI
    document_id: str                     # Parent document identifier
    delta_type: str                      # "new_black_box", "new_contraindication", "retraction", etc.
    severity: DeltaSeverity
    previous_version_hash: Optional[str]
    current_version_hash: str
    previous_content_summary: Optional[str]
    current_content_summary: str
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Impact tracking
    affected_response_ids: list[str] = field(default_factory=list)  # UUIDs of cached responses
    governance_alert_sent: bool = False
    superseded_pack_ids: list[str] = field(default_factory=list)

    # Full content for audit
    previous_content: Optional[str] = None
    current_content: Optional[str] = None


@dataclass
class ScheduledPollConfig:
    """
    Polling schedule per source per L1-16 architecture spec.
    """
    source: SourceAPI
    interval_hours: float        # How often to poll
    priority: int                # 1 = highest priority
    use_webhook: bool = False    # True = event-driven, False = scheduled poll
    webhook_url: Optional[str] = None


# Polling schedule from architecture spec
POLL_SCHEDULE: list[ScheduledPollConfig] = [
    ScheduledPollConfig(SourceAPI.OPENFDA_LABELS,    0.25,   1, use_webhook=True),   # FDA Safety: 15min + RSS
    ScheduledPollConfig(SourceAPI.OPENFDA_FAERS,     168.0,  3),                      # FAERS: weekly
    ScheduledPollConfig(SourceAPI.DAILYMED_SPL,      24.0,   2),                      # Drug Labels: daily
    ScheduledPollConfig(SourceAPI.PUBMED,             6.0,   2),                      # PubMed: 4x/day
    ScheduledPollConfig(SourceAPI.CROSSREF,           0.0,   1, use_webhook=True),    # Retractions: real-time
    ScheduledPollConfig(SourceAPI.CLINICAL_TRIALS,    6.0,   2),                      # ClinicalTrials: 6h
    ScheduledPollConfig(SourceAPI.NICE_GUIDELINES,   24.0,   2),                      # NICE: daily
    ScheduledPollConfig(SourceAPI.EMA_EPAR,          48.0,   3),                      # EMA: 48h
    ScheduledPollConfig(SourceAPI.LACTMED,           168.0,  3),                      # LactMed: weekly
    ScheduledPollConfig(SourceAPI.COCHRANE,          168.0,  3),                      # Cochrane: weekly
    ScheduledPollConfig(SourceAPI.UZ_MOH,           2160.0,  4),                      # UZ MOH: 90 days
    ScheduledPollConfig(SourceAPI.RUSSIAN_MINZDRAV,  720.0,  4),                      # Minzdrav: 30 days
    ScheduledPollConfig(SourceAPI.RXNORM,            720.0,  4),                      # RxNorm: monthly
]

# Delta types that trigger IMMEDIATE governance board alert
CRITICAL_DELTA_TYPES = {
    "new_black_box_warning",
    "new_contraindication",
    "retraction",
    "safety_recall",
    "market_withdrawal",
    "dose_error_signal",
    "new_fatal_interaction",
}


class EvidenceDeltaDetector:
    """
    L1-16B: Delta detection between evidence versions.
    
    When new evidence arrives:
    1. Diff against previous version
    2. Extract clinical deltas (changed recommendations, new warnings, etc.)
    3. Flag all recent responses referencing superseded version
    4. Trigger governance alert for safety-critical deltas
    """

    def __init__(self) -> None:
        # Document version registry: {document_id: (version_hash, content)}
        self._version_registry: dict[str, tuple[str, str]] = {}
        # Delta history
        self._delta_history: list[EvidenceDelta] = []
        # Governance alert callbacks
        self._governance_callbacks: list[Callable[[EvidenceDelta], None]] = []
        # Response tracking: {response_id: set of document_ids cited}
        self._response_citations: dict[str, set[str]] = {}

    def register_document(
        self,
        document_id: str,
        content: str,
        version_hash: Optional[str] = None,
    ) -> str:
        """Register a document version. Returns the content hash."""
        computed_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        hash_to_store = version_hash or computed_hash
        self._version_registry[document_id] = (hash_to_store, content)
        return hash_to_store

    def detect_delta(
        self,
        document_id: str,
        new_content: str,
        source: SourceAPI,
    ) -> Optional[EvidenceDelta]:
        """
        Compare new content against registered version.
        Returns EvidenceDelta if change detected, None if unchanged.
        """
        new_hash = hashlib.sha256(new_content.encode("utf-8")).hexdigest()

        if document_id not in self._version_registry:
            # First time seeing this document — register it
            self.register_document(document_id, new_content, new_hash)
            return None

        prev_hash, prev_content = self._version_registry[document_id]

        if prev_hash == new_hash:
            return None  # No change

        # Change detected — analyse delta
        delta_type, severity = self._classify_delta(prev_content, new_content, source)

        import uuid
        delta = EvidenceDelta(
            delta_id=str(uuid.uuid4()),
            source=source,
            document_id=document_id,
            delta_type=delta_type,
            severity=severity,
            previous_version_hash=prev_hash,
            current_version_hash=new_hash,
            previous_content_summary=prev_content[:500] if prev_content else None,
            current_content_summary=new_content[:500],
            previous_content=prev_content,
            current_content=new_content,
        )

        # Find affected responses
        delta.affected_response_ids = [
            resp_id for resp_id, cited_docs in self._response_citations.items()
            if document_id in cited_docs
        ]
        delta.superseded_pack_ids = list(delta.affected_response_ids)

        # Store new version
        self.register_document(document_id, new_content, new_hash)
        self._delta_history.append(delta)

        logger.info(
            f"DELTA DETECTED: {document_id} [{delta_type}] "
            f"severity={severity.value}, affects {len(delta.affected_response_ids)} responses"
        )

        # Trigger governance alert for critical deltas
        if severity == DeltaSeverity.CRITICAL or delta_type in CRITICAL_DELTA_TYPES:
            delta.governance_alert_sent = True
            self._trigger_governance_alert(delta)

        return delta

    def _classify_delta(
        self,
        prev_content: str,
        new_content: str,
        source: SourceAPI,
    ) -> tuple[str, DeltaSeverity]:
        """
        Classify the type and severity of a delta using keyword analysis.
        Production: use NLP diff + clinical NER for precise classification.
        """
        new_lower = new_content.lower()
        prev_lower = prev_content.lower()

        # Critical: New Black Box warning
        if "black box" in new_lower and "black box" not in prev_lower:
            return "new_black_box_warning", DeltaSeverity.CRITICAL

        if "boxed warning" in new_lower and "boxed warning" not in prev_lower:
            return "new_black_box_warning", DeltaSeverity.CRITICAL

        # Critical: New contraindication
        new_contraindications = set()
        prev_contraindications = set()
        _contra_keywords = ["contraindicated", "do not use", "must not", "prohibited in"]
        for kw in _contra_keywords:
            if kw in new_lower and kw not in prev_lower:
                new_contraindications.add(kw)
        if new_contraindications:
            return "new_contraindication", DeltaSeverity.CRITICAL

        # Critical: Retraction signals
        if "retract" in new_lower:
            return "retraction", DeltaSeverity.CRITICAL

        # Critical: Market withdrawal
        if any(kw in new_lower for kw in ["market withdrawal", "recalled", "pull from market"]):
            return "safety_recall", DeltaSeverity.CRITICAL

        # High: Serious adverse event (new)
        serious_ae_terms = ["fatal", "life-threatening", "severe hepatotoxicity", "serotonin syndrome",
                            "torsades de pointes", "anaphylaxis", "aplastic anaemia"]
        for term in serious_ae_terms:
            if term in new_lower and term not in prev_lower:
                return "new_serious_adverse_event", DeltaSeverity.HIGH

        # High: Dose change
        if any(kw in new_lower for kw in ["dose reduction", "dose adjustment", "maximum dose", "loading dose"]):
            if source in (SourceAPI.OPENFDA_LABELS, SourceAPI.DAILYMED_SPL, SourceAPI.NICE_GUIDELINES):
                return "dose_change", DeltaSeverity.HIGH

        # High: New fatal interaction
        if "fatal" in new_lower and "interaction" in new_lower and "fatal" not in prev_lower:
            return "new_fatal_interaction", DeltaSeverity.HIGH

        # Moderate: General label update
        if source in (SourceAPI.OPENFDA_LABELS, SourceAPI.DAILYMED_SPL):
            return "label_update", DeltaSeverity.MODERATE

        # Moderate: Guideline revision
        if source in (SourceAPI.NICE_GUIDELINES, SourceAPI.EMA_EPAR):
            return "guideline_revision", DeltaSeverity.MODERATE

        # Low: New supporting study
        if source == SourceAPI.PUBMED:
            return "new_publication", DeltaSeverity.LOW

        return "content_update", DeltaSeverity.INFORMATIONAL

    def track_response_citations(self, response_id: str, document_ids: set[str]) -> None:
        """
        Track which documents were cited in a response.
        Enables delta detection to flag affected past responses.
        """
        self._response_citations[response_id] = document_ids

    def get_deltas_since(
        self,
        since: datetime,
        min_severity: DeltaSeverity = DeltaSeverity.MODERATE,
    ) -> list[EvidenceDelta]:
        """Get all deltas since a given datetime at or above minimum severity."""
        severity_order = {
            DeltaSeverity.INFORMATIONAL: 0,
            DeltaSeverity.LOW: 1,
            DeltaSeverity.MODERATE: 2,
            DeltaSeverity.HIGH: 3,
            DeltaSeverity.CRITICAL: 4,
        }
        threshold = severity_order[min_severity]

        return [
            d for d in self._delta_history
            if d.detected_at >= since
            and severity_order.get(d.severity, 0) >= threshold
        ]

    def get_critical_deltas_pending_review(self) -> list[EvidenceDelta]:
        """Return critical deltas that have not been reviewed by governance."""
        return [
            d for d in self._delta_history
            if d.severity == DeltaSeverity.CRITICAL
            and len(d.affected_response_ids) > 0
        ]

    def register_governance_callback(
        self, callback: Callable[[EvidenceDelta], None]
    ) -> None:
        """Register callback invoked on CRITICAL delta detection."""
        self._governance_callbacks.append(callback)

    def _trigger_governance_alert(self, delta: EvidenceDelta) -> None:
        """
        Trigger immediate governance board alert for safety-critical deltas.
        Per architecture: 'immediate clinical governance board alert,
        old evidence pack marked SUPERSEDED, affected responses queued for review.'
        """
        logger.critical(
            f"GOVERNANCE ALERT: {delta.delta_type.upper()} — {delta.document_id} "
            f"[{delta.source.value}]. "
            f"Affects {len(delta.affected_response_ids)} past responses. "
            f"Response IDs: {delta.affected_response_ids[:10]}"
        )
        for cb in self._governance_callbacks:
            try:
                cb(delta)
            except Exception as e:
                logger.error(f"Governance callback error: {e}")


class RealTimeEvidenceMonitor:
    """
    L1-16: Full real-time evidence monitor.
    
    Integrates:
    - Scheduled polling orchestrator (A)
    - Delta detector (B)
    - Staleness fail-closed enforcer (C)
    - SLA dashboard (L1-5)
    
    This is the central coordinator for evidence freshness across CURANIQ.
    In production: runs as a background service with asyncio event loop.
    """

    def __init__(
        self,
        sla_dashboard: Optional[StalenessSLADashboard] = None,
    ) -> None:
        self.sla_dashboard = sla_dashboard or get_sla_dashboard()
        self.delta_detector = EvidenceDeltaDetector()
        self._poll_tasks: dict[SourceAPI, asyncio.Task] = {}
        self._running = False

        # Register governance alert logger
        self.delta_detector.register_governance_callback(self._log_governance_alert)

    def _log_governance_alert(self, delta: EvidenceDelta) -> None:
        """Default governance alert handler — log + escalate."""
        alert = {
            "alert_type": "EVIDENCE_DELTA_CRITICAL",
            "delta_id": delta.delta_id,
            "source": delta.source.value,
            "document_id": delta.document_id,
            "delta_type": delta.delta_type,
            "severity": delta.severity.value,
            "affected_responses": len(delta.affected_response_ids),
            "detected_at": delta.detected_at.isoformat(),
            "action_required": "REVIEW ALL AFFECTED RESPONSES — evidence pack marked SUPERSEDED",
        }
        logger.critical(f"GOVERNANCE ALERT: {json.dumps(alert, indent=2)}")

    async def process_new_evidence(
        self,
        document_id: str,
        content: str,
        source: SourceAPI,
    ) -> Optional[EvidenceDelta]:
        """
        Process newly fetched evidence: detect deltas, update dashboard.
        Called by API connectors after each successful fetch.
        """
        self.sla_dashboard.record_successful_fetch(source)
        delta = self.delta_detector.detect_delta(document_id, content, source)

        if delta and delta.severity in (DeltaSeverity.CRITICAL, DeltaSeverity.HIGH):
            logger.warning(
                f"Evidence delta [{delta.severity.value}]: {document_id} — {delta.delta_type}"
            )

        return delta

    async def _poll_source_loop(
        self,
        config: ScheduledPollConfig,
        fetch_fn: Callable[[], Any],
    ) -> None:
        """Background polling loop for a single source."""
        while self._running:
            try:
                logger.debug(f"Polling {config.source.value}...")
                await fetch_fn()
                await asyncio.sleep(config.interval_hours * 3600)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.sla_dashboard.record_failed_fetch(config.source, str(e))
                # Exponential backoff on failure
                backoff = min(config.interval_hours * 3600, 3600)
                await asyncio.sleep(backoff)

    def get_staleness_summary(self) -> str:
        """
        Generate the public-facing staleness summary string.
        Example: "PubMed: 2h ago | openFDA: 45m ago | NICE: 4d ago"
        """
        return self.sla_dashboard.render_dashboard()["summary"]

    def get_full_dashboard(self) -> dict[str, Any]:
        """Get the full SLA dashboard state."""
        return self.sla_dashboard.render_dashboard()

    def check_source_usability(self, source: SourceAPI) -> tuple[bool, str]:
        """Check if a source is usable — delegates to SLA dashboard."""
        return self.sla_dashboard.is_source_usable(source)

    def get_pending_governance_alerts(self) -> list[dict[str, Any]]:
        """Return all critical deltas awaiting governance review."""
        deltas = self.delta_detector.get_critical_deltas_pending_review()
        return [
            {
                "delta_id": d.delta_id,
                "source": d.source.value,
                "document_id": d.document_id,
                "delta_type": d.delta_type,
                "severity": d.severity.value,
                "detected_at": d.detected_at.isoformat(),
                "affected_response_count": len(d.affected_response_ids),
                "governance_alert_sent": d.governance_alert_sent,
            }
            for d in deltas
        ]


# Module-level singleton monitor
_monitor: Optional[RealTimeEvidenceMonitor] = None


def get_evidence_monitor() -> RealTimeEvidenceMonitor:
    """Get or create the singleton RealTimeEvidenceMonitor."""
    global _monitor
    if _monitor is None:
        _monitor = RealTimeEvidenceMonitor()
    return _monitor
