"""
CURANIQ — Medical Evidence Operating System
Layer 4: AI Model Layer

L4-1  Hybrid Retriever (BM25 + vector + metadata filters)
L4-11 Cross-Encoder Reranker
L4-13 Confidence Scoring Engine (8-component weighted formula)
L4-14 Evidence Hash-Lock Enforcement
"""
from __future__ import annotations
import hashlib, logging, math, re, uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from curaniq.models.evidence import (
    EvidenceChunk, EvidenceTier, CEBM_SCORE,
    RetractionStatus, StalenessStatus,
)
logger = logging.getLogger(__name__)

CURRENT_EMBEDDING_MODEL_ID = "text-embedding-3-large"

# ─────────────────────────────────────────────────────────────────────────────
# L4-1: HYBRID RETRIEVER
# Architecture: 'Multi-stage: keyword (BM25) + semantic (vector) + metadata.
# Combats vocabulary mismatch. High-priority section boost.'
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RetrievalResult:
    chunk: EvidenceChunk
    bm25_score: float = 0.0
    vector_score: float = 0.0
    metadata_boost: float = 0.0
    rerank_score: float = 0.0
    final_score: float = 0.0
    retrieval_reason: str = ""


def _tokenize(text: str) -> list[str]:
    """Simple clinical tokenizer — handles drug names, lab values, abbreviations."""
    text = text.lower()
    # Preserve clinical abbreviations: eGFR, HbA1c, ACEi, etc.
    tokens = re.findall(r'\b[a-z0-9][a-z0-9/\-]{1,30}\b', text)
    return tokens


def _idf(term: str, corpus: list[list[str]]) -> float:
    """Inverse document frequency."""
    n = len(corpus)
    df = sum(1 for doc in corpus if term in doc)
    if df == 0:
        return 0.0
    return math.log((n - df + 0.5) / (df + 0.5) + 1)


def bm25_score(
    query_tokens: list[str],
    doc_tokens: list[str],
    corpus_tokens: list[list[str]],
    k1: float = 1.5,
    b: float = 0.75,
) -> float:
    """BM25 scoring for a single document."""
    avg_len = sum(len(d) for d in corpus_tokens) / max(len(corpus_tokens), 1)
    doc_len = len(doc_tokens)
    score = 0.0
    doc_tf: dict[str, int] = {}
    for t in doc_tokens:
        doc_tf[t] = doc_tf.get(t, 0) + 1
    for term in set(query_tokens):
        tf = doc_tf.get(term, 0)
        if tf == 0:
            continue
        idf = _idf(term, corpus_tokens)
        numerator = tf * (k1 + 1)
        denominator = tf + k1 * (1 - b + b * doc_len / avg_len)
        score += idf * (numerator / denominator)
    return score


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two embedding vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(y * y for y in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def _metadata_boost(chunk: EvidenceChunk, query: str, jurisdiction: Optional[str] = None) -> float:
    """
    Metadata-based relevance boost.
    High-priority clinical sections get boosted.
    Jurisdiction-matching chunks get boosted.
    Fresh evidence gets boosted over stale.
    """
    boost = 0.0
    content_lower = chunk.content.lower()

    # High-priority clinical section boost
    high_priority_sections = {
        "boxed warning", "black box", "contraindication",
        "dosage and administration", "warnings and precautions",
        "drug interactions", "pregnancy", "renal impairment",
    }
    if any(sec in content_lower for sec in high_priority_sections):
        boost += 0.3

    # Evidence tier boost
    tier_boost = {
        EvidenceTier.SYSTEMATIC_REVIEW: 0.25,
        EvidenceTier.GUIDELINE: 0.2,
        EvidenceTier.RCT: 0.15,
        EvidenceTier.COHORT: 0.05,
    }
    boost += tier_boost.get(chunk.evidence_tier, 0.0)

    # Freshness boost
    if chunk.staleness_status == StalenessStatus.FRESH:
        boost += 0.1
    elif chunk.staleness_status == StalenessStatus.STALE:
        boost -= 0.1
    elif chunk.staleness_status == StalenessStatus.CRITICAL:
        boost -= 0.5  # Heavy penalty for critical staleness

    # Retraction penalty
    if chunk.retraction_status == RetractionStatus.CORRECTED:
        boost -= 0.2
    if chunk.retraction_status == RetractionStatus.EXPRESSION:
        boost -= 0.3

    # Jurisdiction match boost
    if jurisdiction and chunk.provenance.jurisdiction.value == jurisdiction.lower():
        boost += 0.15

    # PICO completeness boost (evidence with extracted PICO is more structured)
    if chunk.pico_outcome and chunk.pico_intervention:
        boost += 0.1

    return boost


class HybridRetriever:
    """
    L4-1: Hybrid multi-stage retriever.
    
    Stage 1: BM25 keyword retrieval — handles exact drug names, ICD codes,
             dose values where semantic search fails (vocabulary mismatch).
    Stage 2: Vector similarity — semantic meaning across paraphrases.
    Stage 3: Metadata filtering — jurisdiction, evidence tier, freshness.
    Stage 4: Score fusion — RRF (Reciprocal Rank Fusion) combining all signals.
    
    Production: Stage 2 uses pgvector with IVFFlat index (OpenAI embeddings).
    Current: Stage 1+3 functional; Stage 2 returns 0 without embeddings.
    """

    def __init__(self, chunks: Optional[list[EvidenceChunk]] = None) -> None:
        self._chunks: list[EvidenceChunk] = chunks or []
        self._corpus_tokens: list[list[str]] = []
        self._rebuild_index()

    def add_chunks(self, chunks: list[EvidenceChunk]) -> None:
        self._chunks.extend(chunks)
        self._rebuild_index()

    def _rebuild_index(self) -> None:
        self._corpus_tokens = [_tokenize(c.content) for c in self._chunks]

    def retrieve(
        self,
        query: str,
        query_embedding: Optional[list[float]] = None,
        top_k: int = 20,
        jurisdiction: Optional[str] = None,
        min_evidence_tier: Optional[EvidenceTier] = None,
        exclude_stale: bool = False,
    ) -> list[RetrievalResult]:
        """
        Multi-stage hybrid retrieval. Returns top_k ranked results.
        
        Fails closed: if a chunk fails L4-14 hash-lock verification, it is
        excluded from results regardless of relevance score.
        """
        if not self._chunks:
            return []

        query_tokens = _tokenize(query)
        results: list[RetrievalResult] = []

        for i, chunk in enumerate(self._chunks):
            # L4-14 citability pre-check — fail closed
            can_cite, reason = chunk.is_citable()
            if not can_cite:
                logger.debug(f"Chunk {chunk.chunk_id} excluded from retrieval: {reason}")
                continue

            # Metadata filters
            if min_evidence_tier:
                tier_order = [
                    EvidenceTier.UNKNOWN, EvidenceTier.EXPERT_OPINION,
                    EvidenceTier.CASE_REPORT, EvidenceTier.COHORT,
                    EvidenceTier.GUIDELINE, EvidenceTier.RCT,
                    EvidenceTier.SYSTEMATIC_REVIEW,
                ]
                if tier_order.index(chunk.evidence_tier) < tier_order.index(min_evidence_tier):
                    continue

            if exclude_stale and chunk.staleness_status == StalenessStatus.CRITICAL:
                continue

            # BM25 score
            doc_tokens = self._corpus_tokens[i] if i < len(self._corpus_tokens) else []
            bm25 = bm25_score(query_tokens, doc_tokens, self._corpus_tokens)

            # Vector similarity (if embedding provided)
            vec_score = 0.0
            if query_embedding and chunk.embedding_model_id:
                # Production: retrieve chunk embedding from pgvector
                # Current: embedding not stored in chunk object — placeholder 0.0
                vec_score = 0.0

            # Metadata boost
            meta = _metadata_boost(chunk, query, jurisdiction)

            # RRF fusion (Reciprocal Rank Fusion weight: bm25*0.4 + vec*0.4 + meta*0.2)
            final = bm25 * 0.4 + vec_score * 0.4 + meta * 0.2

            results.append(RetrievalResult(
                chunk=chunk,
                bm25_score=round(bm25, 4),
                vector_score=round(vec_score, 4),
                metadata_boost=round(meta, 4),
                final_score=round(final, 4),
                retrieval_reason=f"BM25={bm25:.3f} vec={vec_score:.3f} meta={meta:.3f}",
            ))

        # Sort by final score
        results.sort(key=lambda r: r.final_score, reverse=True)
        return results[:top_k]

    def get_chunks_for_pack(
        self,
        query: str,
        top_k: int = 20,
        jurisdiction: Optional[str] = None,
    ) -> list[EvidenceChunk]:
        """Convenience method — returns ranked EvidenceChunks for evidence pack assembly."""
        results = self.retrieve(query, top_k=top_k, jurisdiction=jurisdiction)
        return [r.chunk for r in results]


# ─────────────────────────────────────────────────────────────────────────────
# L4-11: CROSS-ENCODER RERANKER
# Architecture: 'Dedicated cross-encoder between L4-1 and L4-2.
# Combats the bi-encoder curse of dimensionality. Clinical relevance scoring.'
# ─────────────────────────────────────────────────────────────────────────────

# Clinical relevance signal patterns — used by rule-based reranker
_DIRECT_ANSWER_PATTERNS = [
    re.compile(r'\b(dose|dosing|recommended dose|standard dose|usual dose)\b', re.I),
    re.compile(r'\b(contraindicated|avoid|do not use|must not)\b', re.I),
    re.compile(r'\b(first.line|first-line|recommended|guideline-recommended)\b', re.I),
    re.compile(r'\b(evidence|trial|study|review)\s+(shows?|demonstrates?|found|confirms?)\b', re.I),
]
_CLINICAL_SPECIFICITY_PATTERN = re.compile(
    r'\b(\d+\s*mg|\d+\s*mcg|\d+\s*mmol|\d+\s*ml|eGFR|creatinine|INR|HbA1c|mmHg)\b', re.I
)


def _cross_encoder_rule_score(query: str, chunk_content: str) -> float:
    """
    Rule-based cross-encoder approximation. Production: replace with
    ms-marco-MiniLM-L-12-v2 cross-encoder model for clinical queries.
    
    Scores 0.0-1.0 based on:
    - Direct answer patterns in chunk
    - Clinical specificity (numeric values, lab results)
    - Query term density
    - Section type relevance
    """
    score = 0.0
    content_lower = chunk_content.lower()
    query_lower = query.lower()

    # Query term density
    query_terms = set(re.findall(r'\b[a-z]{4,}\b', query_lower))
    if query_terms:
        matches = sum(1 for t in query_terms if t in content_lower)
        score += 0.3 * (matches / len(query_terms))

    # Direct answer patterns
    for pat in _DIRECT_ANSWER_PATTERNS:
        if pat.search(chunk_content):
            score += 0.1

    # Clinical specificity boost — chunks with specific values are more useful
    specifics = _CLINICAL_SPECIFICITY_PATTERN.findall(chunk_content)
    score += min(0.2, len(specifics) * 0.04)

    # Black box / warning section get clinical relevance boost
    if re.search(r'black box|boxed warning|contraindicated', content_lower):
        score += 0.15

    # Guideline language boost
    if re.search(r'recommend|advise|should|must|grade [a-d]|level \d', content_lower):
        score += 0.1

    return min(score, 1.0)


class CrossEncoderReranker:
    """
    L4-11: Cross-encoder reranker between retrieval and generation.
    
    Re-scores (query, chunk) pairs with joint encoding — more accurate than
    bi-encoder similarity but more expensive, so applied only to top-K
    retrieved candidates (not the full corpus).
    
    Production: ms-marco-MiniLM-L-12-v2 fine-tuned on clinical QA pairs.
    Current: high-precision rule-based scoring.
    """

    def rerank(
        self,
        query: str,
        results: list[RetrievalResult],
        top_n: int = 10,
    ) -> list[RetrievalResult]:
        """
        Rerank retrieval results using cross-encoder scoring.
        Returns top_n results sorted by rerank_score.
        """
        for result in results:
            rerank = _cross_encoder_rule_score(query, result.chunk.content)
            # Combine: 60% rerank, 40% retrieval score
            result.rerank_score = round(rerank, 4)
            result.final_score = round(
                rerank * 0.6 + result.final_score * 0.4, 4
            )

        results.sort(key=lambda r: r.final_score, reverse=True)
        return results[:top_n]


# ─────────────────────────────────────────────────────────────────────────────
# L4-13: CONFIDENCE SCORING ENGINE
# Architecture: '8-component weighted formula. Every claim receives a score.
# Low-confidence claims trigger Cite-or-Suppress. Appended to every output.'
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ConfidenceComponents:
    """Individual components of the 8-part confidence score."""
    evidence_tier_score:  float = 0.0   # CEBM tier (0-1)
    recency_score:        float = 0.0   # Publication recency (0-1)
    sample_size_score:    float = 0.0   # Study power (0-1)
    consistency_score:    float = 0.0   # Cross-source agreement (0-1)
    jurisdiction_score:   float = 0.0   # Guideline applicability (0-1)
    staleness_penalty:    float = 0.0   # Staleness deduction (0 to -0.3)
    retraction_penalty:   float = 0.0   # Retraction risk deduction (0 to -0.5)
    citation_count_score: float = 0.0   # Number of supporting citations (0-1)

    def weighted_total(self) -> float:
        """
        Weighted confidence formula per architecture:
        0.25*tier + 0.15*recency + 0.10*sample + 0.15*consistency +
        0.10*jurisdiction + 0.15*citations + staleness_penalty + retraction_penalty
        """
        raw = (
            0.25 * self.evidence_tier_score
            + 0.15 * self.recency_score
            + 0.10 * self.sample_size_score
            + 0.15 * self.consistency_score
            + 0.10 * self.jurisdiction_score
            + 0.15 * self.citation_count_score
            + self.staleness_penalty
            + self.retraction_penalty
        )
        return max(0.0, min(1.0, raw))


@dataclass
class ClaimConfidenceScore:
    """Full confidence assessment for a clinical claim."""
    claim_text:         str
    components:         ConfidenceComponents
    final_score:        float
    grade_label:        str           # "HIGH" | "MODERATE" | "LOW" | "VERY LOW"
    suppress:           bool          # True = claim should be suppressed (Cite-or-Suppress)
    display_text:       str           # Human-readable: "Confidence: HIGH (0.82)"
    supporting_chunks:  list[str]     # Chunk IDs supporting this claim
    flag_for_review:    bool = False  # True = flag for human review (L10-5)


CONFIDENCE_THRESHOLDS = {
    "HIGH":     0.75,
    "MODERATE": 0.50,
    "LOW":      0.30,
    "VERY_LOW": 0.0,
}

SUPPRESS_THRESHOLD = 0.30   # Claims below this are suppressed


class ConfidenceScoringEngine:
    """
    L4-13: 8-component confidence scoring for every clinical claim.
    Architecture: 'Every clinical claim receives a confidence score.
    Low-confidence claims trigger Cite-or-Suppress. Score appended to output.'
    """

    def score_claim(
        self,
        claim_text: str,
        supporting_chunks: list[EvidenceChunk],
        query_jurisdiction: Optional[str] = None,
    ) -> ClaimConfidenceScore:
        """Score a single clinical claim against its supporting evidence."""
        if not supporting_chunks:
            return ClaimConfidenceScore(
                claim_text=claim_text,
                components=ConfidenceComponents(),
                final_score=0.0,
                grade_label="VERY_LOW",
                suppress=True,
                display_text="Confidence: VERY LOW (0.00) — No supporting evidence",
                supporting_chunks=[],
                flag_for_review=True,
            )

        comp = ConfidenceComponents()

        # 1. Evidence tier (best chunk tier)
        best_tier_score = max(CEBM_SCORE.get(c.evidence_tier, 0.0) for c in supporting_chunks)
        comp.evidence_tier_score = best_tier_score

        # 2. Recency (best recency among chunks)
        comp.recency_score = max(c.compute_recency_score() for c in supporting_chunks)

        # 3. Sample size (extracted from PICO or content)
        sample_sizes = []
        for chunk in supporting_chunks:
            if chunk.pico_population:
                m = re.search(r'(\d[\d,]+)', chunk.pico_population)
                if m:
                    try:
                        n = int(m.group(1).replace(',', ''))
                        if n > 0:
                            sample_sizes.append(n)
                    except ValueError:
                        pass
        if sample_sizes:
            max_n = max(sample_sizes)
            if max_n >= 10000:     comp.sample_size_score = 1.0
            elif max_n >= 1000:    comp.sample_size_score = 0.8
            elif max_n >= 100:     comp.sample_size_score = 0.6
            elif max_n >= 10:      comp.sample_size_score = 0.4
            else:                  comp.sample_size_score = 0.2
        else:
            comp.sample_size_score = 0.5  # Unknown — conservative

        # 4. Consistency (cross-source agreement)
        sources = set(c.provenance.source_api.value for c in supporting_chunks)
        if len(sources) >= 3:     comp.consistency_score = 1.0
        elif len(sources) == 2:   comp.consistency_score = 0.75
        else:                     comp.consistency_score = 0.5

        # 5. Jurisdiction match
        if query_jurisdiction:
            matching_jur = sum(
                1 for c in supporting_chunks
                if c.provenance.jurisdiction.value == query_jurisdiction.lower()
            )
            comp.jurisdiction_score = min(1.0, matching_jur / max(len(supporting_chunks), 1) + 0.3)
        else:
            comp.jurisdiction_score = 0.7  # No jurisdiction specified — partial credit

        # 6. Citation count score
        n_citations = len(supporting_chunks)
        if n_citations >= 5:     comp.citation_count_score = 1.0
        elif n_citations >= 3:   comp.citation_count_score = 0.75
        elif n_citations >= 2:   comp.citation_count_score = 0.5
        else:                    comp.citation_count_score = 0.25

        # 7. Staleness penalty
        stale_count = sum(1 for c in supporting_chunks if c.staleness_status == StalenessStatus.STALE)
        critical_count = sum(1 for c in supporting_chunks if c.staleness_status == StalenessStatus.CRITICAL)
        comp.staleness_penalty = -(stale_count * 0.05 + critical_count * 0.15)

        # 8. Retraction penalty
        corrected_count = sum(1 for c in supporting_chunks if c.retraction_status == RetractionStatus.CORRECTED)
        concern_count = sum(1 for c in supporting_chunks if c.retraction_status == RetractionStatus.EXPRESSION)
        comp.retraction_penalty = -(corrected_count * 0.1 + concern_count * 0.2)

        final = comp.weighted_total()

        # Grade label
        grade = "VERY_LOW"
        for label, threshold in sorted(CONFIDENCE_THRESHOLDS.items(), key=lambda x: -x[1]):
            if final >= threshold:
                grade = label
                break

        suppress = final < SUPPRESS_THRESHOLD
        flag = final < 0.40 or concern_count > 0

        display = f"Confidence: {grade} ({final:.2f})"
        if suppress:
            display += " — SUPPRESSED: insufficient evidence to cite"

        return ClaimConfidenceScore(
            claim_text=claim_text,
            components=comp,
            final_score=round(final, 3),
            grade_label=grade,
            suppress=suppress,
            display_text=display,
            supporting_chunks=[c.chunk_id for c in supporting_chunks],
            flag_for_review=flag,
        )

    def score_evidence_pack(
        self,
        chunks: list[EvidenceChunk],
        jurisdiction: Optional[str] = None,
    ) -> float:
        """Score overall evidence pack quality (for L4-2 Constrained Generator)."""
        if not chunks:
            return 0.0
        scores = []
        for chunk in chunks:
            # Score each chunk as if it were a standalone claim
            result = self.score_claim(
                claim_text="[pack-level assessment]",
                supporting_chunks=[chunk],
                query_jurisdiction=jurisdiction,
            )
            scores.append(result.final_score)
        return round(sum(scores) / len(scores), 3)


# ─────────────────────────────────────────────────────────────────────────────
# L4-14: EVIDENCE HASH-LOCK ENFORCEMENT
# Architecture: 'Every claim binds to chunk_id + byte_offset + snippet_hash.
# LLM-generated IDs not in pack = REJECTED. Append-only claim ledger.'
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ClaimBinding:
    """Binding of a claim to its exact evidence source."""
    claim_id:       str = field(default_factory=lambda: str(uuid.uuid4()))
    claim_text:     str = ""
    chunk_id:       str = ""
    byte_offset:    int = 0
    snippet_hash:   str = ""
    verified:       bool = False
    rejection_reason: Optional[str] = None
    bound_at:       datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class EvidenceHashLockEnforcer:
    """
    L4-14: Hash-lock enforcement for all clinical claims.
    
    Architecture requirements:
    - Every claim binds to: chunk_id + byte_offset + snippet_hash
    - LLM output referencing chunk_id NOT in the evidence pack → REJECTED
    - If hash of retrieved chunk ≠ stored hash → REJECTED (tamper detected)
    - All bindings stored in append-only claim ledger
    - System REFUSES retrieval if embedding model mismatch
    """

    def __init__(self) -> None:
        self._claim_ledger: list[ClaimBinding] = []

    def verify_claim_binding(
        self,
        claim_text: str,
        claimed_chunk_id: str,
        evidence_pack_chunk_ids: set[str],
        chunk_registry: dict[str, EvidenceChunk],
    ) -> ClaimBinding:
        """
        Verify that a claim's cited chunk_id:
        1. Exists in the evidence pack (not hallucinated)
        2. Has a valid, matching hash (not tampered)
        """
        binding = ClaimBinding(claim_text=claim_text, chunk_id=claimed_chunk_id)

        # Check 1: chunk_id must be in the closed evidence pack
        if claimed_chunk_id not in evidence_pack_chunk_ids:
            binding.verified = False
            binding.rejection_reason = (
                f"HALLUCINATED_EVIDENCE_ID: chunk_id '{claimed_chunk_id}' "
                f"not found in evidence pack. This ID was generated by the LLM "
                f"and does not correspond to any retrieved evidence. CLAIM REJECTED."
            )
            logger.error(f"Hash-lock violation: hallucinated chunk_id {claimed_chunk_id}")
            self._claim_ledger.append(binding)
            return binding

        # Check 2: retrieve actual chunk and verify hash
        chunk = chunk_registry.get(claimed_chunk_id)
        if not chunk:
            binding.verified = False
            binding.rejection_reason = f"CHUNK_NOT_FOUND: {claimed_chunk_id} in pack but missing from registry."
            self._claim_ledger.append(binding)
            return binding

        # Hash verification
        computed_hash = hashlib.sha256(chunk.content.encode("utf-8")).hexdigest()
        stored_hash = chunk.provenance.snippet_hash

        if computed_hash != stored_hash:
            binding.verified = False
            binding.rejection_reason = (
                f"HASH_MISMATCH: chunk '{claimed_chunk_id}' hash verification failed. "
                f"Expected: {stored_hash[:16]}... Got: {computed_hash[:16]}... "
                f"Evidence tampered or corrupted. CLAIM REJECTED."
            )
            logger.critical(f"Evidence tamper detected: chunk {claimed_chunk_id}")
            self._claim_ledger.append(binding)
            return binding

        # All checks passed
        binding.snippet_hash = stored_hash
        binding.byte_offset = chunk.byte_offset
        binding.verified = True
        self._claim_ledger.append(binding)
        return binding

    def verify_pack_integrity(
        self,
        chunks: list[EvidenceChunk],
        pack_hash: str,
    ) -> tuple[bool, str]:
        """Verify the complete evidence pack hash has not been tampered with."""
        computed_pack_content = "|".join(sorted([
            f"{c.chunk_id}:{c.provenance.snippet_hash}" for c in chunks
        ]))
        computed_hash = hashlib.sha256(computed_pack_content.encode()).hexdigest()
        if computed_hash != pack_hash:
            return False, (
                f"PACK_TAMPERED: Evidence pack hash mismatch. "
                f"Expected: {pack_hash[:16]}... Got: {computed_hash[:16]}... "
                "Evidence pack integrity compromised — session terminated."
            )
        return True, "OK"

    def verify_embedding_model_compatibility(
        self,
        chunk: EvidenceChunk,
        query_model_id: str = CURRENT_EMBEDDING_MODEL_ID,
    ) -> tuple[bool, str]:
        """REFUSE retrieval if embedding model mismatch."""
        if not chunk.embedding_model_id:
            return False, "EMBEDDING_MODEL_UNKNOWN: Cannot verify index-time model."
        stored_model = chunk.embedding_model_id.split(":")[0]
        if stored_model != query_model_id.split(":")[0]:
            return False, (
                f"EMBEDDING_MODEL_MISMATCH: index='{stored_model}' ≠ query='{query_model_id}'. "
                "Full re-index required before serving. RETRIEVAL REFUSED."
            )
        return True, "OK"

    def get_ledger_stats(self) -> dict:
        total = len(self._claim_ledger)
        verified = sum(1 for b in self._claim_ledger if b.verified)
        rejected = total - verified
        return {
            "total_claims": total,
            "verified": verified,
            "rejected": rejected,
            "rejection_rate": round(rejected / max(total, 1), 3),
            "hallucination_count": sum(
                1 for b in self._claim_ledger
                if b.rejection_reason and "HALLUCINATED" in b.rejection_reason
            ),
        }
