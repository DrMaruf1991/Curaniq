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
import re
import time
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
from curaniq.layers.L14_interaction.session_memory import ClinicalSessionMemory, AssumptionLedger, OutputExfiltrationScanner, resolve_drug_name, get_search_synonyms
from curaniq.safety.safety_gates import SafetyGateSuiteRunner


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
        self.adversarial_jury       = AdversarialLLMJury()
        self.l4_confidence_scorer   = L4ConfidenceScorer()

        # Load seed evidence for demo/dev
        if not evidence_store:
            self.retriever.load_seed_evidence()

    def process(self, query: ClinicalQuery) -> CURANIQResponse:
        """
        Execute the complete CURANIQ pipeline for a clinical query.
        Returns a fully verified, safety-gated CURANIQResponse.
        """
        start_time = time.perf_counter()

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
        # STAGE 6: L3-1 CQL Safety Kernel (deterministic rules)
        # Drugs and food/herbs already extracted by normalizer.
        # ═══════════════════════════════════════════════════════════════

        cql_results = self.cql_kernel.run_all_checks(
            patient=query.patient_context or _empty_patient(),
            drugs_mentioned=drugs_mentioned,
            food_herb_mentioned=food_herbs if food_herbs else None,
        )

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

        # Build monitoring / stop rules / escalation from CQL + safety gate context
        monitoring = self._extract_monitoring(cql_results, safety_suite_result, drugs_mentioned)
        stop_rules = self._extract_stop_rules(cql_results, llm_output)
        escalation = self._extract_escalation(safety_suite_result, query)

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

        return CURANIQResponse(
            query_id=query.query_id,
            mode=mode,
            user_role=query.user_role,
            triage=triage,
            safety_suite=safety_suite_result,
            claim_contract_enforced=claim_contract.enforcement_passed,
            evidence_cards=evidence_cards,
            summary_text=self._build_summary(evidence_cards, mode),
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


def _empty_patient():
    """Return empty PatientContext for CQL calls when no context provided."""
    from curaniq.models.schemas import PatientContext, Jurisdiction
    return PatientContext(jurisdiction=Jurisdiction.INT)
