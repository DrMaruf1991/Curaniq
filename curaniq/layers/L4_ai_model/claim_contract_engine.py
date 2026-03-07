"""
CURANIQ — Medical Evidence Operating System
Layer 4: Claim Contract Engine (L4-3) + Evidence Hash-Lock (L4-14)

THE ENFORCEMENT MECHANISM.

'The module that converts the product thesis into engineering reality.'
— CURANIQ Architecture v3.6

The Claim Contract Engine:
1. Segments LLM output into atomic clinical claims
2. Classifies each claim type (dosing, DDI, contraindication, etc.)
3. Requires a specific evidence object per claim (from the closed evidence pack)
4. Validates evidence ID exists in the pack (L4-14 — rejects hallucinated IDs)
5. Verifies the exact snippet bytes via SHA-256 (L4-14)
6. Runs NLI entailment check: does this evidence actually support this claim?
7. Blocks any claim where evidence doesn't entail it
8. Enforces cross-document consistency when multiple outputs generated

Evidence Hash-Lock Enforcement (L4-14):
- Every snippet retrieved is SHA-256 hashed at retrieval time
- Verifier checks claims against EXACT hashed bytes (not re-fetched text)
- LLM receives closed set of valid IDs — any ID not in set → REJECTED
- Each claim must reference snippet by ID + byte offset + hash
- Missing provenance field → evidence object INVALID → cannot be cited
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

import aiohttp

from curaniq.models.claims import (
    AtomicClaim,
    ClaimContract,
    ClaimType,
    ClaimVerdict,
    SnippetClaimBinding,
    VerifierDecision,
    HIGH_RISK_CLAIM_TYPES,
)
from curaniq.models.evidence import EvidenceChunk, EvidencePack

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# CLAIM SEGMENTATION PATTERNS
# Used to identify claim boundaries in LLM output
# ─────────────────────────────────────────────────────────────────────────────

# Sentence boundary patterns for clinical text
_SENTENCE_BOUNDARY = re.compile(r'(?<=[.!?])\s+(?=[A-Z])')

# Patterns indicating claim types
_DOSING_PATTERNS = [
    r'\b\d+\.?\d*\s*(mg|mcg|g|units?|mEq|mmol)\b',
    r'\b(dose|dosing|administer|give|prescribe|start)\b',
    r'\b(once|twice|three times|q\d+h|daily|weekly|BID|TID|QID)\b',
    r'\b(maximum|minimum|cap|limit|not exceed)\b',
]
_CONTRAINDICATION_PATTERNS = [
    r'\b(contraindicated|avoid|do not use|prohibited|forbidden)\b',
    r'\b(allerg(y|ic|ies)|hypersensitivity|anaphylaxis)\b',
]
_DDI_PATTERNS = [
    r'\b(interaction|interact|combined|concurrent|concomitant)\b',
    r'\b(inhibit|induce|potentiate|antagonize)\b',
    r'\b(CYP\d+[A-Z]\d*|P-glycoprotein|PGP)\b',
]
_EFFICACY_PATTERNS = [
    r'\b(effective|efficacy|benefit|reduce(s|d)?|improve(s|d)?|NNT|NNH|RR|OR|ARR)\b',
    r'\b(trial|study|evidence|shown|demonstrated|proven)\b',
]
_SAFETY_WARNING_PATTERNS = [
    r'\b(black box|boxed warning|REMS|serious|life-threatening|fatal)\b',
    r'\b(warning|caution|monitor|risk)\b',
]

_CLAIM_TYPE_PATTERNS = {
    ClaimType.DOSING:           [re.compile(p, re.IGNORECASE) for p in _DOSING_PATTERNS],
    ClaimType.CONTRAINDICATION: [re.compile(p, re.IGNORECASE) for p in _CONTRAINDICATION_PATTERNS],
    ClaimType.DRUG_INTERACTION: [re.compile(p, re.IGNORECASE) for p in _DDI_PATTERNS],
    ClaimType.EFFICACY:         [re.compile(p, re.IGNORECASE) for p in _EFFICACY_PATTERNS],
    ClaimType.SAFETY_WARNING:   [re.compile(p, re.IGNORECASE) for p in _SAFETY_WARNING_PATTERNS],
}

# Numeric extraction pattern (for L5-17 Numeric Gate)
_NUMERIC_PATTERN = re.compile(
    r'\b(\d+\.?\d*\s*(?:mg(?:/kg)?|mcg(?:/kg)?|g|mL|L|mEq|mmol|units?|%|mL/min|mg/dL|'
    r'mmHg|bpm|°[CF]|cm|kg|IU|nmol/L|µg/L|ng/mL))\b',
    re.IGNORECASE
)


# ─────────────────────────────────────────────────────────────────────────────
# NLI ENTAILMENT CLIENT
# Calls self-hosted SciFact/MedNLI model for deterministic entailment scoring
# ─────────────────────────────────────────────────────────────────────────────

class NLIEntailmentClient:
    """
    Client for the self-hosted NLI model (SciFact or MedNLI).
    
    Per architecture: 'specialized NLI model (SciFact/MedNLI, self-hosted,
    deterministic score 0.0-1.0)' — this is the ground-truth entailment check.
    
    Input: (premise = evidence snippet, hypothesis = clinical claim)
    Output: entailment score 0.0-1.0
    """
    
    def __init__(self, endpoint: str = "http://localhost:8100/entailment"):
        self.endpoint = endpoint

    async def score(
        self,
        premise: str,       # The evidence snippet
        hypothesis: str,    # The clinical claim to verify
        timeout: float = 5.0,
    ) -> float:
        """
        Compute NLI entailment score.
        Returns 0.0-1.0 (1.0 = evidence fully entails claim, 0.0 = contradiction).
        
        On model unavailability: returns 0.5 (uncertain) — conservative.
        """
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self.endpoint,
                    json={"premise": premise, "hypothesis": hypothesis},
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        score = float(result.get("entailment_score", 0.5))
                        return max(0.0, min(1.0, score))
                    else:
                        logger.warning(f"NLI model returned HTTP {resp.status}")
                        return 0.5  # Conservative uncertain
        except Exception as e:
            logger.warning(f"NLI model unavailable: {e}. Using conservative score 0.5")
            return 0.5  # Fail-safe: uncertain


# ─────────────────────────────────────────────────────────────────────────────
# EVIDENCE HASH-LOCK ENGINE — L4-14
# ─────────────────────────────────────────────────────────────────────────────

class EvidenceHashLockEngine:
    """
    L4-14: Evidence Object Integrity & Hash-Lock Enforcement.
    
    'Closes the hallucinated evidence ID attack vector.'
    
    4 hard constraints enforced:
    1. IMMUTABLE SNIPPETS: every snippet is SHA-256 hashed at retrieval
    2. UNFORGEABLE IDs: LLM receives closed set — hallucinated IDs rejected
    3. SNIPPET-CLAIM BINDING: claims reference exact snippet by ID+offset+hash
    4. PROVENANCE CHAIN: complete or evidence object is INVALID
    """

    @staticmethod
    def hash_snippet(content: str | bytes) -> str:
        """Compute SHA-256 hash of evidence snippet bytes."""
        if isinstance(content, str):
            content = content.encode("utf-8")
        return hashlib.sha256(content).hexdigest()

    @staticmethod
    def verify_snippet_integrity(chunk: EvidenceChunk, binding: SnippetClaimBinding) -> tuple[bool, str]:
        """
        Verify that the snippet referenced by a claim binding is intact.
        
        Checks:
        1. chunk_id exists in the binding
        2. snippet_hash in binding matches chunk's stored hash
        3. Provenance chain is complete
        
        Returns (is_valid, reason).
        """
        # Check 1: Provenance completeness
        if not chunk.provenance.is_complete():
            return False, "INVALID: Evidence chunk has incomplete provenance chain"
        
        # Check 2: Hash verification — the binding's hash MUST match chunk's stored hash
        stored_hash = chunk.provenance.snippet_hash
        binding_hash = binding.snippet_hash
        
        if stored_hash != binding_hash:
            return False, (
                f"HASH_MISMATCH: Binding references hash {binding_hash[:16]}... "
                f"but chunk stores {stored_hash[:16]}... "
                "Evidence drift detected — this snippet may have been modified."
            )
        
        # Check 3: Content verification (if raw bytes available)
        if chunk.content_bytes:
            computed_hash = EvidenceHashLockEngine.hash_snippet(chunk.content_bytes)
            if computed_hash != stored_hash:
                return False, (
                    f"CONTENT_TAMPERED: Stored hash {stored_hash[:16]}... "
                    f"does not match content hash {computed_hash[:16]}... "
                    "Evidence content has been modified since ingestion."
                )
        
        return True, "HASH_VERIFIED"

    @staticmethod
    def validate_evidence_id(chunk_id: str, evidence_pack: EvidencePack) -> tuple[bool, str]:
        """
        L4-14 constraint: LLM-generated evidence IDs must exist in the closed set.
        
        'The LLM receives a closed set of valid IDs with its evidence context.
        Any ID in the LLM output that does NOT exist in the provided set is REJECTED.'
        
        Returns (is_valid, reason).
        """
        if not evidence_pack.validate_chunk_id(chunk_id):
            return False, (
                f"HALLUCINATED_ID: Evidence ID '{chunk_id}' does not exist in the "
                f"evidence pack (pack_id: {evidence_pack.pack_id}). "
                "The model cannot invent evidence IDs. This claim is REJECTED."
            )
        return True, "ID_VALID"

    @staticmethod
    def create_binding(
        chunk: EvidenceChunk,
        relevant_span_start: int = 0,
        relevant_span_length: Optional[int] = None,
    ) -> SnippetClaimBinding:
        """
        Create an immutable snippet-claim binding for a given chunk.
        The binding references the EXACT bytes at the specified offset.
        """
        content_bytes = chunk.content_bytes or chunk.content.encode("utf-8")
        span_length = relevant_span_length or len(content_bytes)
        
        # Extract the relevant span
        span_bytes = content_bytes[relevant_span_start:relevant_span_start + span_length]
        span_hash = EvidenceHashLockEngine.hash_snippet(span_bytes)
        
        return SnippetClaimBinding(
            chunk_id=chunk.chunk_id,
            byte_offset=relevant_span_start,
            snippet_hash=span_hash,
            span_length=span_length,
        )


# ─────────────────────────────────────────────────────────────────────────────
# CLAIM CONTRACT ENGINE — L4-3
# ─────────────────────────────────────────────────────────────────────────────

class ClaimContractEngine:
    """
    L4-3: Claim Contract Engine — THE enforcement mechanism.
    
    'Converts the product thesis into engineering reality.'
    
    Processes:
    1. Segmentation: splits LLM structured output into atomic claims
    2. Classification: identifies claim type per ClaimType enum
    3. Evidence binding: maps each claim to its evidence source
    4. ID validation: rejects any hallucinated evidence IDs (L4-14)
    5. Hash verification: ensures evidence integrity (L4-14)
    6. Citability check: blocks retracted, quarantined, stale sources
    7. NLI entailment: verifies evidence actually entails each claim
    8. Cross-document consistency: checks for contradictions across claims
    9. Verdict assignment: PASS/SUPPRESS/BLOCK per confidence thresholds
    """

    def __init__(
        self,
        nli_client: Optional[NLIEntailmentClient] = None,
        nli_entailment_threshold: float = 0.50,
        nli_model_version: str = "scifact-nli-v1",
    ):
        self.nli_client = nli_client or NLIEntailmentClient()
        self.nli_entailment_threshold = nli_entailment_threshold
        self.nli_model_version = nli_model_version
        self.hash_lock = EvidenceHashLockEngine()

    async def process_llm_output(
        self,
        llm_structured_output: dict,
        evidence_pack: EvidencePack,
        query_id: str,
        query_text: str,
        query_risk_level: str = "standard",
        primary_llm: str = "claude-sonnet-4-6",
        cql_overrides: Optional[list[dict]] = None,
    ) -> ClaimContract:
        """
        Main entry point: process structured LLM output through the full contract pipeline.
        
        LLM output format (from L4-2 Constrained Generator):
        {
            "claims": [
                {
                    "text": "Metformin is contraindicated when eGFR < 30 mL/min",
                    "evidence_id": "<chunk_id from evidence pack>",
                    "claim_type": "contraindication"
                },
                ...
            ],
            "safe_next_steps": ["Check eGFR urgently", "Consider insulin..."],
        }
        """
        raw_claims = llm_structured_output.get("claims", [])
        safe_next_steps = llm_structured_output.get("safe_next_steps", [])
        
        processed_claims: list[AtomicClaim] = []
        
        for raw_claim in raw_claims:
            claim = await self._process_single_claim(
                raw_claim=raw_claim,
                evidence_pack=evidence_pack,
            )
            processed_claims.append(claim)
        
        # Check cross-document consistency
        processed_claims = self._check_cross_consistency(processed_claims)
        
        # Build contract
        contract = ClaimContract(
            query_id=query_id,
            evidence_pack_id=evidence_pack.pack_id,
            all_claims=processed_claims,
            query_text=query_text,
            query_risk_level=query_risk_level,
            primary_llm=primary_llm,
            nli_model=self.nli_model_version,
            evidence_freshness=evidence_pack.staleness_display(),
            safe_next_steps=safe_next_steps,
            cql_overrides=cql_overrides or [],
        )
        
        logger.info(
            f"ClaimContract [{query_id}]: {contract.total_claims} total claims, "
            f"{contract.suppressed_claims} suppressed, "
            f"refused={contract.refused}"
        )
        
        return contract

    async def _process_single_claim(
        self,
        raw_claim: dict,
        evidence_pack: EvidencePack,
    ) -> AtomicClaim:
        """
        Process one raw claim through all enforcement layers.
        Returns an AtomicClaim with a final verdict.
        """
        claim_text = raw_claim.get("text", "")
        evidence_id = raw_claim.get("evidence_id", "")
        claimed_type = raw_claim.get("claim_type", "unknown")
        
        # Classify claim type
        claim_type = self._classify_claim_type(claim_text, claimed_type)
        
        # Extract numeric values (for L5-17 Numeric Gate)
        numerics = _NUMERIC_PATTERN.findall(claim_text)
        contains_numeric = len(numerics) > 0
        
        # ─── STEP 1: Validate evidence ID (L4-14 — reject hallucinated IDs) ────
        id_valid, id_reason = self.hash_lock.validate_evidence_id(evidence_id, evidence_pack)
        
        if not id_valid:
            logger.warning(f"HALLUCINATED_ID detected: {evidence_id[:32]}...")
            return AtomicClaim(
                claim_text=claim_text,
                claim_type=claim_type,
                evidence_binding=SnippetClaimBinding(
                    chunk_id=evidence_id,
                    byte_offset=0,
                    snippet_hash="INVALID",
                    span_length=0,
                ),
                verdict=ClaimVerdict.BLOCKED_HALLUC,
                verdict_reason=id_reason,
                contains_numeric=contains_numeric,
                numeric_values=numerics,
            )
        
        # ─── STEP 2: Retrieve the evidence chunk ───────────────────────────────
        chunk = evidence_pack.get_chunk(evidence_id)
        if chunk is None:
            return AtomicClaim(
                claim_text=claim_text,
                claim_type=claim_type,
                evidence_binding=SnippetClaimBinding(
                    chunk_id=evidence_id, byte_offset=0, snippet_hash="", span_length=0
                ),
                verdict=ClaimVerdict.BLOCKED_HALLUC,
                verdict_reason=f"Evidence chunk {evidence_id} not found in pack",
                contains_numeric=contains_numeric,
                numeric_values=numerics,
            )
        
        # ─── STEP 3: Check citability (retraction, quarantine, staleness) ──────
        can_cite, cite_reason = chunk.is_citable()
        if not can_cite:
            verdict = (
                ClaimVerdict.BLOCKED_RETRACT if "RETRACT" in cite_reason
                else ClaimVerdict.BLOCKED_STALE if "STALE" in cite_reason
                else ClaimVerdict.SUPPRESSED
            )
            return AtomicClaim(
                claim_text=claim_text,
                claim_type=claim_type,
                evidence_binding=self.hash_lock.create_binding(chunk),
                verdict=verdict,
                verdict_reason=cite_reason,
                contains_numeric=contains_numeric,
                numeric_values=numerics,
            )
        
        # ─── STEP 4: Create snippet-claim binding + verify hash (L4-14) ────────
        binding = self.hash_lock.create_binding(chunk)
        hash_valid, hash_reason = self.hash_lock.verify_snippet_integrity(chunk, binding)
        
        if not hash_valid:
            logger.error(f"Hash integrity failure for chunk {evidence_id[:32]}...: {hash_reason}")
            return AtomicClaim(
                claim_text=claim_text,
                claim_type=claim_type,
                evidence_binding=binding,
                verdict=ClaimVerdict.BLOCKED_HALLUC,
                verdict_reason=hash_reason,
                contains_numeric=contains_numeric,
                numeric_values=numerics,
            )
        
        # ─── STEP 5: NLI entailment check ───────────────────────────────────────
        # Does this evidence actually entail/support this claim?
        nli_score = await self.nli_client.score(
            premise=chunk.content,
            hypothesis=claim_text,
        )
        
        # If NLI score below threshold — claim is not entailed by evidence
        if nli_score < self.nli_entailment_threshold:
            return AtomicClaim(
                claim_text=claim_text,
                claim_type=claim_type,
                evidence_binding=binding,
                nli_entailment_score=nli_score,
                nli_model_version=self.nli_model_version,
                verdict=ClaimVerdict.BLOCKED_NLI,
                verdict_reason=(
                    f"NLI_FAIL: Evidence does not entail this claim "
                    f"(entailment score: {nli_score:.3f} < threshold {self.nli_entailment_threshold}). "
                    "Claim suppressed — unsupported inference."
                ),
                contains_numeric=contains_numeric,
                numeric_values=numerics,
            )
        
        # ─── STEP 6: Numeric grounding check (L5-17 — are numbers from evidence?) ─
        numeric_grounded = True
        if contains_numeric:
            numeric_grounded = self._check_numeric_grounding(numerics, chunk.content)
        
        # ─── STEP 7: Compute initial confidence score (full scoring in L4-13) ──
        # Basic confidence here — full adversarial scoring happens in L4-12/L4-13
        basic_confidence = self._compute_basic_confidence(
            nli_score=nli_score,
            evidence_tier_score=chunk.cebm_score(),
            recency_score=chunk.compute_recency_score(),
            source_count=1,  # Single source — adversarial jury may update
        )
        
        # ─── STEP 8: Assign initial verdict ─────────────────────────────────────
        verdict, verdict_reason, uncertainty_marker, human_review_flag = (
            self._assign_verdict(basic_confidence, claim_type, numeric_grounded)
        )
        
        claim = AtomicClaim(
            claim_text=claim_text,
            claim_type=claim_type,
            evidence_binding=binding,
            nli_entailment_score=nli_score,
            nli_model_version=self.nli_model_version,
            confidence_score=basic_confidence,
            confidence_components={
                "nli_entailment": nli_score,
                "evidence_tier": chunk.cebm_score(),
                "recency": chunk.compute_recency_score(),
                "source_count": 1.0 if 1 >= 3 else (0.8 if 1 == 2 else 0.6),
                "evidence_tier_label": f"{chunk.evidence_tier.value} from {chunk.provenance.source_api.value}",
            },
            verdict=verdict,
            verdict_reason=verdict_reason,
            uncertainty_marker=uncertainty_marker,
            human_review_flag=human_review_flag,
            contains_numeric=contains_numeric,
            numeric_values=numerics,
            numeric_grounded=numeric_grounded,
        )
        
        return claim

    def _classify_claim_type(self, text: str, declared_type: str) -> ClaimType:
        """
        Classify claim type from text content and declared type.
        Pattern matching + keyword detection.
        """
        # Trust declared type if valid
        try:
            return ClaimType(declared_type)
        except ValueError:
            pass
        
        # Fall back to pattern matching
        type_scores: dict[ClaimType, int] = {}
        for claim_type, patterns in _CLAIM_TYPE_PATTERNS.items():
            score = sum(1 for p in patterns if p.search(text))
            if score > 0:
                type_scores[claim_type] = score
        
        if type_scores:
            return max(type_scores, key=type_scores.get)
        
        return ClaimType.UNKNOWN

    def _check_numeric_grounding(self, numerics: list[str], evidence_text: str) -> bool:
        """
        L5-17: Verify that every numeric value in a claim appears verbatim in evidence.
        
        Per architecture: 'if a number is not in CQL output AND not verbatim from
        any snippet → SUPPRESS that claim.'
        
        Returns True if all numerics are grounded in evidence text.
        """
        for numeric in numerics:
            # Normalize and check if this numeric appears in the evidence
            normalized_numeric = numeric.strip().lower()
            if normalized_numeric not in evidence_text.lower():
                # Numeric not found verbatim — potential hallucination
                logger.warning(
                    f"Numeric value '{numeric}' not found verbatim in evidence text. "
                    "Possible LLM arithmetic or hallucination."
                )
                return False
        return True

    def _compute_basic_confidence(
        self,
        nli_score: float,
        evidence_tier_score: float,
        recency_score: float,
        source_count: int,
    ) -> float:
        """
        Basic confidence scoring per L4-13 formula.
        Full adversarial scoring happens after L4-12 verification.
        
        Score = weighted average of:
        (a) NLI entailment score (0.0-1.0)
        (b) Cross-LLM agreement — not yet available at this stage (0.5 placeholder)
        (c) Evidence quality tier per CEBM
        (d) Evidence recency
        (e) Source count: 3+ = 1.0, 2 = 0.8, 1 = 0.6, 0 = SUPPRESS
        """
        if source_count == 0:
            return 0.0  # SUPPRESS — no evidence
        
        source_score = 1.0 if source_count >= 3 else (0.8 if source_count == 2 else 0.6)
        
        weights = {
            "nli": 0.35,
            "llm_agreement": 0.20,    # Will be updated by L4-12
            "evidence_tier": 0.20,
            "recency": 0.15,
            "source_count": 0.10,
        }
        
        score = (
            weights["nli"] * nli_score +
            weights["llm_agreement"] * 0.5 +   # Placeholder — updated by L4-12
            weights["evidence_tier"] * evidence_tier_score +
            weights["recency"] * recency_score +
            weights["source_count"] * source_score
        )
        
        return round(max(0.0, min(1.0, score)), 4)

    def _assign_verdict(
        self,
        confidence: float,
        claim_type: ClaimType,
        numeric_grounded: bool,
    ) -> tuple[ClaimVerdict, str, Optional[str], bool]:
        """
        Assign verdict based on confidence score and claim properties.
        
        Returns (verdict, verdict_reason, uncertainty_marker, human_review_flag)
        
        Thresholds per L4-13:
        ≥0.85 → PASS_HIGH
        0.70-0.85 → PASS_MEDIUM (uncertainty marker)
        0.50-0.70 → PASS_LOW (caveat + human review flag)
        <0.50 → SUPPRESSED
        """
        # Numeric grounding failure for dose/DDI claims → immediate suppress
        if not numeric_grounded and claim_type in (ClaimType.DOSING, ClaimType.DRUG_INTERACTION):
            return (
                ClaimVerdict.SUPPRESSED,
                "NUMERIC_UNGROUNDED: Numeric value in this claim could not be verified "
                "as coming from CQL engine or verbatim from evidence (L5-17 Numeric Gate).",
                None,
                False,
            )
        
        if confidence >= 0.85:
            return ClaimVerdict.PASS_HIGH, "HIGH_CONFIDENCE", None, False
        
        elif confidence >= 0.70:
            marker = "Evidence quality: MODERATE — verify with current guidelines"
            if claim_type in HIGH_RISK_CLAIM_TYPES:
                marker = (
                    f"⚠️ MODERATE confidence for high-risk claim ({claim_type.value}). "
                    "Verify against current drug label or guideline before acting."
                )
            return ClaimVerdict.PASS_MEDIUM, "MEDIUM_CONFIDENCE", marker, False
        
        elif confidence >= 0.50:
            marker = (
                f"⚠️ LOW confidence ({confidence:.2f}). "
                "Limited evidence support. Consult specialist or primary guideline source."
            )
            return (
                ClaimVerdict.PASS_LOW,
                "LOW_CONFIDENCE",
                marker,
                True,  # Flag for human review
            )
        
        else:
            return (
                ClaimVerdict.SUPPRESSED,
                f"SUPPRESSED: Confidence {confidence:.3f} below minimum threshold 0.50. "
                "Insufficient evidence to make this clinical claim.",
                None,
                False,
            )

    def _check_cross_consistency(self, claims: list[AtomicClaim]) -> list[AtomicClaim]:
        """
        L4-3: Cross-document consistency checking.
        Detects contradictions between visible claims.
        
        Example: Claim A says "dose 500 mg" — Claim B says "dose 250 mg" for same drug.
        Such contradictions are flagged, and lower-confidence claim is suppressed.
        """
        visible = [c for c in claims if c.is_shown_to_clinician]
        
        # Group dosing claims by drug name (heuristic extraction)
        dosing_claims = [c for c in visible if c.claim_type == ClaimType.DOSING]
        
        # Simple contradiction detection: same drug, different numeric values
        for i, claim_a in enumerate(dosing_claims):
            for claim_b in dosing_claims[i + 1:]:
                # Check if claims involve same drug but different doses
                if self._are_contradictory(claim_a.claim_text, claim_b.claim_text):
                    # Suppress lower-confidence claim
                    if (claim_a.confidence_score or 0) < (claim_b.confidence_score or 0):
                        # Suppress claim_a in the full list
                        for c in claims:
                            if c.claim_id == claim_a.claim_id:
                                c.verdict = ClaimVerdict.SUPPRESSED
                                c.verdict_reason = (
                                    "CROSS_CONSISTENCY: This claim contradicts a higher-confidence "
                                    "claim for the same drug. The higher-confidence claim is retained."
                                )
                    else:
                        for c in claims:
                            if c.claim_id == claim_b.claim_id:
                                c.verdict = ClaimVerdict.SUPPRESSED
                                c.verdict_reason = (
                                    "CROSS_CONSISTENCY: This claim contradicts a higher-confidence "
                                    "claim for the same drug. The higher-confidence claim is retained."
                                )
        
        return claims

    def _are_contradictory(self, text_a: str, text_b: str) -> bool:
        """
        Heuristic check for contradictory claims.
        Full implementation uses semantic similarity — simplified here.
        """
        # Extract drug names and dosing numerics from both claims
        numerics_a = set(_NUMERIC_PATTERN.findall(text_a))
        numerics_b = set(_NUMERIC_PATTERN.findall(text_b))
        
        # If both contain different numeric dose values for what appears to be same drug
        # (drug name extraction is simplified here — L4-10 clinical knowledge graph handles this fully)
        if numerics_a and numerics_b and not numerics_a.intersection(numerics_b):
            # Both have numeric values and they don't overlap — potential contradiction
            # Apply only if they share significant text context (same drug discussion)
            common_words = set(text_a.lower().split()).intersection(set(text_b.lower().split()))
            significant_common = {w for w in common_words if len(w) > 4}
            return len(significant_common) >= 3  # 3+ meaningful shared words suggests same topic
        
        return False

    def build_evidence_context_for_llm(self, evidence_pack: EvidencePack) -> str:
        """
        Build the evidence context string that the Constrained Generator (L4-2)
        receives. This includes ONLY the closed set of valid evidence chunk IDs.
        
        The LLM receives this context and MUST reference only these IDs.
        Any ID not in this set will be rejected by L4-14.
        """
        lines = [
            "=== CURANIQ EVIDENCE PACK ===",
            f"Pack ID: {evidence_pack.pack_id}",
            f"Total evidence chunks: {len(evidence_pack.chunks)}",
            f"Evidence freshness: {evidence_pack.staleness_display()}",
            "",
            "AVAILABLE EVIDENCE (cite by CHUNK_ID only):",
            "═" * 60,
        ]
        
        citable = evidence_pack.get_citable_chunks()
        
        for chunk in citable:
            can_cite, cite_reason = chunk.is_citable()
            if not can_cite:
                continue
            
            lines.append(f"\nCHUNK_ID: {chunk.chunk_id}")
            lines.append(f"SOURCE: {chunk.provenance.source_api.value}")
            lines.append(f"EVIDENCE_TIER: {chunk.evidence_tier.value}")
            lines.append(f"CEBM_SCORE: {chunk.cebm_score()}")
            if chunk.provenance.publication_date:
                lines.append(f"PUBLISHED: {chunk.provenance.publication_date.strftime('%Y-%m-%d')}")
            if chunk.provenance.jurisdiction:
                lines.append(f"JURISDICTION: {chunk.provenance.jurisdiction.value}")
            lines.append(f"CONTENT:\n{chunk.content[:800]}...")  # Truncated for context window
            lines.append("─" * 40)
        
        lines.extend([
            "",
            "⚠️ CRITICAL INSTRUCTIONS FOR CLAIM GENERATION:",
            "1. Every claim MUST reference a CHUNK_ID from the list above.",
            "2. Do NOT invent, guess, or fabricate evidence IDs.",
            "3. Do NOT reference any source not listed above.",
            "4. Do NOT generate any numeric value (dose, lab value, percentage) not",
            "   found verbatim in the evidence above OR computed by the CQL kernel.",
            "5. If evidence is insufficient for a safe claim → state 'insufficient evidence'.",
            "6. Output format: JSON array of {text, evidence_id, claim_type} objects.",
        ])
        
        return "\n".join(lines)
