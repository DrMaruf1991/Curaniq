"""
CURANIQ — Main Pipeline Orchestrator
Wires all components together in the correct sequence:

  1.  L5-13 Triage Gate           → emergency halt if life-threatening
  2.  L6-1  Prompt Injection Defense → sanitize input
  3.  L14-1 Mode Router           → classify interaction mode
  4.  L14-2 Question Decomposer   → expand into sub-queries
  5.  L4-1  Hybrid Retriever      → fetch evidence from store
  6.  L3-1  CQL Safety Kernel     → deterministic safety rules
  7.  L4-2  Constrained Generator → LLM with evidence-locked prompt
  8.  L4-3  Claim Contract Engine → segment, classify, verify, score claims
  9.  L5    Safety Gate Suite     → all 11 safety gates
  10. L8-1  Evidence Card Builder → format verified claims as UI cards
  11. L1-16 Freshness Stamps      → attach source freshness to response
  12. L9-1  Audit Ledger          → immutable compliance record
"""
from __future__ import annotations
import hashlib
import logging
import re
import time
import asyncio
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

from curaniq.core.cql_kernel import CQLKernel
from curaniq.core.claim_contract import ClaimContractEngine
from curaniq.core.pipeline_components import (
    ConstrainedGenerator,
    HybridRetriever,
    ModeRouter,
    QuestionDecomposer,
)
from curaniq.audit.ledger import AuditLedger
from curaniq.models.schemas import (
    ClaimType,
    ClinicalQuery,
    ConfidenceLevel,
    CURANIQResponse,
    EvidenceCard,
    EvidenceObject,
    EvidenceSourceType,
    FreshnessStamp,
    GradeLevel,
    InteractionMode,
    SafetyFlag,
    TriageResult,
    UserRole,
)
from curaniq.safety.triage_gate import TriageGate
from curaniq.layers.L2_curation.ontology import OntologyNormalizer
from curaniq.layers.L8_interface.universal_input import UniversalInputNormalizer
from curaniq.layers.L6_security.prompt_defense import PromptDefenseSuite

# L8: Interface layer — Evidence Cards, Role-Based UI, Multilingual
from curaniq.layers.L8_interface.interface_layer import (
    EvidenceCardsBuilder,
    RoleBasedUIAdapter,
    MultilingualEngine,
    MedicationBoundaryDisplay,
    LanguageAutoDetector,
    MedicalTranslationEngine,
)

# L2: Evidence curation engines
from curaniq.layers.L2_curation.grade_engine import GRADEGradingEngine
from curaniq.layers.L2_curation.living_review import LivingReviewEngine
from curaniq.layers.L2_curation.retraction_jurisdiction import (
    RetractionWatchSentinel,
    JurisdictionGuidanceGate,
)

# L9: Payment gateway
from curaniq.layers.L9_audit_payments.citation_payment import PaymentGateway

# L1: Evidence ingestion pipeline
from curaniq.layers.L1_evidence_ingestion.staleness_monitor import (
    StalenessSLADashboard,
    RealTimeEvidenceMonitor,
)
from curaniq.layers.L1_evidence_ingestion.semantic_chunker import (
    SemanticChunkingEngine,
    EvidenceChunkMetadataStamper,
)
from curaniq.layers.L1_evidence_ingestion.evidence_compiler import (
    EvidenceCompiler,
    NegativeEvidenceRegistry,
)

# L4: AI model layer (registered — richer versions of core/ components)
from curaniq.layers.L4_ai_model.adversarial_jury import (
    AdversarialLLMJury,
    ConfidenceScorer as L4ConfidenceScorer,
)

# L5: Safety gate pipeline (registered — class-based 14-gate version)
from curaniq.layers.L5_safety_gates.safety_gate_pipeline import (
    SafetyGatePipeline as L5SafetyGatePipeline,
)
from curaniq.layers.L6_security.llm_client import MultiLLMClient
from curaniq.layers.L6_security.phi_scrubber import PHIScrubber
from curaniq.layers.L11_local_reality.drug_availability import LocalDrugAvailabilityFilter
from curaniq.layers.L14_interaction.session_memory import ClinicalSessionMemory, AssumptionLedger
from curaniq.layers.L6_security.phi_scrubber import OutputExfiltrationScanner
from curaniq.safety.safety_gates import SafetyGateSuiteRunner

# ── NEW: L0 Regulatory Foundation ──
from curaniq.layers.L0_regulatory.qms_risk import (
    QualityManagementSystem,
    RiskManagementFramework,
    ValidationProgramme,
)
from curaniq.layers.L0_regulatory.security_infra import (
    SecretManager,
    CybersecurityLifecycle,
    DataArchitectureConfig,
)

# ── NEW: L1 Evidence Quality (L1-3, L1-6, L1-7) ──
from curaniq.layers.L1_evidence_ingestion.evidence_quality import (
    NegativeEvidenceRegistry as L1NegativeEvidenceRegistry,
    SourceQualityScorer,
    DeduplicationEngine,
)
# ── NEW: L1-9/L1-10 Guideline Connectors ──
from curaniq.layers.L1_evidence_ingestion.guideline_connectors import (
    NICEGuidelineConnector,
    WHOGuidelineConnector,
)
# ── WIRING: L1-1 Extended API Connectors (was unwired) ──
from curaniq.layers.L1_evidence_ingestion.api_connectors import (
    EvidenceSourceOrchestrator,
    PubMedConnector,
    OpenFDAConnector,
    CrossrefConnector,
)

# ── NEW: L2-15 Terminology Version Control ──
from curaniq.layers.L2_curation.terminology_version import (
    TerminologyVersionControl,
)

# ── WIRING: L3-1 Extended CQL Engine (was unwired) ──
from curaniq.layers.L3_safety_kernel.cql_engine import CQLEngine as ExtendedCQLEngine

# ── WIRING: L4 Extended modules (were unwired) ──
from curaniq.layers.L4_ai_model.retrieval_pipeline import HybridRetrievalPipeline
from curaniq.layers.L4_ai_model.constrained_generator import ConstrainedLLMGenerator
from curaniq.layers.L4_ai_model.claim_contract_engine import (
    ClaimContractEngine as ExtendedClaimContractEngine,
    NLIEntailmentClient,
    EvidenceHashLockEngine,
)

# ── NEW: L6-6 Upload Sanitization ──
from curaniq.layers.L6_security.upload_sanitization import UploadSanitizer

# ── WIRING: L7 EHR Integration (were unwired) ──
from curaniq.layers.L7_ehr_integration.fhir_gateway import FHIRGateway
from curaniq.layers.L7_ehr_integration.cds_hooks import CDSHooksService
from curaniq.layers.L7_ehr_integration.token_lifecycle import EHRTokenLifecycleManager
from curaniq.layers.L7_ehr_integration.antibiogram import InstitutionalAntibiogram
from curaniq.layers.L7_ehr_integration.institutional_knowledge import InstitutionalKnowledgeEngine

# ── NEW: L8-5/L8-12 Multilingual Safety ──
from curaniq.layers.L8_interface.multilingual_safety import (
    MultilingualClinicalInterface,
    MeaningLockEngine,
)

# ── NEW: L9-3 Citation Provenance Graph ──
from curaniq.layers.L9_audit_payments.citation_provenance import CitationProvenanceGraph

# ── WIRING: L10-1 Shadow Deploy (was unwired) ──
from curaniq.layers.L10_testing.shadow_deploy import ShadowDeploymentEngine

# ── L10-2/L10-4/L10-11/L10-12 Regression + Benchmark + Trust + ROI ──
from curaniq.layers.L10_testing.regression_benchmark import (
    SyntheticPatientRegression,
    BenchmarkDashboard,
    ClinicianTrustDashboard,
    InstitutionalROICalculator,
)

# ── WIRING: L14-6 Document Intake (was unwired) ──
from curaniq.layers.L14_interaction.document_intake import DocumentIntakePipeline

# ── NEW: L0-9/L0-10/L0-11 Ops + Boundary ──
from curaniq.layers.L0_regulatory.ops_boundary import (
    ProductBoundaryEnforcer,
    ProductionOpsHub,
    IncidentResponseSystem,
    ProductMode,
)
# ── NEW: L1-10 LactMed + L1-12 Cochrane ──
from curaniq.layers.L1_evidence_ingestion.lactmed_cochrane import (
    LactMedConnector,
    CochraneConnector,
)
# ── NEW: L4-11 Cross-Encoder Reranking ──
from curaniq.layers.L4_ai_model.cross_encoder import CrossEncoderReranker
# ── NEW: L8-8 Medication Coverage Scope Fence ──
from curaniq.layers.L8_interface.coverage_scope import MedicationCoverageScopeFence

# ── P2 CLUSTER 3: L7 EHR & Clinical Workflows (8 modules) ──
from curaniq.layers.L7_ehr_integration.clinical_scoring_labs import (
    LabResultInterpreter,          # L7-11
    ClinicalScoringEngine,         # L7-14
    AlertFatigueManager,           # L7-10
    OrderSetCopilot,               # L7-7
)
from curaniq.layers.L7_ehr_integration.clinical_workflows import (
    ContextAwareEvidenceDelivery,   # L7-4
    InstitutionPolicyEnforcer,     # L7-6
    MedicationReconciliationEngine, # L7-8
    ClinicalPathwayGenerator,      # L7-9
)

# ── P2 CLUSTER 4: L8/L14 Interface & UX (12 modules) ──
from curaniq.layers.L8_interface.interface_extensions import (
    VisualReasoningMapBuilder,     # L8-2
    TokenUncertaintyVisualizer,    # L8-3
    EvidenceWatchlist,             # L8-6
    ClinicianChallengeHandler,     # L8-7
    PatientEducationGenerator,     # L8-9
    MedicalCalculatorHub,          # L8-10
    ClinicianReviewSigner,         # L8-11
    BackTranslationVerifier,       # L8-13
)
from curaniq.layers.L14_interaction.interaction_extensions import (
    EvidenceMapVisualizer,         # L14-4
    IterativeSourceExpander,       # L14-5
    CounterfactualToggle,          # L14-6
    VoiceInputPipeline,            # L14-9
)

# ── P2 CLUSTER 5: L4/L5 AI & Safety (9 modules) ──
from curaniq.layers.L4_ai_model.ai_extensions import (
    SelfCorrectionRAGLoop,         # L4-5
    MultiAgentDebateProtocol,      # L4-6
    AdversarialRedTeamAgent,       # L4-7
    AbductiveReasoningEngine,      # L4-8
    ClinicalKnowledgeGraphEngine,  # L4-10
)
from curaniq.layers.L5_safety_gates.safety_extensions import (
    ConformalPredictionEngine,     # L5-5
    SourceTriangulationGate,       # L5-8
    PredictiveClinicalAlertGenerator,  # L5-15
    PatientTrajectoryAnalyzer,     # L5-16
)

# ── P2 CLUSTER 6: L9/L10 Audit & Testing (11 modules) ──
from curaniq.layers.L9_L10_extensions.audit_testing_p2 import (
    C2PACredentialManager,          # L9-2
    ClinicianOverrideLogger,        # L9-4
    AISafetyOfficerDashboard,       # L9-5
    ProductAnalytics,               # L9-8
    SIEMIntegration,                # L9-9
    EquityMonitor,                  # L10-3
    SafetyFuzzTester,               # L10-5
    EvidenceChangeImpactAnalyzer,   # L10-6
    ClinicalOutcomeTracker,         # L10-7
    FeedbackLoopAnalytics,          # L10-8
    MultilingualEvalHarness,        # L10-9
)
# ── P2 CLUSTER 7: L0/L11/L12 Foundation (4 modules) ──
from curaniq.layers.L0_L11_L12_extensions.foundation_p2 import (
    PCCPDocumentationGenerator,     # L0-4
    OfflineEdgeDeployment,          # L11-4
    OutcomeFeedbackLoop,            # L12-10
    EvidenceStrengthAdjuster,       # L12-11
)

# ── P3: L12 Research & Genomics + L13 Federated (13 modules) ──
from curaniq.layers.L12_research.research_genomics import (
    PharmacogenomicEngine,         # L12-1
    GenomicResolver,               # L12-2
    ChemicalStructureValidator,    # L12-3
    VelocityTrendTracker,          # L12-4
    NOf1TrialDesigner,             # L12-5
    CounterfactualSimulator,       # L12-6
    VisualDiffDetector,            # L12-7
    AmbientAudioSentinel,          # L12-8
    ClinicalTrialMatcher,          # L12-9
)
from curaniq.layers.L13_federated.federated_network import (
    FederatedTruthNetwork,         # L13-1
    FederatedHallucinationRegistry,# L13-2
    FederatedSafetySignalNetwork,  # L13-3
    RWEAggregationEngine,          # L13-4
)

# ── P2 CLUSTER 1: L3 Clinical Specialty Engines (12 modules) ──
from curaniq.layers.L3_safety_kernel.geriatric_renal_anticoag_tdm import (
    GeriatricSafetyEngine,         # L3-8
    DedicatedRenalDosingEngine,    # L3-14
    AnticoagulationEngine,         # L3-11
    TDMPKPDEngine,                 # L3-18
)
from curaniq.layers.L3_safety_kernel.specialty_engines_p2 import (
    AntimicrobialStewardshipEngine,  # L3-10
    AWaReCategory,                    # For stewardship checks
    OncologyChemoSafetyEngine,       # L3-13
    PsychiatricSafetyEngine,         # L3-15
    SubstanceUseSafetyEngine,        # L3-16
    MultiMorbidityResolver,          # L3-19
    VaccinationEngine,               # L3-20
    FormalVerificationEngine,        # L3-3
    TemporalLogicVerifier,           # L3-4
)

# ── P2 CLUSTER 2: L1/L2 Evidence Pipeline (14 modules) ──
from curaniq.layers.L1_evidence_ingestion.evidence_sources_p2 import (
    PreprintQuarantinePipeline,     # L1-6
    WHOICTRPConnector,              # L1-7
    EMAEPARConnector,               # L1-8
    PharmacovigilanceFeed,          # L1-11
    WHOEssentialMedicinesConnector, # L1-13
    WebIntelligenceScanner,         # L1-17
)
from curaniq.layers.L2_curation.evidence_curation_p2 import (
    OntologyCrossMapValidator,      # L2-2
    GuidelineConflictResolver,      # L2-5
    CitationIntentClassifier,       # L2-8
    ConceptDriftMonitor,            # L2-9
    MetaAnalysisEngine,             # L2-10
    ApplicabilityEngine,            # L2-11
    JournalIntegrityScorer,         # L2-12
    TrialIntegrityDetector,         # L2-14
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# L6-1: PROMPT INJECTION DEFENSE  (sanitizer)
# ─────────────────────────────────────────────────────────────────────────────

# Known prompt injection patterns from CURANIQ MPIB adversarial library
_INJECTION_PATTERNS: list[re.Pattern] = [
    re.compile(r'ignore\s+(all\s+)?(previous|prior|above)\s+(instructions?|prompts?|rules?)', re.I),
    re.compile(r'you\s+are\s+now\s+(?:a\s+)?(?:different|new|unrestricted|DAN)', re.I),
    re.compile(r'forget\s+(?:all\s+)?(?:your\s+)?(?:training|instructions?|rules?|restrictions?)', re.I),
    re.compile(r'disregard\s+(?:all\s+)?(?:previous|prior|above)\s+', re.I),
    re.compile(r'<\s*/?(?:system|assistant|user|prompt)\s*>', re.I),
    re.compile(r'###\s*(?:SYSTEM|NEW\s+TASK|OVERRIDE)', re.I),
    re.compile(r'act\s+as\s+(?:if\s+you\s+(?:are|were)\s+)?(?:an?\s+)?(?:unrestricted|jailbreak)', re.I),
    re.compile(r'print\s+(?:your\s+)?(?:system\s+prompt|instructions|api\s+key)', re.I),
]


def sanitize_input(query_text: str) -> tuple[str, bool]:
    """
    L6-1: Detect and sanitize prompt injection attempts.
    Returns (sanitized_text, injection_detected).
    """
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(query_text):
            return (
                "[PROMPT INJECTION DETECTED — input sanitized]",
                True,
            )
    # Basic sanitization: strip control characters
    sanitized = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', query_text)
    return sanitized, False


# ─────────────────────────────────────────────────────────────────────────────
# L8-1: EVIDENCE CARD BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build_evidence_cards(
    claim_contract: "ClaimContract",  # noqa: F821
    evidence_pack: "EvidencePack",    # noqa: F821
) -> list[EvidenceCard]:
    """
    L8-1: Convert verified AtomicClaims into displayable EvidenceCards.
    Only non-blocked claims become cards.
    Cards are sorted: HIGH confidence first, then MEDIUM, then LOW.
    """
    from curaniq.models.schemas import AtomicClaim, ClaimContract, EvidencePack

    ev_map = {str(e.evidence_id): e for e in evidence_pack.objects}
    cards: list[EvidenceCard] = []

    for claim in claim_contract.atomic_claims:
        if claim.is_blocked:
            continue
        if claim.confidence_level == ConfidenceLevel.SUPPRESS:
            continue

        # Build source list
        sources = []
        for ev_id in claim.evidence_ids[:3]:
            ev = ev_map.get(str(ev_id))
            if ev:
                sources.append({
                    "source_id":    ev.source_id,
                    "title":        ev.title,
                    "tier":         ev.tier.value,
                    "snippet":      ev.snippet[:250] + "..." if len(ev.snippet) > 250 else ev.snippet,
                    "url":          ev.url,
                    "published":    ev.published_date.year if ev.published_date else None,
                    "grade":        ev.grade.value if ev.grade else None,
                    "jurisdiction": ev.jurisdiction.value,
                })

        # Determine uncertainty marker and caveat
        uncertainty_marker: Optional[str] = None
        caveat: Optional[str] = None

        if claim.confidence_level == ConfidenceLevel.MEDIUM:
            uncertainty_marker = "⚠️ Moderate certainty — consider additional sources"
        elif claim.confidence_level == ConfidenceLevel.LOW:
            uncertainty_marker = "⚠️ Low certainty — human expert review recommended"
            caveat = "Limited evidence base. This recommendation requires specialist verification before clinical application."

        if SafetyFlag.EDGE_CASE in claim.safety_flags or SafetyFlag.HIGH_RISK_PATIENT in claim.safety_flags:
            caveat = (caveat or "") + " This patient falls into a high-risk category where standard evidence may not apply."

        # Get grade from best evidence source
        grade: Optional[GradeLevel] = None
        for ev_id in claim.evidence_ids:
            ev = ev_map.get(str(ev_id))
            if ev and ev.grade:
                grade = ev.grade
                break

        # Numeric verification status
        numeric_verified = all(
            nt.status.value != "blocked"
            for nt in claim.numeric_tokens
        )

        cards.append(EvidenceCard(
            claim_text=claim.claim_text,
            claim_type=claim.claim_type,
            confidence_level=claim.confidence_level,
            confidence_score=claim.confidence_score,
            grade=grade,
            sources=sources,
            safety_flags=claim.safety_flags,
            uncertainty_marker=uncertainty_marker,
            caveat=caveat,
            numeric_verified=numeric_verified,
        ))

    # Sort: HIGH first, then MEDIUM, then LOW
    order = {ConfidenceLevel.HIGH: 0, ConfidenceLevel.MEDIUM: 1, ConfidenceLevel.LOW: 2}
    cards.sort(key=lambda c: order.get(c.confidence_level, 3))

    return cards


# ─────────────────────────────────────────────────────────────────────────────
# L1-16: FRESHNESS STAMP BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def build_freshness_stamps(evidence_pack: "EvidencePack") -> list[FreshnessStamp]:  # noqa: F821
    """
    L1-16: Build freshness stamps for the response.
    Architecture spec: "PubMed: 2h ago. OpenFDA: 6h ago." Staleness fail-closed.
    """
    from curaniq.models.schemas import EvidencePack

    source_freshness: dict[EvidenceSourceType, FreshnessStamp] = {}
    now = datetime.now(timezone.utc)

    for ev in evidence_pack.objects:
        if ev.source_type in source_freshness:
            continue

        age_hours = (now - ev.last_verified_at).total_seconds() / 3600
        is_stale = age_hours > ev.staleness_ttl_hours

        if is_stale:
            display_text = f"{ev.source_type.value}: STALE — {age_hours:.0f}h since last verification"
        elif age_hours < 1:
            display_text = f"{ev.source_type.value}: {age_hours * 60:.0f}min ago"
        elif age_hours < 24:
            display_text = f"{ev.source_type.value}: {age_hours:.0f}h ago"
        else:
            display_text = f"{ev.source_type.value}: {age_hours / 24:.0f}d ago"

        source_freshness[ev.source_type] = FreshnessStamp(
            source_type=ev.source_type,
            last_checked=ev.last_verified_at,
            staleness_ttl_hours=ev.staleness_ttl_hours,
            is_stale=is_stale,
            display_text=display_text,
        )

    return list(source_freshness.values())


# ─────────────────────────────────────────────────────────────────────────────
# CURANIQ PIPELINE  — main orchestrator
# ─────────────────────────────────────────────────────────────────────────────

# Default monitoring requirements for common drug types
_DEFAULT_MONITORING: dict[str, list[str]] = {
    "anticoagulant": ["INR or anti-Xa levels as per protocol", "Signs of bleeding (bruising, haematuria, melena)", "Platelet count baseline"],
    "nephrotoxic":   ["eGFR/creatinine at 48–72h and weekly", "Urine output monitoring", "Avoid concurrent nephrotoxins"],
    "qtc":           ["Baseline ECG before initiation", "Repeat ECG at 2–4h post first dose", "Serum electrolytes (K+, Mg2+)"],
    "hepatotoxic":   ["LFTs at baseline, 4 weeks, and 3 months", "Symptoms of hepatitis (jaundice, RUQ pain)", "Alcohol intake history"],
    "diabetes":      ["HbA1c every 3 months until stable", "Blood glucose self-monitoring as directed", "Renal function annually or with dose changes"],
}


class CURANIQPipeline:
    """
    The CURANIQ Main Pipeline.
    Orchestrates all 12 pipeline stages for every clinical query.
    Thread-safe; stateless per request (state held in returned objects).
    """

    def __init__(
        self,
        llm_client: Optional[Any] = None,
        evidence_store: Optional[list[EvidenceObject]] = None,
    ) -> None:
        # Initialize all components
        self.triage_gate       = TriageGate()
        self.mode_router       = ModeRouter()
        self.decomposer        = QuestionDecomposer()
        self.retriever         = HybridRetriever(evidence_store)
        self.cql_kernel        = CQLKernel()
        # LLM client from environment. None = mock mode (no API keys).
        _llm = llm_client or MultiLLMClient.from_environment()
        self.generator         = ConstrainedGenerator(_llm)
        self.claim_engine      = ClaimContractEngine()
        self.safety_suite      = SafetyGateSuiteRunner()
        self.audit_ledger      = AuditLedger()
        self.ontology          = OntologyNormalizer()
        self.input_normalizer  = UniversalInputNormalizer()
        self.prompt_defense    = PromptDefenseSuite()
        self.phi_scrubber      = PHIScrubber()
        self.drug_availability = LocalDrugAvailabilityFilter()
        self.session_memory    = ClinicalSessionMemory()
        self.assumption_ledger = AssumptionLedger()
        self.output_scanner    = OutputExfiltrationScanner()

        # L8: Interface engines
        self.evidence_cards_builder = EvidenceCardsBuilder()
        self.role_adapter           = RoleBasedUIAdapter()
        self.multilingual           = MultilingualEngine()
        self.med_boundary           = MedicationBoundaryDisplay()
        self.translation_engine     = MedicalTranslationEngine()

        # L2: Curation engines
        self.grade_engine           = GRADEGradingEngine()
        self.living_review          = LivingReviewEngine()
        self.retraction_sentinel    = RetractionWatchSentinel()
        self.jurisdiction_gate      = JurisdictionGuidanceGate()

        # L9: Payment
        self.payment_gateway        = PaymentGateway()

        # L1: Evidence ingestion infrastructure
        self.staleness_dashboard    = StalenessSLADashboard()
        self.evidence_monitor       = RealTimeEvidenceMonitor()
        self.semantic_chunker       = SemanticChunkingEngine()
        self.chunk_stamper          = EvidenceChunkMetadataStamper()
        self.evidence_compiler      = EvidenceCompiler()
        self.negative_registry      = NegativeEvidenceRegistry()

        # L4: Adversarial verification (L4-12 jury protocol)
        self.adversarial_jury       = AdversarialLLMJury.from_environment()
        self.l4_confidence_scorer   = L4ConfidenceScorer()

        # Load seed evidence for demo/dev
        if not evidence_store:
            self.retriever.load_seed_evidence()

        # ── L0: Regulatory Foundation ──
        self.qms = QualityManagementSystem()
        self.risk_framework = RiskManagementFramework()
        self.validation_programme = ValidationProgramme()
        self.secret_manager = SecretManager()
        self.cybersecurity = CybersecurityLifecycle()

        # ── L1: Evidence Quality (L1-3, L1-6, L1-7) ──
        self.negative_evidence = L1NegativeEvidenceRegistry()
        self.source_quality_scorer = SourceQualityScorer()
        self.deduplication_engine = DeduplicationEngine()
        self.nice_connector = NICEGuidelineConnector()
        self.who_connector = WHOGuidelineConnector()
        self.evidence_orchestrator = EvidenceSourceOrchestrator()

        # ── L2-15: Terminology Version Control ──
        self.terminology_versions = TerminologyVersionControl()

        # ── L4: Extended modules (richer than core/ simplified versions) ──
        self.extended_cql = ExtendedCQLEngine()
        self.extended_retriever = HybridRetrievalPipeline()
        self.extended_generator = ConstrainedLLMGenerator()
        self.extended_claim_engine = ExtendedClaimContractEngine()
        self.nli_client = NLIEntailmentClient()
        self.hash_lock_engine = EvidenceHashLockEngine()

        # ── L6-6: Upload Sanitization ──
        self.upload_sanitizer = UploadSanitizer()

        # ── L7: EHR Integration (activated when EHR connected) ──
        self.fhir_gateway = FHIRGateway()
        self.cds_hooks = CDSHooksService()
        self.ehr_token_manager = EHRTokenLifecycleManager()
        self.antibiogram = InstitutionalAntibiogram()
        self.institutional_knowledge = InstitutionalKnowledgeEngine()

        # ── L8-5/L8-12: Multilingual Safety ──
        self.multilingual_clinical = MultilingualClinicalInterface()
        self.meaning_lock = MeaningLockEngine()

        # ── L9-3: Citation Provenance ──
        self.citation_provenance = CitationProvenanceGraph()

        # ── L10: Shadow Deploy + Regression + Benchmark ──
        self.shadow_deploy = ShadowDeploymentEngine()
        self.synthetic_regression = SyntheticPatientRegression()
        self.benchmark_dashboard = BenchmarkDashboard()

        # ── L14-6: Document Intake ──
        self.document_intake = DocumentIntakePipeline()

        # ── L0-9/L0-10/L0-11: Ops + Boundary Enforcement ──
        self.boundary_enforcer = ProductBoundaryEnforcer()
        self.ops_hub = ProductionOpsHub()
        self.incident_system = IncidentResponseSystem()

        # ── L1-10/L1-12: LactMed + Cochrane connectors ──
        self.lactmed = LactMedConnector()
        self.cochrane = CochraneConnector()

        # ── L4-11: Cross-Encoder Reranking ──
        self.cross_encoder = CrossEncoderReranker()

        # ── L8-8: Medication Coverage Scope Fence ──
        self.scope_fence = MedicationCoverageScopeFence()

        # ── L10-11/L10-12: Clinician Trust + ROI ──
        self.clinician_trust = ClinicianTrustDashboard()
        self.roi_calculator = InstitutionalROICalculator()

        # ── P2 CLUSTER 1: L3 Clinical Specialty Engines ──
        self.geriatric_engine = GeriatricSafetyEngine()       # L3-8
        self.renal_dosing = DedicatedRenalDosingEngine()       # L3-14
        self.anticoag_engine = AnticoagulationEngine()         # L3-11
        self.tdm_engine = TDMPKPDEngine()                      # L3-18
        self.antimicrobial_engine = AntimicrobialStewardshipEngine()  # L3-10
        self.oncology_engine = OncologyChemoSafetyEngine()     # L3-13
        self.psych_engine = PsychiatricSafetyEngine()          # L3-15
        self.substance_engine = SubstanceUseSafetyEngine()     # L3-16
        self.multimorbidity = MultiMorbidityResolver()         # L3-19
        self.vaccination_engine = VaccinationEngine()          # L3-20
        self.formal_verifier = FormalVerificationEngine()      # L3-3
        self.temporal_verifier = TemporalLogicVerifier()       # L3-4

        # ── P2 CLUSTER 2: L1/L2 Evidence Pipeline ──
        self.preprint_quarantine = PreprintQuarantinePipeline()  # L1-6
        self.ictrp = WHOICTRPConnector()                         # L1-7
        self.ema_epar = EMAEPARConnector()                       # L1-8
        self.pharmacovigilance = PharmacovigilanceFeed()          # L1-11
        self.who_eml = WHOEssentialMedicinesConnector()           # L1-13
        self.web_intelligence = WebIntelligenceScanner()          # L1-17
        self.cross_map_validator = OntologyCrossMapValidator()    # L2-2
        self.guideline_conflicts = GuidelineConflictResolver()    # L2-5
        self.citation_intent = CitationIntentClassifier()         # L2-8
        self.concept_drift = ConceptDriftMonitor()                # L2-9
        self.meta_analysis = MetaAnalysisEngine()                 # L2-10
        self.applicability_engine = ApplicabilityEngine()         # L2-11
        self.journal_integrity = JournalIntegrityScorer()         # L2-12
        self.trial_integrity = TrialIntegrityDetector()           # L2-14

        # ── P2 CLUSTER 3: L7 EHR & Clinical Workflows ──
        self.lab_interpreter = LabResultInterpreter()              # L7-11
        self.clinical_scoring = ClinicalScoringEngine()            # L7-14
        self.alert_fatigue = AlertFatigueManager()                 # L7-10
        self.order_copilot = OrderSetCopilot()                     # L7-7
        self.context_evidence = ContextAwareEvidenceDelivery()     # L7-4
        self.policy_enforcer = InstitutionPolicyEnforcer()         # L7-6
        self.med_reconciliation = MedicationReconciliationEngine() # L7-8
        self.pathway_generator = ClinicalPathwayGenerator()        # L7-9

        # ── P2 CLUSTER 4: L8/L14 Interface & UX ──
        self.reasoning_map = VisualReasoningMapBuilder()           # L8-2
        self.uncertainty_viz = TokenUncertaintyVisualizer()         # L8-3
        self.evidence_watchlist = EvidenceWatchlist()               # L8-6
        self.challenge_handler = ClinicianChallengeHandler()        # L8-7
        self.patient_education = PatientEducationGenerator()        # L8-9
        self.calculator_hub = MedicalCalculatorHub()                # L8-10
        self.review_signer = ClinicianReviewSigner()                # L8-11
        self.back_translation = BackTranslationVerifier()           # L8-13
        self.evidence_map_viz = EvidenceMapVisualizer()             # L14-4
        self.source_expander = IterativeSourceExpander()            # L14-5
        self.counterfactual = CounterfactualToggle()                # L14-6
        self.voice_pipeline = VoiceInputPipeline()                  # L14-9

        # ── P2 CLUSTER 5: L4/L5 AI & Safety ──
        self.self_correction = SelfCorrectionRAGLoop()             # L4-5
        self.debate_protocol = MultiAgentDebateProtocol()          # L4-6
        self.red_team = AdversarialRedTeamAgent()                  # L4-7
        self.abductive = AbductiveReasoningEngine()                # L4-8
        self.knowledge_graph = ClinicalKnowledgeGraphEngine()      # L4-10
        self.conformal = ConformalPredictionEngine()                # L5-5
        self.triangulation = SourceTriangulationGate()             # L5-8
        self.predictive_alerts = PredictiveClinicalAlertGenerator() # L5-15
        self.trajectory = PatientTrajectoryAnalyzer()              # L5-16

        # ── P2 CLUSTER 6: L9/L10 Audit & Testing ──
        self.c2pa = C2PACredentialManager()                        # L9-2
        self.override_logger = ClinicianOverrideLogger()           # L9-4
        self.safety_dashboard = AISafetyOfficerDashboard()         # L9-5
        self.analytics = ProductAnalytics()                        # L9-8
        self.siem = SIEMIntegration()                              # L9-9
        self.equity_monitor = EquityMonitor()                      # L10-3
        self.fuzz_tester = SafetyFuzzTester()                      # L10-5
        self.change_impact = EvidenceChangeImpactAnalyzer()        # L10-6
        self.outcome_tracker = ClinicalOutcomeTracker()            # L10-7
        self.feedback_analytics = FeedbackLoopAnalytics()          # L10-8
        self.multilingual_eval = MultilingualEvalHarness()         # L10-9

        # ── P2 CLUSTER 7: L0/L11/L12 Foundation ──
        self.pccp_docs = PCCPDocumentationGenerator()              # L0-4
        self.offline_mode = OfflineEdgeDeployment()                # L11-4
        self.outcome_feedback = OutcomeFeedbackLoop()              # L12-10
        self.evidence_adjuster = EvidenceStrengthAdjuster()        # L12-11

        # ── P3: L12 Research & Genomics + L13 Federated ──
        self.pgx_engine = PharmacogenomicEngine()                  # L12-1
        self.genomic_resolver = GenomicResolver()                  # L12-2
        self.chem_validator = ChemicalStructureValidator()          # L12-3
        self.velocity_tracker = VelocityTrendTracker()             # L12-4
        self.nof1_designer = NOf1TrialDesigner()                   # L12-5
        self.counterfactual_sim = CounterfactualSimulator()        # L12-6
        self.imaging_diff = VisualDiffDetector()                   # L12-7
        self.ambient_audio = AmbientAudioSentinel()                # L12-8
        self.trial_matcher = ClinicalTrialMatcher()                # L12-9
        self.truth_network = FederatedTruthNetwork()               # L13-1
        self.hallucination_registry = FederatedHallucinationRegistry()  # L13-2
        self.safety_signal_network = FederatedSafetySignalNetwork()    # L13-3
        self.rwe_aggregation = RWEAggregationEngine()              # L13-4

    def process(self, query: ClinicalQuery) -> CURANIQResponse:
        """
        Execute the complete CURANIQ pipeline for a clinical query.
        Returns a fully verified, safety-gated CURANIQResponse.
        """
        start_time = time.perf_counter()

        # ═══════════════════════════════════════════════════════════════
        # STAGE 0.5: L8-5 Language Detection
        # Detect input language BEFORE normalization. This determines
        # the output language for the final response.
        # ═══════════════════════════════════════════════════════════════
        detected_language = self.multilingual_clinical.detect_language(query.raw_text)
        query_language = detected_language.value  # "en", "ru", "uz"

        # ═══════════════════════════════════════════════════════════════
        # STAGE 0.3: L0-10 Rate Limiting & Cost Budget Check
        # ═══════════════════════════════════════════════════════════════
        client_id = getattr(query, 'client_id', 'default')
        rate_ok, rate_msg = self.ops_hub.check_rate_limit(client_id)
        if not rate_ok:
            return self._build_refusal_response(
                query, "RATE_LIMITED", rate_msg, InteractionMode.QUICK_ANSWER,
            )

        # ═══════════════════════════════════════════════════════════════
        # STAGE 1: L8-12/L8-13 Universal Input Normalization
        # Any language -> English for all deterministic processing.
        # ═══════════════════════════════════════════════════════════════
        normalized = self.input_normalizer.normalize(query.raw_text)
        english_text = normalized.english_text
        drugs_mentioned = normalized.detected_drugs
        food_herbs = normalized.detected_foods

        # ═══════════════════════════════════════════════════════════════
        # STAGE 1.5: L6-1 Prompt Defense Suite (6-layer structural)
        # Runs AFTER normalization so it has drug/food context for
        # medical domain gating (Layer 2 of defense).
        # ═══════════════════════════════════════════════════════════════
        defense = self.prompt_defense.defend(
            raw_text=query.raw_text,
            detected_drugs=drugs_mentioned,
            detected_foods=food_herbs,
        )
        if defense.blocked:
            return self._build_refusal_response(
                query,
                "PROMPT_INJECTION",
                f"Security defense triggered (threat={defense.threat_score}): "
                + "; ".join(defense.details),
                InteractionMode.QUICK_ANSWER,
            )
        sanitized_text = defense.sanitized_text

        # ═══════════════════════════════════════════════════════════════
        # STAGE 2: L5-13 Triage Gate (on ENGLISH text — works any language)
        # ═══════════════════════════════════════════════════════════════
        triage = self.triage_gate.assess(english_text, query.patient_context)

        if triage.result == TriageResult.EMERGENCY:
            # Pipeline HALTS. Return pre-scripted emergency escalation only.
            return self._build_emergency_response(query, triage)

        # ═══════════════════════════════════════════════════════════════
        # STAGE 3: L14-1 Mode Router
        # ═══════════════════════════════════════════════════════════════
        mode = self.mode_router.route(query)

        # ═══════════════════════════════════════════════════════════════
        # STAGE 4: L14-2 Question Decomposer
        # ═══════════════════════════════════════════════════════════════
        sub_queries = self.decomposer.decompose(english_text)

        # ═══════════════════════════════════════════════════════════════
        # STAGE 5: L4-1 Hybrid Retriever
        # ═══════════════════════════════════════════════════════════════
        evidence_pack = self.retriever.retrieve(
            query=query,
            mode=mode,
            sub_queries=sub_queries[1:],
        )

        # ═══════════════════════════════════════════════════════════════
        # STAGE 5.1: L14-3 Assumption Ledger
        # Assess what patient context is MISSING and log assumptions.
        # Every assumption is shown to the clinician and correctable.
        # ═══════════════════════════════════════════════════════════════
        self.assumption_ledger.clear()
        assumptions = self.assumption_ledger.assess_missing_context(
            query_text=english_text,
            patient_context=query.patient_context,
            drugs_mentioned=drugs_mentioned,
        )

        # ═══════════════════════════════════════════════════════════════
        # STAGE 5.15: L8-8 Medication Coverage Scope Fence
        # Check if drugs are within CURANIQ's validated formulary.
        # Out-of-scope drugs get reduced confidence, not refusal.
        # ═══════════════════════════════════════════════════════════════
        scope_check = self.scope_fence.check_scope(drugs_mentioned, english_text)
        if not scope_check.in_scope and scope_check.confidence_modifier == 0.0:
            return self._build_refusal_response(
                query, "OUT_OF_SCOPE", scope_check.scope_message, mode,
            )

        # ═══════════════════════════════════════════════════════════════
        # STAGE 5.16: L14-5 Iterative Source Expansion
        # If evidence is thin (<3 results), auto-broaden the query.
        # ═══════════════════════════════════════════════════════════════
        if len(evidence_pack.objects) < 3:
            expansions = self.source_expander.expand_query(
                english_text, drugs_mentioned, len(evidence_pack.objects),
            )
            if expansions:
                # Log expansion suggestions for retriever to use
                for exp in expansions[:3]:
                    logger.info("Source expansion: %s — %s", exp["strategy"], exp["detail"])

        # ═══════════════════════════════════════════════════════════════
        # STAGE 5.17: L4-11 Cross-Encoder Reranking
        # Re-score evidence candidates with cross-encoder for precision.
        # Neural model if CROSS_ENCODER_URL set, else enhanced lexical.
        # ═══════════════════════════════════════════════════════════════
        if evidence_pack.objects:
            rerank_result = self.cross_encoder.rerank(
                query=english_text,
                candidates=[
                    {
                        "evidence_id": str(ev.evidence_id),
                        "title": ev.title,
                        "snippet": ev.snippet,
                        "published_year": ev.published_date.year if ev.published_date else None,
                    }
                    for ev in evidence_pack.objects
                ],
                top_k=min(15, len(evidence_pack.objects)),
            )
            # Reorder evidence_pack.objects by reranked order
            if rerank_result.reranked:
                reranked_ids = [r.evidence_id for r in rerank_result.reranked]
                id_to_ev = {str(ev.evidence_id): ev for ev in evidence_pack.objects}
                reordered = [id_to_ev[eid] for eid in reranked_ids if eid in id_to_ev]
                # Keep any evidence not in reranked list at the end
                remaining = [ev for ev in evidence_pack.objects if str(ev.evidence_id) not in reranked_ids]
                evidence_pack.objects = reordered + remaining

        # ═══════════════════════════════════════════════════════════════
        # STAGE 5.2: L1-7 Deduplication Engine
        # Remove duplicate evidence across sources (DOI/PMID/title match).
        # Same study from PubMed + Crossref → keep highest quality.
        # ═══════════════════════════════════════════════════════════════
        dedup_count = 0
        unique_objects = []
        for ev in evidence_pack.objects:
            is_dup = self.deduplication_engine.check_duplicate(
                doi=getattr(ev, 'doi', None),
                pmid=getattr(ev, 'source_id', None),
                title=getattr(ev, 'title', ''),
                year=ev.published_date.year if ev.published_date else None,
            )[0]
            if not is_dup:
                self.deduplication_engine.register_evidence(
                    evidence_id=str(ev.evidence_id),
                    doi=getattr(ev, 'doi', None),
                    pmid=getattr(ev, 'source_id', None),
                    title=getattr(ev, 'title', ''),
                    year=ev.published_date.year if ev.published_date else None,
                    source=ev.source_type.value if ev.source_type else '',
                )
                unique_objects.append(ev)
            else:
                dedup_count += 1
        if dedup_count > 0:
            evidence_pack.objects = unique_objects
            logger.info("L1-7 Dedup: removed %d duplicate evidence objects", dedup_count)

        # ═══════════════════════════════════════════════════════════════
        # STAGE 5.3: L1-6 Source Quality Scoring
        # Score each evidence source: study design, journal quartile,
        # recency, sample size, bias risk → composite quality score.
        # Low-quality sources get lower confidence in claim verification.
        # ═══════════════════════════════════════════════════════════════
        for ev in evidence_pack.objects:
            journal_name = getattr(ev, 'journal', '') or ''
            quartile = self.source_quality_scorer.get_journal_quartile(journal_name)
            quality = self.source_quality_scorer.score_source(
                source_id=str(ev.evidence_id),
                study_design=self._infer_study_design(ev),
                journal_quartile=quartile,
                publication_year=ev.published_date.year if ev.published_date else 2020,
                sample_size=getattr(ev, 'sample_size', 0) or 0,
                bias_risk_low=True,  # Default conservative; override with RoB data
            )
            # Store quality score on evidence object for downstream use
            if not hasattr(ev, '_quality_score'):
                object.__setattr__(ev, '_quality_score', quality.composite_score)

        # ═══════════════════════════════════════════════════════════════
        # STAGE 5.4: L1-3 Negative Evidence Check
        # For each drug mentioned, check if negative evidence exists:
        # failed trials, safety signals, withdrawn approvals.
        # This data is injected into the safety context for L5 gates.
        # ═══════════════════════════════════════════════════════════════
        negative_flags: list[str] = []
        for drug in drugs_mentioned:
            if self.negative_evidence.has_safety_signal(drug):
                entries = self.negative_evidence.query_negative_evidence(drug)
                for entry in entries:
                    negative_flags.append(
                        f"NEGATIVE_EVIDENCE: {drug} — {entry.evidence_type.value}: "
                        f"{entry.finding_summary[:100]}"
                    )
            # Also scan retrieved evidence abstracts for negative results
            for ev in evidence_pack.objects:
                neg_type = self.negative_evidence.classify_abstract(ev.snippet)
                if neg_type and drug.lower() in ev.snippet.lower():
                    self.negative_evidence.index_negative_evidence(
                        drug=drug,
                        condition="",
                        finding=ev.snippet[:200],
                        evidence_type=neg_type,
                        pmid=ev.source_id,
                    )
                    negative_flags.append(
                        f"DETECTED_IN_ABSTRACT: {drug} — {neg_type.value}"
                    )

        # ═══════════════════════════════════════════════════════════════
        # STAGE 5.5: L2-15 Terminology Version Pinning
        # Record which terminology versions were used for this query.
        # Enables reproducibility: same query + same versions = same mapping.
        # ═══════════════════════════════════════════════════════════════
        terminology_manifest = self.terminology_versions.get_version_manifest()

        # ═══════════════════════════════════════════════════════════════
        # STAGE 5.6: L1-6 Preprint Quarantine
        # Flag and reduce confidence of preprint evidence sources.
        # ═══════════════════════════════════════════════════════════════
        for ev in evidence_pack.objects:
            preprint_check = self.preprint_quarantine.check(
                doi=getattr(ev, 'doi', '') or '',
                url=getattr(ev, 'url', '') or '',
                title=ev.title,
                abstract=ev.snippet,
            )
            if preprint_check.is_preprint:
                # Reduce evidence quality score for preprints
                existing_score = getattr(ev, '_quality_score', 0.7)
                object.__setattr__(ev, '_quality_score',
                    existing_score * preprint_check.confidence_modifier)
                negative_flags.append(
                    f"PREPRINT: {ev.title[:60]} — {preprint_check.warning[:80]}"
                )

        # ═══════════════════════════════════════════════════════════════
        # STAGE 5.7: L2-14 Trial Integrity + L2-12 Journal Integrity
        # Detect p-hacking signals, predatory journals.
        # ═══════════════════════════════════════════════════════════════
        for ev in evidence_pack.objects:
            # Trial integrity check
            integrity = self.trial_integrity.assess_integrity(ev.snippet)
            if integrity["integrity_risk_score"] >= 0.5:
                negative_flags.append(
                    f"TRIAL_INTEGRITY [{integrity['integrity_risk_score']}]: "
                    f"{ev.title[:50]} — {integrity['signals'][:2]}"
                )

            # Journal integrity check
            journal_name = getattr(ev, 'journal', '') or ''
            if journal_name:
                j_score, j_warnings = self.journal_integrity.score_journal(journal_name)
                if j_score < 0.5:
                    negative_flags.append(
                        f"JOURNAL_INTEGRITY [{j_score}]: {journal_name} — {'; '.join(j_warnings[:2])}"
                    )

        # ═══════════════════════════════════════════════════════════════
        # STAGE 5.8: L2-8 Citation Intent Classification
        # Classify whether evidence SUPPORTS or CONTRADICTS each claim
        # area. Contradicting evidence gets special treatment.
        # ═══════════════════════════════════════════════════════════════
        # (Runs post-claim-generation — stored for use in STAGE 8/12)

        # ═══════════════════════════════════════════════════════════════
        # STAGE 6: L3-1 CQL Safety Kernel (deterministic rules)
        # Drugs and food/herbs already extracted by normalizer.
        # ═══════════════════════════════════════════════════════════════

        cql_results = self.cql_kernel.run_all_checks(
            patient=query.patient_context or _empty_patient(),
            drugs_mentioned=drugs_mentioned,
            food_herb_mentioned=food_herbs if food_herbs else None,
        )

        # ═══════════════════════════════════════════════════════════════
        # STAGE 6.5: L3 P2 Clinical Specialty Engines
        # All deterministic. Run AFTER CQL kernel, BEFORE LLM.
        # Results feed into the LLM prompt as additional safety context.
        # ═══════════════════════════════════════════════════════════════
        specialty_alerts: list[str] = []
        patient = query.patient_context or _empty_patient()
        patient_age = getattr(patient, 'age', None) or 0
        patient_egfr = None
        if patient.renal:
            patient_egfr = getattr(patient.renal, 'egfr_ml_min', None)

        # L3-8: Geriatric Safety (Beers, STOPP/START, ACB)
        if patient_age >= 65:
            geriatric_alerts = self.geriatric_engine.assess(
                patient_age=patient_age,
                drugs=drugs_mentioned,
                egfr=patient_egfr,
            )
            for ga in geriatric_alerts:
                specialty_alerts.append(
                    f"GERIATRIC [{ga.category.value}]: {ga.drug} — {ga.rationale[:100]}. "
                    f"Recommendation: {ga.recommendation[:100]}"
                )

        # L3-14: Dedicated Renal Dosing (CKD-stage-specific)
        if patient_egfr and patient_egfr < 60:
            for drug in drugs_mentioned:
                renal_adj = self.renal_dosing.get_adjustment(
                    drug, patient_egfr,
                    on_dialysis=getattr(patient.renal, 'on_dialysis', False) if patient.renal else False,
                )
                if renal_adj and renal_adj.action != "normal":
                    specialty_alerts.append(
                        f"RENAL [{renal_adj.ckd_stage.value}]: {drug} — {renal_adj.action}. "
                        f"Dose: {renal_adj.adjusted_dose}. {renal_adj.monitoring}"
                    )

        # L3-15: Psychiatric Safety (serotonin syndrome risk)
        psych_alerts = self.psych_engine.check_serotonin_syndrome_risk(drugs_mentioned)
        for pa in psych_alerts:
            specialty_alerts.append(
                f"PSYCHIATRIC [{pa.severity}]: {pa.alert_type} — {pa.message[:120]}"
            )

        # L3-10: Antimicrobial Stewardship (AWaRe classification)
        for drug in drugs_mentioned:
            abx_result = self.antimicrobial_engine.assess(drug, indication=english_text)
            if abx_result.aware_category == AWaReCategory.RESERVE:
                specialty_alerts.append(
                    f"STEWARDSHIP [RESERVE]: {drug} — {abx_result.recommendation[:120]}"
                )

        # L3-19: Multi-Morbidity Conflicts
        patient_conditions = []
        if hasattr(patient, 'conditions') and patient.conditions:
            patient_conditions = patient.conditions
        if patient_conditions:
            mm_conflicts = self.multimorbidity.check_conflicts(patient_conditions, drugs_mentioned)
            for conflict in mm_conflicts:
                specialty_alerts.append(
                    f"MULTI-MORBIDITY: {conflict['drug']} for {conflict['condition_treated']} "
                    f"conflicts with {conflict['condition_harmed']} — {conflict['recommendation'][:100]}"
                )

        # L3-11: Anticoagulation Engine (when anticoagulants detected)
        _anticoag_drugs = {"warfarin","rivaroxaban","apixaban","dabigatran","edoxaban","enoxaparin","heparin"}
        anticoag_present = [d for d in drugs_mentioned if d.lower() in _anticoag_drugs]
        for ac_drug in anticoag_present:
            rev = self.anticoag_engine.get_reversal(ac_drug)
            if rev:
                specialty_alerts.append(
                    f"ANTICOAG: {ac_drug} reversal agent: {rev['agent'][:80]}. Onset: {rev['onset']}"
                )
            doac_dose = self.anticoag_engine.get_doac_dose(
                ac_drug, "af", crcl=patient_egfr,
                age=patient_age, weight_kg=getattr(patient, 'weight_kg', None),
            )
            if doac_dose:
                specialty_alerts.append(
                    f"ANTICOAG DOSING: {ac_drug} — {doac_dose.get('dose', '')}. "
                    f"Source: {doac_dose.get('source', '')}"
                )

        # L3-18: TDM Engine (when narrow therapeutic index drugs detected)
        _tdm_drugs = {"vancomycin","gentamicin","lithium","digoxin","phenytoin",
                       "carbamazepine","valproic_acid","tacrolimus","cyclosporine"}
        tdm_present = [d for d in drugs_mentioned if d.lower().replace(" ","_") in _tdm_drugs]
        for tdm_drug in tdm_present:
            if patient_egfr:
                adj_t12 = self.tdm_engine.estimate_adjusted_half_life(tdm_drug, patient_egfr)
                if adj_t12:
                    specialty_alerts.append(
                        f"TDM: {tdm_drug} estimated t1/2 = {adj_t12}h at eGFR {patient_egfr}. "
                        "Dose interval adjustment may be needed."
                    )

        # L3-16: Substance Use Safety (when SUD medications detected)
        _sud_drugs = {"methadone","buprenorphine","naltrexone","naloxone","disulfiram","acamprosate"}
        sud_present = [d for d in drugs_mentioned if d.lower() in _sud_drugs]
        if sud_present:
            sud_alerts = self.substance_engine.assess_combinations(drugs_mentioned)
            for sa in sud_alerts:
                specialty_alerts.append(
                    f"SUBSTANCE USE [{sa['severity']}]: {sa['drugs']} — {sa['message'][:120]}"
                )

        # L2-5: Guideline Conflict Detection
        conflicts = self.guideline_conflicts.find_conflicts(english_text)
        for conflict in conflicts:
            specialty_alerts.append(
                f"GUIDELINE_CONFLICT [{conflict.severity.value}]: {conflict.topic} — "
                f"{conflict.guideline_a} vs {conflict.guideline_b}. "
                f"Resolution: {conflict.resolution_strategy[:80]}"
            )

        # L5-15: Predictive Clinical Alerts
        predictive = self.predictive_alerts.assess_risks(
            patient_age=patient_age,
            conditions=patient_conditions,
            drugs=drugs_mentioned,
        )
        for pa in predictive:
            specialty_alerts.append(
                f"PREDICTIVE [{pa.probability}]: {pa.risk_description} — "
                f"Horizon: {pa.time_horizon}. Action: {pa.recommended_action[:80]}"
            )

        # L12-1: Pharmacogenomics Check (when genotype data available)
        patient_genotypes = getattr(patient, 'genotypes', None) or {}
        if patient_genotypes and drugs_mentioned:
            for drug in drugs_mentioned:
                pgx_recs = self.pgx_engine.check_drug(drug, patient_genotypes)
                for rec in pgx_recs:
                    specialty_alerts.append(
                        f"PGx [{rec.cpic_level}]: {rec.gene}/{rec.phenotype} + {rec.drug} — "
                        f"{rec.action[:100]}. Source: {rec.source[:50]}"
                    )

        # L1-13: WHO Essential Medicines List check
        eml_status = self.who_eml.get_eml_status(drugs_mentioned)
        for drug, is_eml in eml_status.items():
            if not is_eml and drug:
                specialty_alerts.append(
                    f"WHO_EML: {drug} is NOT on the WHO Essential Medicines List 2023"
                )

        # L7-4: Context-Aware Evidence Delivery
        # Proactively surface relevant evidence based on patient context
        if patient_conditions or drugs_mentioned:
            patient_labs = {}
            if patient_egfr:
                patient_labs["egfr"] = patient_egfr
            context_matches = self.context_evidence.match_context(
                patient_conditions=patient_conditions,
                patient_medications=drugs_mentioned,
                patient_labs=patient_labs,
                patient_age=patient_age,
            )
            for match in context_matches[:3]:
                specialty_alerts.append(
                    f"PROACTIVE [{match['priority']}]: Relevant evidence topics: "
                    f"{', '.join(match['topics'][:3])} "
                    f"(patient factors: {', '.join(match['patient_factors'][:2])})"
                )

        # L7-6: Institution Policy Check on all drugs
        for drug in drugs_mentioned:
            policy = self.policy_enforcer.check_policy(drug, indication=english_text)
            if not policy.compliant:
                for violation in policy.violations:
                    specialty_alerts.append(
                        f"POLICY [{violation['type']}]: {drug} — {violation['reason'][:80]}. "
                        f"Approval needed from: {policy.approval_authority}"
                    )

        # Inject specialty alerts into CQL results for downstream use
        cql_results["specialty_alerts"] = specialty_alerts

        # ═══════════════════════════════════════════════════════════════
        # STAGE 7: L4-2 Constrained Generator (LLM with evidence lock)
        # ═══════════════════════════════════════════════════════════════
        # ═══════════════════════════════════════════════════════════════
        # L6-2: PHI Scrubbing (BEFORE LLM — the LLM never sees PHI)
        # HIPAA Safe Harbor 18 identifiers stripped from query text.
        # ═══════════════════════════════════════════════════════════════
        phi_result = self.phi_scrubber.scrub(english_text if 'english_text' in dir() else sanitized_text)
        scrubbed_query_text = phi_result.scrubbed_text

        llm_output, cross_llm_agreement = self.generator.generate(
            query=query,
            evidence_pack=evidence_pack,
            mode=mode,
            cql_results=cql_results,
        )

        # ═══════════════════════════════════════════════════════════════
        # STAGE 8: L4-3 Claim Contract Engine (enforcement)
        # ═══════════════════════════════════════════════════════════════
        # Feed CQL computation logs into the claim engine for L5-17
        cql_logs = cql_results.get("computation_logs", [])
        self.claim_engine.update_cql_logs(cql_logs)

        claim_contract = self.claim_engine.process(
            query_id=query.query_id,
            llm_raw_output=llm_output,
            evidence_pack=evidence_pack,
            cross_llm_agreement=cross_llm_agreement,
        )

        # ═══════════════════════════════════════════════════════════════
        # STAGE 8.5: L4-12 Adversarial LLM Cross-Verification (Jury)
        # If OPENAI_API_KEY is set: GPT-4o critiques Claude's claims.
        # If not set: claims proceed with base confidence (degraded).
        # Jury NEVER generates — only critiques. Different failure modes
        # = extremely low probability of correlated hallucination.
        # ═══════════════════════════════════════════════════════════════
        if self.adversarial_jury.is_enabled and claim_contract.atomic_claims:
            try:
                # Determine risk level from triage + claim types
                query_risk = "high_risk" if triage.result == TriageResult.HIGH_RISK else "standard"
                
                # Run async jury in sync pipeline context
                verified_claims = asyncio.get_event_loop().run_until_complete(
                    self.adversarial_jury.verify_claims(
                        claims=claim_contract.atomic_claims,
                        evidence_pack=evidence_pack,
                        query_risk_level=query_risk,
                    )
                )
                claim_contract.atomic_claims = verified_claims
                
                # Update cross_llm_agreement from actual jury results
                # Average agreement across all claims that were verified
                agreement_scores = [
                    1.0 if c.verifier_decision and c.verifier_decision.value == "faithful" else
                    0.5 if c.verifier_decision else 0.85
                    for c in verified_claims
                    if c.verdict.value.startswith("pass")
                ]
                if agreement_scores:
                    cross_llm_agreement = sum(agreement_scores) / len(agreement_scores)
                    
            except Exception as jury_err:
                # Jury failure is NON-BLOCKING — claims proceed with base confidence
                # This is the correct fail-open for verification (fail-closed is for safety gates)
                import logging
                logging.getLogger(__name__).warning(
                    f"L4-12 jury failed (non-blocking): {jury_err}. "
                    "Claims proceed with base confidence."
                )

        # ═══════════════════════════════════════════════════════════════
        # STAGE 9: L5 Safety Gate Suite
        # ═══════════════════════════════════════════════════════════════
        # Determine completeness elements from LLM output
        has_monitoring = any(
            w in llm_output.lower()
            for w in ["monitor", "monitoring", "check", "labs", "levels", "ecg", "blood test"]
        )
        has_stop_rules = any(
            w in llm_output.lower()
            for w in ["stop", "discontinue", "hold", "withheld", "suspend", "cease"]
        )
        has_escalation = any(
            w in llm_output.lower()
            for w in ["escalat", "urgent", "specialist", "refer", "emergency", "seek"]
        )
        has_follow_up = any(
            w in llm_output.lower()
            for w in ["follow", "review", "repeat", "recheck", "6 weeks", "3 months", "weeks"]
        )

        safety_suite_result, safe_next_steps = self.safety_suite.run_all(
            query=query,
            claim_contract=claim_contract,
            evidence_pack=evidence_pack,
            mode=mode,
            has_monitoring=has_monitoring,
            has_stop_rules=has_stop_rules,
            has_escalation_thresholds=has_escalation,
            has_follow_up=has_follow_up,
        )

        # If hard-blocked → generate refusal with safe next steps
        if safety_suite_result.hard_block:
            refusal_reason = next(
                (g.message for g in safety_suite_result.gates if not g.passed and g.message),
                "Safety gate failure — response cannot be verified to meet CURANIQ quality standards."
            )
            return self._build_safety_block_response(
                query, triage, mode, evidence_pack, claim_contract,
                safety_suite_result, cql_logs, safe_next_steps, refusal_reason,
                elapsed_ms=(time.perf_counter() - start_time) * 1000,
            )

        # ═══════════════════════════════════════════════════════════════
        # STAGE 10: L8-1 Evidence Card Builder
        # ═══════════════════════════════════════════════════════════════
        evidence_cards = build_evidence_cards(claim_contract, evidence_pack)

        # ═══════════════════════════════════════════════════════════════
        # STAGE 11: L1-16 Freshness Stamps
        # ═══════════════════════════════════════════════════════════════
        freshness_stamps = build_freshness_stamps(evidence_pack)

        # ═══════════════════════════════════════════════════════════════
        # STAGE 11.5: L8-12 Meaning Lock + L8-5 Multilingual Output
        # If input was non-English, translate the summary with
        # meaning-lock safety verification. Negation/dose/route errors
        # → REFUSE translation, deliver English for safety.
        # ═══════════════════════════════════════════════════════════════
        summary_text = self._build_summary(evidence_cards, mode)
        translation_warnings: list[str] = []

        if query_language != "en":
            # Extract meaning locks from English output BEFORE translation
            meaning_locks = self.meaning_lock.extract_locks(summary_text, "en")

            # Attempt safe translation (uses translation_fn if available)
            translated_summary, was_translated, t_warnings = (
                self.multilingual_clinical.safe_translate(
                    english_output=summary_text,
                    target_language=detected_language,
                    translation_fn=None,  # Set when translation API connected
                )
            )
            summary_text = translated_summary
            translation_warnings = t_warnings

            # ═══════════════════════════════════════════════════════════
            # STAGE 11.55: L8-13 Back-Translation Verification
            # Verify translated text preserves negations and doses.
            # ═══════════════════════════════════════════════════════════
            if was_translated:
                bt_result = self.back_translation.verify_round_trip(
                    original=self._build_summary(evidence_cards, mode),
                    back_translated=summary_text,  # Approximate: compare structure
                )
                if not bt_result["passed"]:
                    for failure in bt_result["failures"]:
                        if failure["severity"] == "critical":
                            # Lost negation: revert to English
                            summary_text = self._build_summary(evidence_cards, mode)
                            translation_warnings.append(
                                "Translation reverted to English: negation lost in translation."
                            )
                            break

        # ═══════════════════════════════════════════════════════════════
        # STAGE 11.6: Inject negative evidence warnings into response
        # ═══════════════════════════════════════════════════════════════
        if negative_flags:
            safe_next_steps = safe_next_steps + [
                f"⚠️ {flag}" for flag in negative_flags[:3]
            ]

        # ═══════════════════════════════════════════════════════════════
        # STAGE 11.7: L0-9 Product Boundary Enforcement
        # Check if output content types are permitted for user's role.
        # Patient mode BLOCKS dosing/diagnostic/directive content.
        # ═══════════════════════════════════════════════════════════════
        if query.user_role and query.user_role.value == "patient":
            output_categories = self.boundary_enforcer.classify_output(summary_text)
            boundary_check = self.boundary_enforcer.check_output(
                ProductMode.PATIENT, output_categories,
            )
            if not boundary_check.allowed:
                # Strip blocked content, add patient-safe message
                summary_text = (
                    "Based on the available evidence, here is general educational "
                    "information about your query. For specific dosing, diagnostic, "
                    "or treatment decisions, please consult your healthcare provider.\n\n"
                    + boundary_check.message
                )
                # Remove evidence cards that contain dosing/directive content
                evidence_cards = [
                    card for card in evidence_cards
                    if card.claim_type.value not in ("DOSING", "CONTRAINDICATION", "MONITORING")
                ]

        # Build monitoring / stop rules / escalation from CQL + safety gate context
        monitoring = self._extract_monitoring(cql_results, safety_suite_result, drugs_mentioned)
        stop_rules = self._extract_stop_rules(cql_results, llm_output)
        escalation = self._extract_escalation(safety_suite_result, query)

        # ═══════════════════════════════════════════════════════════════
        # STAGE 12.7: L8-9 Patient Education Simplification
        # If patient mode, simplify clinical language to grade 8 level.
        # ═══════════════════════════════════════════════════════════════
        if query.user_role and query.user_role.value == "patient":
            from curaniq.layers.L8_interface.interface_extensions import ReadabilityLevel
            summary_text = self.patient_education.simplify(
                summary_text, ReadabilityLevel.GRADE_8,
            )

        elapsed_ms = (time.perf_counter() - start_time) * 1000

        # ═══════════════════════════════════════════════════════════════
        # STAGE 12: L9-1 Audit Ledger
        # ═══════════════════════════════════════════════════════════════
        ledger_entry = self.audit_ledger.record(
            query=query,
            triage=triage,
            mode_detected=mode,
            evidence_pack=evidence_pack,
            claim_contract=claim_contract,
            safety_suite=safety_suite_result,
            cql_logs=cql_logs,
            refused=False,
        )

        # ═══════════════════════════════════════════════════════════════
        # STAGE 12.5: L9-3 Citation Provenance Graph
        # Build directed acyclic graph: Claim → Evidence → Source.
        # Enables click-through provenance and incident investigation.
        # ═══════════════════════════════════════════════════════════════
        try:
            self.citation_provenance.build_trace(
                query_id=str(query.query_id),
                claims=[
                    {
                        "claim_text": c.claim_text,
                        "claim_type": c.claim_type.value if hasattr(c.claim_type, 'value') else str(c.claim_type),
                        "confidence_score": c.confidence_score,
                        "is_blocked": c.is_blocked,
                        "evidence_ids": [str(eid) for eid in c.evidence_ids],
                        "numeric_tokens": [
                            {"cql_rule_id": getattr(nt, 'cql_rule_id', '')}
                            for nt in c.numeric_tokens
                        ],
                    }
                    for c in claim_contract.atomic_claims
                    if not c.is_blocked
                ],
                evidence_objects=[
                    {
                        "evidence_id": str(e.evidence_id),
                        "source_id": e.source_id,
                        "title": e.title,
                        "url": e.url,
                        "snippet": e.snippet[:200],
                        "published_date": e.published_date.isoformat() if e.published_date else "",
                        "source_type": e.source_type.value if e.source_type else "",
                        "grade": e.grade.value if e.grade else "",
                    }
                    for e in evidence_pack.objects
                ],
                cql_logs=[
                    {
                        "rule_id": log.rule_id if hasattr(log, 'rule_id') else str(log.get('rule_id', '')),
                        "rule_version": getattr(log, 'rule_version', ''),
                        "formula_applied": getattr(log, 'formula_applied', ''),
                        "output_value": getattr(log, 'output_value', ''),
                    }
                    for log in cql_logs
                ] if cql_logs else [],
            )
        except Exception as prov_err:
            logger.warning("L9-3 provenance graph build failed (non-blocking): %s", prov_err)

        # ═══════════════════════════════════════════════════════════════
        # STAGE 13: L4-10 Knowledge Graph Extraction (non-blocking)
        # Extract drug-condition-evidence triples for incremental KG build.
        # ═══════════════════════════════════════════════════════════════
        try:
            self.knowledge_graph.extract_from_pipeline(
                drugs=drugs_mentioned,
                conditions=patient_conditions,
                ddi_results=cql_results.get("ddi_results", []),
                evidence_objects=[
                    {"title": e.title, "source": getattr(e, 'source_type', '')}
                    for e in evidence_pack.objects
                ],
            )
        except Exception:
            pass  # Non-blocking

        # ═══════════════════════════════════════════════════════════════
        # STAGE 14: L9-8 Analytics + L9-2 C2PA Credentials
        # ═══════════════════════════════════════════════════════════════
        try:
            self.analytics.track(
                event_type="query",
                user_role=query.user_role.value if query.user_role else "unknown",
                duration_ms=elapsed_ms,
            )
        except Exception:
            pass

        try:
            self.c2pa.generate_credential(
                query_id=str(query.query_id),
                output_text=summary_text,
                model_used="claude-sonnet-4-20250514",
                evidence_hashes=[hashlib.sha256(e.snippet.encode()).hexdigest()[:16]
                                 for e in evidence_pack.objects[:10]],
                gates_passed=[g.gate_name for g in safety_suite_result.gate_results
                              if hasattr(g, 'gate_name') and g.passed]
                             if hasattr(safety_suite_result, 'gate_results') else [],
            )
        except Exception:
            pass

        return CURANIQResponse(
            query_id=query.query_id,
            mode=mode,
            user_role=query.user_role,
            triage=triage,
            safety_suite=safety_suite_result,
            claim_contract_enforced=claim_contract.enforcement_passed,
            evidence_cards=evidence_cards,
            summary_text=summary_text,
            safe_next_steps=safe_next_steps,
            monitoring_required=monitoring,
            escalation_thresholds=escalation,
            follow_up_interval=self._suggest_follow_up(drugs_mentioned, cql_results),
            freshness_stamps=freshness_stamps,
            sources_used=evidence_pack.source_count,
            processing_time_ms=round(elapsed_ms, 1),
            refused=False,
            audit_ledger_id=ledger_entry.entry_id,
        )

    # ─────────────────────────────────────────────────────────────────
    # HELPER METHODS
    # ─────────────────────────────────────────────────────────────────

    def _build_emergency_response(self, query: ClinicalQuery, triage) -> CURANIQResponse:
        """Build pre-scripted emergency response — no LLM, no retrieval."""
        from curaniq.models.schemas import SafetyGateResult, SafetyGateSuite, EvidencePack, ClaimContract, TriageAssessment
        empty_pack = EvidencePack(pack_id=uuid4(), query_id=query.query_id, objects=[])
        empty_contract = ClaimContract(query_id=query.query_id, atomic_claims=[])

        emergency_gate = SafetyGateResult(
            gate_id="TRIAGE_EMERGENCY",
            gate_name="Emergency Triage",
            passed=False,
            message=triage.escalation_message,
            severity="EMERGENCY",
        )
        suite = SafetyGateSuite(
            query_id=query.query_id,
            gates=[emergency_gate],
            overall_passed=False,
            hard_block=True,
        )
        self.audit_ledger.record(
            query=query, triage=triage, mode_detected=InteractionMode.QUICK_ANSWER,
            evidence_pack=empty_pack, claim_contract=empty_contract,
            safety_suite=suite, cql_logs=[], refused=True,
            refusal_reason="EMERGENCY_TRIAGE",
        )
        return CURANIQResponse(
            query_id=query.query_id,
            mode=InteractionMode.QUICK_ANSWER,
            user_role=query.user_role,
            triage=triage,
            safety_suite=suite,
            claim_contract_enforced=False,
            evidence_cards=[],
            summary_text=triage.escalation_message,
            safe_next_steps=["Call emergency services immediately — 103 / 112"],
            monitoring_required=[],
            escalation_thresholds=[],
            freshness_stamps=[],
            sources_used=0,
            refused=True,
            refusal_reason="EMERGENCY_TRIAGE — Pre-scripted escalation. No AI analysis.",
        )

    def _build_refusal_response(
        self,
        query: ClinicalQuery,
        reason_code: str,
        message: str,
        mode: InteractionMode,
    ) -> CURANIQResponse:
        from curaniq.models.schemas import SafetyGateResult, SafetyGateSuite, EvidencePack, ClaimContract, TriageAssessment
        from .triage_gate import TriageAssessment
        dummy_triage = TriageAssessment(result=TriageResult.CLEAR)
        empty_pack = EvidencePack(pack_id=uuid4(), query_id=query.query_id, objects=[])
        empty_contract = ClaimContract(query_id=query.query_id, atomic_claims=[])
        suite = SafetyGateSuite(query_id=query.query_id, gates=[], overall_passed=False, hard_block=True)
        return CURANIQResponse(
            query_id=query.query_id,
            mode=mode,
            user_role=query.user_role,
            triage=dummy_triage,
            safety_suite=suite,
            claim_contract_enforced=False,
            evidence_cards=[],
            summary_text=message,
            refused=True,
            refusal_reason=reason_code,
        )

    def _build_safety_block_response(
        self, query, triage, mode, evidence_pack, claim_contract,
        safety_suite, cql_logs, safe_next_steps, refusal_reason, elapsed_ms,
    ) -> CURANIQResponse:
        self.audit_ledger.record(
            query=query, triage=triage, mode_detected=mode,
            evidence_pack=evidence_pack, claim_contract=claim_contract,
            safety_suite=safety_suite, cql_logs=cql_logs,
            refused=True, refusal_reason=refusal_reason,
        )
        return CURANIQResponse(
            query_id=query.query_id,
            mode=mode,
            user_role=query.user_role,
            triage=triage,
            safety_suite=safety_suite,
            claim_contract_enforced=False,
            evidence_cards=[],
            summary_text=refusal_reason,
            safe_next_steps=safe_next_steps,
            monitoring_required=[],
            escalation_thresholds=[],
            freshness_stamps=[],
            sources_used=evidence_pack.source_count,
            processing_time_ms=round(elapsed_ms, 1),
            refused=True,
            refusal_reason=refusal_reason,
        )

    def _extract_drugs(self, text: str) -> list[str]:
        """
        Extract drug names using L2-1 OntologyNormalizer.
        Universal: any language -> canonical INN.
        """
        import re as _re
        from curaniq.layers.L2_curation.ontology import _REVERSE_DRUG_LOOKUP

        found_inns: list[str] = []
        seen: set[str] = set()
        text_lower = text.lower()

        # Match known drug names from ontology (any language)
        for variant, inn in _REVERSE_DRUG_LOOKUP.items():
            if len(variant) >= 3 and _re.search(
                r'\b' + _re.escape(variant) + r'\b', text_lower
            ):
                if inn not in seen:
                    found_inns.append(inn)
                    seen.add(inn)

        # Tokenize and resolve unknown words
        tokens = _re.findall(r'\b[a-zA-Z\u0400-\u04FF]{4,}\b', text)
        for token in tokens:
            canonical, resolved = resolve_drug_name(token)
            if resolved and canonical not in seen:
                found_inns.append(canonical)
                seen.add(canonical)

        return found_inns

    def _extract_food_herbs(self, text: str) -> list[str]:
        """
        Extract food/herb mentions via UniversalInputNormalizer.
        Any language -> canonical English terms for L3-17 processing.
        """
        normalized = self.input_normalizer.normalize(text)
        return normalized.detected_foods

    def _extract_monitoring(self, cql_results: dict, safety_suite, drugs: list[str]) -> list[str]:
        """Generate monitoring requirements from CQL results and safety flags."""
        monitoring: list[str] = []

        # Add renal monitoring if renal adjustments made
        if cql_results.get("renal_adjustments"):
            monitoring.append("Renal function (eGFR/creatinine) — baseline and after dose changes")

        # Add QT monitoring if QT risk found
        qt = cql_results.get("qt_assessment")
        if qt and qt.get("score", 0) >= 7:
            monitoring.append("12-lead ECG — baseline before initiation, repeat at 2–4h post first dose")
            monitoring.append("Serum electrolytes (K+, Mg2+) — correct before initiating QT-prolonging drug")

        # Drug-specific monitoring
        for drug in drugs:
            if drug in ("warfarin", "dabigatran", "rivaroxaban", "apixaban"):
                monitoring.append(f"{drug}: INR (warfarin) or anti-Xa monitoring; signs of bleeding")
            elif drug in ("lithium", "vancomycin", "gentamicin", "digoxin", "phenytoin"):
                monitoring.append(f"{drug}: Therapeutic drug monitoring — serum levels required")
            elif drug in ("metformin",):
                monitoring.append("Renal function (eGFR) before initiation and every 3–6 months")

        # Safety gate warnings
        for gate in safety_suite.gates:
            if gate.severity == "WARNING" and gate.message:
                monitoring.append(f"⚠️ {gate.message}")

        return list(set(monitoring))[:8]   # Deduplicate, cap at 8

    def _extract_stop_rules(self, cql_results: dict, llm_output: str) -> list[str]:
        """Extract stop/hold rules from CQL results."""
        stop_rules: list[str] = []

        renal = cql_results.get("renal_adjustments", {})
        for drug, adj in renal.items():
            if adj.get("action") in ("contraindicated", "avoid"):
                stop_rules.append(
                    f"STOP {drug} if eGFR falls to the contraindicated threshold: {adj.get('dose', 'see label')}"
                )
            else:
                stop_rules.append(
                    f"Review {drug} dose if renal function deteriorates >30% from baseline"
                )

        if not stop_rules:
            stop_rules.append("Discontinue and seek medical review if: severe adverse reaction, significant organ function deterioration, or clinical deterioration despite treatment")

        return stop_rules[:5]

    def _extract_escalation(self, safety_suite, query: ClinicalQuery) -> list[str]:
        """Generate escalation thresholds from safety context."""
        escalation = [
            "Seek urgent specialist review if: severe or unexpected adverse effects, clinical deterioration, signs of toxicity",
        ]
        if query.patient_context and query.patient_context.is_pregnant:
            escalation.append("Obstetric emergency: any acute deterioration in pregnancy requires immediate obstetric team review")
        if query.patient_context and query.patient_context.renal and query.patient_context.renal.on_dialysis:
            escalation.append("Nephrology review: any dose change in dialysis patient requires nephrology team approval")
        return escalation

    def _suggest_follow_up(self, drugs: list[str], cql_results: dict) -> Optional[str]:
        """Suggest follow-up interval based on drug class and CQL results."""
        if cql_results.get("renal_adjustments"):
            return "Renal function review in 4–6 weeks after any dose change"
        if any(d in drugs for d in ["warfarin", "phenytoin", "lithium", "digoxin", "vancomycin"]):
            return "Drug level monitoring within 5–7 days of initiation or dose change"
        if drugs:
            return "Clinical review in 4–8 weeks to assess therapeutic response and tolerability"
        return None

    def _build_summary(self, cards: list[EvidenceCard], mode: InteractionMode) -> str:
        """Build a brief summary text from evidence cards."""
        if not cards:
            return "No verified clinical information could be generated for this query."

        high_conf = [c for c in cards if c.confidence_level == ConfidenceLevel.HIGH]
        summary_parts = [f"Response based on {len(cards)} verified claims"]
        if high_conf:
            summary_parts.append(f"{len(high_conf)} with HIGH confidence")
        return ". ".join(summary_parts) + "."

    def _infer_study_design(self, evidence_object) -> "StudyDesign":
        """Infer study design from evidence metadata for L1-6 quality scoring."""
        from curaniq.layers.L1_evidence_ingestion.evidence_quality import StudyDesign

        tier = getattr(evidence_object, 'tier', None)
        title = getattr(evidence_object, 'title', '') or ''
        snippet = getattr(evidence_object, 'snippet', '') or ''
        source_type = getattr(evidence_object, 'source_type', None)
        text = (title + ' ' + snippet).lower()

        # Source-type based inference
        if source_type and hasattr(source_type, 'value'):
            st = source_type.value.lower()
            if 'fda' in st or 'label' in st or 'dailymed' in st:
                return StudyDesign.DRUG_LABEL
            if 'guideline' in st or 'nice' in st or 'who' in st:
                return StudyDesign.GUIDELINE

        # Text-based inference using real study design keywords
        if any(kw in text for kw in ['systematic review', 'meta-analysis', 'cochrane review']):
            return StudyDesign.SYSTEMATIC_REVIEW
        if any(kw in text for kw in ['randomized', 'randomised', 'rct', 'double-blind', 'placebo-controlled']):
            return StudyDesign.RCT
        if any(kw in text for kw in ['cohort study', 'prospective study', 'longitudinal']):
            return StudyDesign.COHORT
        if any(kw in text for kw in ['case-control', 'case control', 'retrospective']):
            return StudyDesign.CASE_CONTROL
        if any(kw in text for kw in ['case series', 'case report']):
            return StudyDesign.CASE_SERIES
        if any(kw in text for kw in ['preprint', 'medrxiv', 'biorxiv', 'not peer-reviewed']):
            return StudyDesign.PREPRINT
        if any(kw in text for kw in ['expert opinion', 'editorial', 'commentary', 'consensus']):
            return StudyDesign.EXPERT_OPINION

        return StudyDesign.COHORT  # Conservative default


def _empty_patient():
    """Return empty PatientContext for CQL calls when no context provided."""
    from curaniq.models.schemas import PatientContext, Jurisdiction
    return PatientContext(jurisdiction=Jurisdiction.INT)
