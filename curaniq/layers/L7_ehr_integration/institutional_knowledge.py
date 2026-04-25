"""
CURANIQ — Medical Evidence Operating System
L7-16: Institutional Knowledge Engine & Local Protocol Learner

Architecture spec:
  'Learns each hospital's unwritten rules over time. Components:
  (a) Local formulary integration
  (b) Local protocol capture — institutional overlays on national guidelines
  (c) Override pattern analysis — >60% override rate → flags for review
  (d) Specialist preference learning — statistical frequency, NOT LLM inference

  CRITICAL SAFETY CONSTRAINT: institutional preferences NEVER override
  safety alerts (DDIs, contraindications, Black Box warnings).'

Design:
  - Per-tenant data isolation (hospital A never sees hospital B's data)
  - Formulary: drug availability + preferred alternatives + cost tiers
  - Protocols: structured overlays with version history
  - Override tracking: statistical analysis, not ML inference
  - All preferences gated by SAFETY_NEVER_OVERRIDE set
  - JSON/CSV import for formulary + antibiogram (no vendor lock-in)
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
# SAFETY CONSTRAINT: categories that institutional preferences
# can NEVER override. Architecture invariant.
# ─────────────────────────────────────────────────────────────────

SAFETY_NEVER_OVERRIDE: frozenset[str] = frozenset({
    "ddi_major",
    "ddi_contraindicated",
    "allergy_cross_reactivity",
    "black_box_warning",
    "contraindication_absolute",
    "pregnancy_category_x",
    "dose_lethal_range",
    "qt_prolongation_high_risk",
    "renal_contraindicated",
    "hepatic_contraindicated",
})


# ─────────────────────────────────────────────────────────────────
# LOCAL FORMULARY — (a) from architecture spec
# ─────────────────────────────────────────────────────────────────

class FormularyStatus(str, Enum):
    AVAILABLE = "available"
    RESTRICTED = "restricted"       # Requires approval
    NOT_STOCKED = "not_stocked"
    SHORTAGE = "shortage"           # Temporary supply issue
    PREFERRED = "preferred"         # Institution preferred


@dataclass
class FormularyEntry:
    """A drug's status in a hospital's local formulary."""
    drug_name: str
    inn_name: str = ""              # International Nonproprietary Name
    rxnorm_code: Optional[str] = None
    atc_code: Optional[str] = None
    status: FormularyStatus = FormularyStatus.AVAILABLE
    cost_tier: int = 2              # 1=cheapest, 5=most expensive
    alternatives: list[str] = field(default_factory=list)
    restrictions: Optional[str] = None   # "Infectious disease approval required"
    notes: Optional[str] = None
    last_updated: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class LocalFormulary:
    """
    Hospital-specific drug formulary.
    Import from CSV/JSON. Query by drug name.
    When a recommended drug is NOT on formulary, suggests alternatives.
    """

    def __init__(self, tenant_id: str = "default") -> None:
        self.tenant_id = tenant_id
        self._entries: dict[str, FormularyEntry] = {}  # normalized_name → entry

    def import_entries(self, entries: list[dict]) -> int:
        """
        Bulk import formulary entries from JSON/CSV data.
        Returns count of entries imported.
        """
        count = 0
        for raw in entries:
            try:
                entry = FormularyEntry(
                    drug_name=raw["drug_name"],
                    inn_name=raw.get("inn_name", raw["drug_name"]),
                    rxnorm_code=raw.get("rxnorm_code"),
                    atc_code=raw.get("atc_code"),
                    status=FormularyStatus(raw.get("status", "available")),
                    cost_tier=int(raw.get("cost_tier", 2)),
                    alternatives=[a.strip() for a in raw.get("alternatives", "").split(",") if a.strip()] if isinstance(raw.get("alternatives"), str) else raw.get("alternatives", []),
                    restrictions=raw.get("restrictions"),
                    notes=raw.get("notes"),
                )
                key = entry.inn_name.lower().strip()
                self._entries[key] = entry
                count += 1
            except (KeyError, ValueError) as e:
                logger.warning(f"L7-16: Formulary import skip: {e}")
        logger.info(f"L7-16: Imported {count} formulary entries for tenant={self.tenant_id}")
        return count

    def check(self, drug_name: str) -> Optional[FormularyEntry]:
        """Check if a drug is on the local formulary."""
        key = drug_name.lower().strip()
        return self._entries.get(key)

    def suggest_alternative(self, drug_name: str) -> list[FormularyEntry]:
        """
        When a drug is not on formulary or not available,
        suggest formulary alternatives sorted by cost tier.
        """
        entry = self.check(drug_name)
        if entry and entry.status == FormularyStatus.AVAILABLE:
            return []  # Drug is available — no alternative needed

        alternatives = []
        alt_names = entry.alternatives if entry else []

        for alt_name in alt_names:
            alt_entry = self.check(alt_name)
            if alt_entry and alt_entry.status in (FormularyStatus.AVAILABLE, FormularyStatus.PREFERRED):
                alternatives.append(alt_entry)

        return sorted(alternatives, key=lambda e: (e.status != FormularyStatus.PREFERRED, e.cost_tier))

    def get_formulary_alert(self, drug_name: str) -> Optional[str]:
        """
        Generate a formulary alert message if drug is not available.
        Returns None if drug is on formulary and available.
        """
        entry = self.check(drug_name)
        if entry is None:
            return f"⚠️ {drug_name} is NOT on this facility's formulary. Check local availability."
        if entry.status == FormularyStatus.NOT_STOCKED:
            alts = self.suggest_alternative(drug_name)
            alt_text = ", ".join(a.drug_name for a in alts[:3]) if alts else "none available"
            return f"⚠️ {drug_name} is not stocked. Formulary alternatives: {alt_text}"
        if entry.status == FormularyStatus.RESTRICTED:
            return f"🔒 {drug_name} is restricted: {entry.restrictions or 'approval required'}"
        if entry.status == FormularyStatus.SHORTAGE:
            return f"⚠️ {drug_name} is in shortage. Consider alternatives."
        return None

    @property
    def entry_count(self) -> int:
        return len(self._entries)


# ─────────────────────────────────────────────────────────────────
# LOCAL PROTOCOL CAPTURE — (b) institutional overlays
# ─────────────────────────────────────────────────────────────────

class ProtocolScope(str, Enum):
    INSTITUTION = "institution"     # Applies hospital-wide
    DEPARTMENT = "department"       # E.g., ICU, orthopedics
    UNIT = "unit"                   # Specific ward/unit


@dataclass
class InstitutionalProtocol:
    """
    A local protocol that overlays on national guidelines.
    Example: 'In our ICU, we use vancomycin AUC-guided dosing'
    """
    protocol_id: str = field(default_factory=lambda: str(uuid4()))
    tenant_id: str = ""
    title: str = ""
    description: str = ""
    scope: ProtocolScope = ProtocolScope.INSTITUTION
    department: Optional[str] = None
    clinical_domain: str = ""       # "antimicrobial", "anticoagulation", etc.
    overrides_guideline: Optional[str] = None  # Which national guideline this modifies
    active: bool = True
    version: int = 1
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    approved_by: Optional[str] = None  # Pharmacy committee / medical director
    # Structured rules
    conditions: list[dict] = field(default_factory=list)  # When to apply
    actions: list[dict] = field(default_factory=list)      # What to do differently
    evidence_basis: Optional[str] = None                   # Why this local deviation


class ProtocolStore:
    """Per-tenant protocol storage with version history."""

    def __init__(self, tenant_id: str = "default") -> None:
        self.tenant_id = tenant_id
        self._protocols: dict[str, InstitutionalProtocol] = {}
        self._history: dict[str, list[InstitutionalProtocol]] = defaultdict(list)

    def add(self, protocol: InstitutionalProtocol) -> str:
        """Add or update a protocol. Maintains version history."""
        protocol.tenant_id = self.tenant_id
        existing = self._protocols.get(protocol.protocol_id)
        if existing:
            self._history[protocol.protocol_id].append(existing)
            protocol.version = existing.version + 1
            protocol.updated_at = datetime.now(timezone.utc).isoformat()
        self._protocols[protocol.protocol_id] = protocol
        logger.info(f"L7-16: Protocol '{protocol.title}' v{protocol.version} stored for {self.tenant_id}")
        return protocol.protocol_id

    def get_applicable(
        self,
        clinical_domain: str,
        department: Optional[str] = None,
    ) -> list[InstitutionalProtocol]:
        """Get active protocols matching clinical domain and department scope."""
        results = []
        for proto in self._protocols.values():
            if not proto.active:
                continue
            if proto.clinical_domain and proto.clinical_domain != clinical_domain:
                continue
            # Scope filtering: institution-wide always applies, department matches
            if proto.scope == ProtocolScope.INSTITUTION:
                results.append(proto)
            elif department and proto.department and proto.department.lower() == department.lower():
                results.append(proto)
        return results

    @property
    def protocol_count(self) -> int:
        return len(self._protocols)


# ─────────────────────────────────────────────────────────────────
# OVERRIDE PATTERN ANALYSIS — (c) statistical frequency
# ─────────────────────────────────────────────────────────────────

@dataclass
class OverrideRecord:
    """A clinician override of a CURANIQ recommendation."""
    record_id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    clinician_id: str = ""
    clinician_specialty: Optional[str] = None
    department: Optional[str] = None
    recommendation_type: str = ""   # "drug_choice", "dose", "interaction_alert", etc.
    recommendation_text: str = ""
    override_reason: Optional[str] = None
    safety_category: Optional[str] = None  # If this was a safety alert that was overridden


class OverrideAnalyzer:
    """
    Statistical override pattern analysis (NOT ML, per architecture spec).
    Tracks when clinicians consistently override recommendations.
    Flags >60% override rate for pharmacy committee review.
    """

    REVIEW_THRESHOLD: float = 0.60  # >60% override rate → flag

    def __init__(self, tenant_id: str = "default") -> None:
        self.tenant_id = tenant_id
        self._records: list[OverrideRecord] = []
        # Tracking: recommendation_key → {overridden: int, accepted: int}
        self._stats: dict[str, dict[str, int]] = defaultdict(lambda: {"overridden": 0, "accepted": 0})

    def record_override(self, record: OverrideRecord) -> None:
        """Record an override event."""
        # SAFETY CHECK: never suppress safety alert overrides from analysis
        self._records.append(record)
        key = record.recommendation_type
        self._stats[key]["overridden"] += 1

        if record.safety_category and record.safety_category in SAFETY_NEVER_OVERRIDE:
            logger.warning(
                f"L7-16 SAFETY OVERRIDE: Clinician {record.clinician_id} overrode "
                f"safety alert ({record.safety_category}): {record.recommendation_text[:100]}. "
                "This is logged for mandatory review."
            )

    def record_acceptance(self, recommendation_type: str) -> None:
        """Record that a recommendation was accepted."""
        self._stats[recommendation_type]["accepted"] += 1

    def get_flagged_patterns(self) -> list[dict]:
        """
        Get recommendations with >60% override rate.
        These should be reviewed by pharmacy/therapeutics committee.
        """
        flagged = []
        for rec_type, counts in self._stats.items():
            total = counts["overridden"] + counts["accepted"]
            if total < 5:  # Minimum sample size
                continue
            rate = counts["overridden"] / total
            if rate > self.REVIEW_THRESHOLD:
                flagged.append({
                    "recommendation_type": rec_type,
                    "override_rate": round(rate, 4),
                    "total_encounters": total,
                    "overridden": counts["overridden"],
                    "accepted": counts["accepted"],
                    "flag": f"Override rate {rate:.0%} exceeds {self.REVIEW_THRESHOLD:.0%} threshold",
                })
        return sorted(flagged, key=lambda x: -x["override_rate"])

    def get_specialty_patterns(self) -> dict[str, dict]:
        """Breakdown of override patterns by specialty."""
        by_specialty: dict[str, dict[str, int]] = defaultdict(lambda: {"overridden": 0, "total": 0})
        for record in self._records:
            spec = record.clinician_specialty or "unknown"
            by_specialty[spec]["overridden"] += 1
            by_specialty[spec]["total"] += 1
        return dict(by_specialty)

    @property
    def total_records(self) -> int:
        return len(self._records)


# ─────────────────────────────────────────────────────────────────
# SPECIALIST PREFERENCE LEARNING — (d) statistical frequency
# ─────────────────────────────────────────────────────────────────

@dataclass
class PreferenceSignal:
    """A preference signal from prescription/recommendation patterns."""
    department: str
    drug_category: str        # "NSAID", "statin", "antibiotic", etc.
    preferred_drug: str
    frequency: int = 1
    last_seen: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class SpecialistPreferenceLearner:
    """
    Learns department-level drug preferences from prescription patterns.
    Pure statistical frequency — NOT LLM inference (per architecture spec).
    
    Example: 'Orthopedics prefers celecoxib over ibuprofen post-operatively'
    learned from observing 80% of orthopedic NSAIDs are celecoxib prescriptions.
    
    SAFETY CONSTRAINT: preferences NEVER override safety alerts.
    Only applies when multiple safe options exist.
    """

    def __init__(self, tenant_id: str = "default") -> None:
        self.tenant_id = tenant_id
        # (department, drug_category) → {drug_name: count}
        self._freq: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))

    def record_prescription(
        self, department: str, drug_category: str, drug_name: str
    ) -> None:
        """Record a prescription event for frequency analysis."""
        key = (department.lower(), drug_category.lower())
        self._freq[key][drug_name.lower()] += 1

    def get_preference(
        self,
        department: str,
        drug_category: str,
        min_observations: int = 10,
        min_preference_pct: float = 0.60,
    ) -> Optional[PreferenceSignal]:
        """
        Get the department's preferred drug for a category.
        Returns None if no clear preference (below thresholds).
        """
        key = (department.lower(), drug_category.lower())
        freq = self._freq.get(key)
        if not freq:
            return None

        total = sum(freq.values())
        if total < min_observations:
            return None

        top_drug = max(freq, key=lambda d: freq[d])
        pct = freq[top_drug] / total
        if pct < min_preference_pct:
            return None

        return PreferenceSignal(
            department=department,
            drug_category=drug_category,
            preferred_drug=top_drug,
            frequency=freq[top_drug],
        )

    def get_all_preferences(self, min_observations: int = 10) -> list[PreferenceSignal]:
        """Get all learned preferences above threshold."""
        results = []
        for (dept, cat), freq in self._freq.items():
            pref = self.get_preference(dept, cat, min_observations)
            if pref:
                results.append(pref)
        return results


# ─────────────────────────────────────────────────────────────────
# INSTITUTIONAL KNOWLEDGE ENGINE — orchestrator
# ─────────────────────────────────────────────────────────────────

class InstitutionalKnowledgeEngine:
    """
    L7-16: Institutional Knowledge Engine.
    
    Orchestrates all four components:
    (a) Local Formulary
    (b) Local Protocol Capture
    (c) Override Pattern Analysis
    (d) Specialist Preference Learning
    
    Per-tenant isolation: each hospital gets its own instance.
    """

    def __init__(self, tenant_id: str = "default") -> None:
        self.tenant_id = tenant_id
        self.formulary = LocalFormulary(tenant_id)
        self.protocols = ProtocolStore(tenant_id)
        self.overrides = OverrideAnalyzer(tenant_id)
        self.preferences = SpecialistPreferenceLearner(tenant_id)

    def enrich_recommendation(
        self,
        drug_name: str,
        recommendation_type: str,
        safety_category: Optional[str] = None,
        department: Optional[str] = None,
        clinical_domain: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Enrich a CURANIQ recommendation with institutional context.
        
        Returns dict with:
          - formulary_alert: str or None
          - formulary_alternatives: list of alternative drugs
          - applicable_protocols: list of local protocol overlays
          - department_preference: preferred drug if applicable
          - safety_override_blocked: True if trying to override safety
        """
        result: dict[str, Any] = {
            "formulary_alert": None,
            "formulary_alternatives": [],
            "applicable_protocols": [],
            "department_preference": None,
            "safety_override_blocked": False,
        }

        # (a) Formulary check
        result["formulary_alert"] = self.formulary.get_formulary_alert(drug_name)
        alts = self.formulary.suggest_alternative(drug_name)
        result["formulary_alternatives"] = [
            {"drug": a.drug_name, "status": a.status.value, "cost_tier": a.cost_tier}
            for a in alts[:5]
        ]

        # (b) Local protocols
        if clinical_domain:
            protos = self.protocols.get_applicable(clinical_domain, department)
            result["applicable_protocols"] = [
                {"title": p.title, "description": p.description, "scope": p.scope.value}
                for p in protos[:3]
            ]

        # (d) Specialist preference (only if NO safety concern)
        if department and safety_category not in SAFETY_NEVER_OVERRIDE:
            # Determine drug category heuristically from recommendation type
            pref = self.preferences.get_preference(department, recommendation_type)
            if pref:
                result["department_preference"] = {
                    "preferred_drug": pref.preferred_drug,
                    "department": pref.department,
                    "note": f"{pref.department} typically prefers {pref.preferred_drug} "
                            f"for {pref.drug_category} (based on {pref.frequency} prescriptions)",
                }

        # SAFETY CONSTRAINT: block if trying to override safety alerts
        if safety_category and safety_category in SAFETY_NEVER_OVERRIDE:
            result["safety_override_blocked"] = True

        return result

    def summary(self) -> dict:
        """Audit summary for L9-1."""
        return {
            "module": "L7-16",
            "tenant_id": self.tenant_id,
            "formulary_entries": self.formulary.entry_count,
            "protocols": self.protocols.protocol_count,
            "override_records": self.overrides.total_records,
            "flagged_patterns": len(self.overrides.get_flagged_patterns()),
            "learned_preferences": len(self.preferences.get_all_preferences()),
        }
