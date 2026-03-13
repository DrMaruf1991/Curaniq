"""
CURANIQ Medical Evidence Operating System
Core Data Models — v1.0
All Pydantic schemas that flow through the pipeline.
"""
from __future__ import annotations
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


# ─────────────────────────────────────────────────────────────────────────────
# ENUMS
# ─────────────────────────────────────────────────────────────────────────────

class UserRole(str, Enum):
    CLINICIAN   = "clinician"
    PATIENT     = "patient"
    RESEARCHER  = "researcher"
    ADMIN       = "admin"


class InteractionMode(str, Enum):
    QUICK_ANSWER    = "quick_answer"        # Mode 1 — ~5s
    EVIDENCE_DEEP   = "evidence_deep_dive"  # Mode 2 — ~60s
    LIVING_DOSSIER  = "living_dossier"      # Mode 3 — ongoing
    DECISION_SESSION= "decision_session"    # Mode 4 — interactive
    DOCUMENT_PROC   = "document_processing" # Mode 5 — upload


class EvidenceTier(str, Enum):
    SYSTEMATIC_REVIEW = "systematic_review"  # Oxford CEBM Level 1
    RCT               = "rct"               # Level 2
    GUIDELINE         = "guideline"         # Level 2b
    COHORT            = "cohort"            # Level 3
    CASE_REPORT       = "case_report"       # Level 4
    EXPERT_OPINION    = "expert_opinion"    # Level 5
    PREPRINT          = "preprint"          # QUARANTINED


class GradeLevel(str, Enum):
    A  = "A"   # High — strong recommendation
    B  = "B"   # Moderate
    C  = "C"   # Low — conditional recommendation
    D  = "D"   # Very low — weak/against


class ClaimType(str, Enum):
    DOSING           = "dosing"
    CONTRAINDICATION = "contraindication"
    DRUG_INTERACTION = "drug_interaction"
    EFFICACY         = "efficacy"
    SAFETY_SIGNAL    = "safety_signal"
    DIAGNOSTIC       = "diagnostic"
    MONITORING       = "monitoring"
    PROGNOSIS        = "prognosis"
    GENERAL          = "general"


class ConfidenceLevel(str, Enum):
    HIGH    = "HIGH"      # >= 0.85 — show normally
    MEDIUM  = "MEDIUM"    # 0.70–0.85 — show with uncertainty marker
    LOW     = "LOW"       # 0.50–0.70 — show with caveat + human review flag
    SUPPRESS= "SUPPRESS"  # < 0.50 — never show


class SafetyFlag(str, Enum):
    BLACK_BOX_WARNING  = "black_box_warning"
    REMS_REQUIRED      = "rems_required"
    RETRACTED_SOURCE   = "retracted_source"
    STALE_DATA         = "stale_data"
    CONTRAINDICATED    = "contraindicated"
    HIGH_RISK_PATIENT  = "high_risk_patient"
    NUMERIC_UNVERIFIED = "numeric_unverified"
    EDGE_CASE          = "edge_case"
    DOSE_IMPLAUSIBLE   = "dose_implausible"


class TriageResult(str, Enum):
    CLEAR     = "clear"      # No emergency — continue pipeline
    EMERGENCY = "emergency"  # Hard stop — pre-scripted escalation only
    URGENT    = "urgent"     # High risk — expedited + extra verification


class Jurisdiction(str, Enum):
    UZ  = "UZ"   # Uzbekistan
    RU  = "RU"   # Russia / Minzdrav
    US  = "US"   # FDA / US guidelines
    UK  = "UK"   # NICE
    EU  = "EU"   # EMA
    INT = "INT"  # International (WHO)


class EvidenceSourceType(str, Enum):
    PUBMED          = "pubmed"
    CLINICALTRIALS  = "clinicaltrials"
    OPENFDA         = "openfda"
    DAILYMED        = "dailymed"
    RXNORM          = "rxnorm"
    NICE            = "nice"
    EMA             = "ema"
    COCHRANE        = "cochrane"
    RETRACTION_WATCH= "retraction_watch"
    CROSSREF        = "crossref"
    WHO             = "who"
    LACTMED         = "lactmed"
    CREDIBLEMEDS    = "crediblemeds"
    UZ_MOH          = "uz_moh"
    RU_MINZDRAV     = "ru_minzdrav"


class NumericTokenStatus(str, Enum):
    DETERMINISTIC = "deterministic"      # From CQL computation
    VERBATIM      = "verbatim_quoted"    # Character-identical from evidence source
    BLOCKED       = "blocked"            # Neither — SUPPRESS this claim


# ─────────────────────────────────────────────────────────────────────────────
# PATIENT CONTEXT  (minimal — principle of least data, L6-2)
# ─────────────────────────────────────────────────────────────────────────────

class RenalFunction(BaseModel):
    egfr_ml_min: Optional[float] = None           # CKD-EPI / MDRD
    crcl_ml_min: Optional[float] = None           # Cockcroft-Gault
    on_dialysis: bool = False
    dialysis_type: Optional[Literal["HD", "PD", "CRRT"]] = None


class HepaticFunction(BaseModel):
    child_pugh_class: Optional[Literal["A", "B", "C"]] = None
    meld_score: Optional[int] = None


class PatientContext(BaseModel):
    """Minimum necessary patient context — PHI scrubbed before LLM (L6-2)."""
    age_years: Optional[int] = None
    weight_kg: Optional[float] = None
    height_cm: Optional[float] = None
    sex_at_birth: Optional[Literal["M", "F"]] = None
    is_pregnant: bool = False
    gestational_week: Optional[int] = None
    is_breastfeeding: bool = False
    renal: Optional[RenalFunction] = None
    hepatic: Optional[HepaticFunction] = None
    active_medications: list[str] = Field(default_factory=list)
    allergies: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    jurisdiction: Jurisdiction = Jurisdiction.INT

    @field_validator("age_years")
    @classmethod
    def age_must_be_positive(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and (v < 0 or v > 130):
            raise ValueError("Age out of plausible range")
        return v


# ─────────────────────────────────────────────────────────────────────────────
# QUERY / REQUEST
# ─────────────────────────────────────────────────────────────────────────────

class ClinicalQuery(BaseModel):
    """Incoming clinical question — the entry point to the pipeline."""
    query_id: UUID = Field(default_factory=uuid4)
    raw_text: str = Field(..., min_length=3, max_length=4000)
    user_role: UserRole = UserRole.CLINICIAN
    mode: Optional[InteractionMode] = None   # None → auto-detect via L14-1
    patient_context: Optional[PatientContext] = None
    jurisdiction: Jurisdiction = Jurisdiction.INT
    session_id: Optional[UUID] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    attachments: list[str] = Field(default_factory=list)   # File IDs for Mode 5


# ─────────────────────────────────────────────────────────────────────────────
# EVIDENCE OBJECTS
# ─────────────────────────────────────────────────────────────────────────────

class EvidenceObject(BaseModel):
    """A single retrieved, verified evidence unit."""
    evidence_id: UUID = Field(default_factory=uuid4)
    source_type: EvidenceSourceType
    source_id: str          # e.g., PMID, DOI, NCT number, DailyMed SPL ID
    title: str
    snippet: str            # Extracted relevant passage
    snippet_byte_offset: Optional[int] = None   # For L5-17 verbatim tracing
    snippet_hash: Optional[str] = None          # SHA-256 of snippet for hash-lock (L4-14)
    url: Optional[str] = None
    authors: list[str] = Field(default_factory=list)
    published_date: Optional[datetime] = None
    ingested_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_verified_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    tier: EvidenceTier
    grade: Optional[GradeLevel] = None
    jurisdiction: Jurisdiction = Jurisdiction.INT
    is_retracted: bool = False
    retraction_date: Optional[datetime] = None
    staleness_ttl_hours: int = 24
    is_stale: bool = False

    @property
    def quality_score(self) -> float:
        """Oxford CEBM quality score — used in L4-13 confidence formula."""
        mapping = {
            EvidenceTier.SYSTEMATIC_REVIEW: 1.0,
            EvidenceTier.RCT:               0.9,
            EvidenceTier.GUIDELINE:         0.85,
            EvidenceTier.COHORT:            0.7,
            EvidenceTier.CASE_REPORT:       0.5,
            EvidenceTier.EXPERT_OPINION:    0.3,
            EvidenceTier.PREPRINT:          0.1,
        }
        return mapping.get(self.tier, 0.3)

    @property
    def recency_score(self) -> float:
        """Recency component for L4-13 confidence scoring."""
        if not self.published_date:
            return 0.5
        age_years = (datetime.now(timezone.utc) - self.published_date).days / 365
        if age_years < 1:   return 1.0
        if age_years < 3:   return 0.85
        if age_years < 5:   return 0.7
        return 0.5


class EvidencePack(BaseModel):
    """Collection of evidence objects assembled by the retriever."""
    pack_id: UUID = Field(default_factory=uuid4)
    query_id: UUID
    objects: list[EvidenceObject]
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    retrieval_strategy: str = "hybrid_bm25_vector"
    total_candidates_considered: int = 0

    @property
    def source_count(self) -> int:
        return len({e.source_id for e in self.objects})

    @property
    def has_retracted(self) -> bool:
        return any(e.is_retracted for e in self.objects)

    @property
    def has_stale(self) -> bool:
        return any(e.is_stale for e in self.objects)


# ─────────────────────────────────────────────────────────────────────────────
# CLAIM CONTRACT  (L4-3 — the core enforcement unit)
# ─────────────────────────────────────────────────────────────────────────────

class NumericToken(BaseModel):
    """Every number in output must be deterministic OR verbatim (L5-17)."""
    value_str: str                  # e.g., "500 mg", "30 mL/min", "5-10%"
    status: NumericTokenStatus
    cql_computation_id: Optional[str] = None    # If deterministic
    evidence_snippet_id: Optional[UUID] = None  # If verbatim
    byte_offset: Optional[int] = None
    hash_match: Optional[bool] = None


class AtomicClaim(BaseModel):
    """A single verifiable clinical assertion — the unit of the Claim Contract."""
    claim_id: UUID = Field(default_factory=uuid4)
    claim_text: str
    claim_type: ClaimType
    evidence_ids: list[UUID]            # Must reference EvidenceObject.evidence_id
    entailment_score: float = 0.0       # NLI model output 0.0–1.0
    is_supported: bool = False          # True only if entailment passes threshold
    is_blocked: bool = False            # True if suppressed by any gate
    block_reason: Optional[str] = None
    numeric_tokens: list[NumericToken] = Field(default_factory=list)
    confidence_score: float = 0.0       # Composite from L4-13
    confidence_level: ConfidenceLevel = ConfidenceLevel.SUPPRESS
    safety_flags: list[SafetyFlag] = Field(default_factory=list)


class ClaimContract(BaseModel):
    """The complete verified claim set for a response."""
    contract_id: UUID = Field(default_factory=uuid4)
    query_id: UUID
    atomic_claims: list[AtomicClaim]
    total_claims: int = 0
    blocked_claims: int = 0
    passed_claims: int = 0
    enforcement_passed: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def compute_totals(self) -> "ClaimContract":
        self.total_claims   = len(self.atomic_claims)
        self.blocked_claims = sum(1 for c in self.atomic_claims if c.is_blocked)
        self.passed_claims  = self.total_claims - self.blocked_claims
        # Enforcement passes if >0 claims survive AND no high-risk unsupported claims
        self.enforcement_passed = (
            self.passed_claims > 0 and
            self.blocked_claims / max(self.total_claims, 1) < 0.5
        )
        return self


# ─────────────────────────────────────────────────────────────────────────────
# CQL COMPUTATION LOG  (L3-1 output — feeds L5-17)
# ─────────────────────────────────────────────────────────────────────────────

class CQLComputationLog(BaseModel):
    """Immutable log of a deterministic CQL computation."""
    computation_id: str = Field(default_factory=lambda: str(uuid4()))
    rule_id: str                # e.g., "CQL.RENAL.METFORMIN.EGFR"
    rule_version: str
    inputs: dict[str, Any]
    formula_applied: str
    output_value: str           # The exact numeric string produced
    output_unit: Optional[str] = None
    computed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    is_deterministic: bool = True


# ─────────────────────────────────────────────────────────────────────────────
# SAFETY GATE RESULTS  (L5 layer)
# ─────────────────────────────────────────────────────────────────────────────

class SafetyGateResult(BaseModel):
    gate_id: str
    gate_name: str
    passed: bool
    message: Optional[str] = None
    severity: Literal["INFO", "WARNING", "BLOCK", "EMERGENCY"] = "INFO"
    flags_raised: list[SafetyFlag] = Field(default_factory=list)
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class SafetyGateSuite(BaseModel):
    """Results from all L5 safety gates."""
    query_id: UUID
    gates: list[SafetyGateResult]
    overall_passed: bool
    hard_block: bool = False        # Any EMERGENCY/BLOCK gate failed
    warnings: list[str] = Field(default_factory=list)
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def compute_overall(self) -> "SafetyGateSuite":
        if not self.gates:
            # Empty gates = refusal/emergency path. Preserve values as set by caller.
            return self
        self.hard_block = any(
            g.severity in ("EMERGENCY", "BLOCK") and not g.passed
            for g in self.gates
        )
        self.overall_passed = not self.hard_block
        self.warnings = [
            g.message for g in self.gates
            if g.severity == "WARNING" and not g.passed and g.message
        ]
        return self


# ─────────────────────────────────────────────────────────────────────────────
# TRIAGE  (L5-13 — pre-LLM emergency classifier)
# ─────────────────────────────────────────────────────────────────────────────

class TriageAssessment(BaseModel):
    result: TriageResult
    triggered_criteria: list[str] = Field(default_factory=list)
    escalation_message: Optional[str] = None
    assessed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ─────────────────────────────────────────────────────────────────────────────
# FRESHNESS STAMPS  (L1-16)
# ─────────────────────────────────────────────────────────────────────────────

class FreshnessStamp(BaseModel):
    source_type: EvidenceSourceType
    last_checked: datetime
    staleness_ttl_hours: int
    is_stale: bool
    display_text: str   # e.g., "PubMed: 2h ago", "openFDA: STALE — 26h"


# ─────────────────────────────────────────────────────────────────────────────
# FINAL RESPONSE
# ─────────────────────────────────────────────────────────────────────────────

class EvidenceCard(BaseModel):
    """A single displayed evidence card for the UI (L8-1)."""
    card_id: UUID = Field(default_factory=uuid4)
    claim_text: str
    claim_type: ClaimType
    confidence_level: ConfidenceLevel
    confidence_score: float
    grade: Optional[GradeLevel] = None
    sources: list[dict[str, Any]]       # Title, URL, snippet, tier, date
    safety_flags: list[SafetyFlag] = Field(default_factory=list)
    freshness_stamps: list[FreshnessStamp] = Field(default_factory=list)
    uncertainty_marker: Optional[str] = None
    caveat: Optional[str] = None
    numeric_verified: bool = True


class CURANIQResponse(BaseModel):
    """The final structured response sent to the client."""
    response_id: UUID = Field(default_factory=uuid4)
    query_id: UUID
    mode: InteractionMode
    user_role: UserRole

    # Gate results
    triage: TriageAssessment
    safety_suite: SafetyGateSuite
    claim_contract_enforced: bool

    # Content
    evidence_cards: list[EvidenceCard]
    summary_text: Optional[str] = None     # Human-readable synthesis
    safe_next_steps: list[str] = Field(default_factory=list)   # L5-3
    monitoring_required: list[str] = Field(default_factory=list)  # L5-10
    escalation_thresholds: list[str] = Field(default_factory=list)  # L5-10
    follow_up_interval: Optional[str] = None

    # Metadata
    freshness_stamps: list[FreshnessStamp] = Field(default_factory=list)
    sources_used: int = 0
    processing_time_ms: Optional[float] = None
    refused: bool = False
    refusal_reason: Optional[str] = None
    audit_ledger_id: Optional[UUID] = None
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ─────────────────────────────────────────────────────────────────────────────
# AUDIT LEDGER ENTRY  (L9-1 — immutable)
# ─────────────────────────────────────────────────────────────────────────────

class AuditLedgerEntry(BaseModel):
    """Immutable audit record — append-only, cryptographically chained."""
    entry_id: UUID = Field(default_factory=uuid4)
    query_id: UUID
    session_id: Optional[UUID] = None
    user_role: UserRole
    mode: InteractionMode
    jurisdiction: Jurisdiction

    # Pipeline trace
    triage_result: TriageResult
    mode_detected: InteractionMode
    evidence_pack_id: UUID
    claim_contract_id: UUID
    safety_suite_passed: bool
    hard_blocked: bool

    # Evidence provenance (L9-3)
    evidence_source_ids: list[str]
    cql_computation_ids: list[str]
    refused: bool
    refusal_reason: Optional[str] = None

    # Integrity
    previous_entry_hash: Optional[str] = None   # Hash of prior entry (chain)
    entry_hash: Optional[str] = None            # SHA-256 of this entry
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ─────────────────────────────────────────────────────────────────────────────
# UNIFIED MODEL ADDITIONS (Fix 0J — merged from evidence.py + claims.py)
# ─────────────────────────────────────────────────────────────────────────────

class SourceAPI(str, Enum):
    """Governed evidence sources. Web search is NOT a valid source."""
    PUBMED              = "pubmed"
    CLINICAL_TRIALS     = "clinical_trials"
    COCHRANE            = "cochrane"
    OPENFDA_LABELS      = "openfda_labels"
    OPENFDA_FAERS       = "openfda_faers"
    DAILYMED_SPL        = "dailymed_spl"
    CROSSREF            = "crossref"
    NICE_GUIDELINES     = "nice_guidelines"
    EMA_EPAR            = "ema_epar"
    LACTMED             = "lactmed"
    WHO_ICTRP           = "who_ictrp"
    RXNORM              = "rxnorm"
    RETRACTION_WATCH    = "retraction_watch"
    UZ_MOH              = "uz_moh"
    RUSSIAN_MINZDRAV    = "russian_minzdrav"
    CIS_REGIONAL        = "cis_regional"
    MEDRXIV             = "medrxiv"


class StalenessStatus(str, Enum):
    """Evidence staleness state per L1-5 SLA Dashboard."""
    FRESH    = "fresh"
    STALE    = "stale"
    CRITICAL = "critical"
    UNKNOWN  = "unknown"


class RetractionStatus(str, Enum):
    """Retraction state per L2-7 Retraction Watch Sentinel."""
    CLEAR      = "clear"
    RETRACTED  = "retracted"
    CORRECTED  = "corrected"
    EXPRESSION = "expression_of_concern"
    UNCHECKED  = "unchecked"


class ClaimVerdict(str, Enum):
    """Final disposition of a claim after full pipeline evaluation."""
    PASS_HIGH        = "pass_high"
    PASS_MEDIUM      = "pass_medium"
    PASS_LOW         = "pass_low"
    SUPPRESSED       = "suppressed"
    BLOCKED_RETRACT  = "blocked_retract"
    BLOCKED_STALE    = "blocked_stale"
    BLOCKED_HALLUC   = "blocked_hallucination"
    BLOCKED_NLI      = "blocked_nli"
    REFUSED          = "refused"
    PENDING          = "pending"


class VerifierDecision(str, Enum):
    """Adversarial LLM verifier decision per L4-12."""
    FAITHFUL     = "faithful"
    DISTORTED    = "distorted"
    OMISSION     = "omission"
    SCOPE_MISS   = "scope_miss"
    UNSUPPORTED  = "unsupported"
    FABRICATED   = "fabricated"


class SnippetClaimBinding(BaseModel):
    """Binds a claim to a specific evidence snippet. L4-14 requirement."""
    chunk_id:       str
    byte_offset:    int
    snippet_hash:   str
    span_length:    int
    model_config = {"frozen": True}


HIGH_RISK_CLAIM_TYPES: set[ClaimType] = {
    ClaimType.DOSING,
    ClaimType.CONTRAINDICATION,
    ClaimType.DRUG_INTERACTION,
}


# Backward compatibility alias for layers/ that import EvidenceChunk
EvidenceChunk = EvidenceObject
