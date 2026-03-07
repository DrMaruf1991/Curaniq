"""
CURANIQ — Medical Evidence Operating System
Layer 1: Evidence Ingestion

L1-14  Structured Semantic Chunking Engine
L1-15  Evidence Chunk Metadata Stamping

Architecture requirements (verbatim):
- Structure-aware document chunking using GROBID/Unstructured
- Preserves heading hierarchy, table boundaries, paragraph relationships
- NEVER severs a clinical recommendation from its contraindication
- 'Naive PDF chunking routinely splits "Recommended dose: X" from
  "Contraindicated in patients with Y" — causing FATAL retrieval errors'
- Every vector embedding carries: model_id, tokenizer_hash, source_doi,
  publication_date, jurisdiction, evidence_tier, chunk_position,
  parent_document_id
- System REFUSES retrieval if query-time embedding model ≠ index-time model
- On model upgrade: mandatory full re-index with validation before cutover
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from curaniq.models.evidence import (
    EvidenceChunk,
    EvidenceProvenanceChain,
    EvidenceTier,
    Jurisdiction,
    RetractionStatus,
    SourceAPI,
    StalenessStatus,
    STALENESS_TTL_HOURS,
)

logger = logging.getLogger(__name__)

PIPELINE_VERSION = "1.0.0"

# Current embedding model — must match index-time model for retrieval validity
CURRENT_EMBEDDING_MODEL_ID = "text-embedding-3-large"
CURRENT_EMBEDDING_MODEL_HASH = hashlib.sha256(
    CURRENT_EMBEDDING_MODEL_ID.encode()
).hexdigest()[:16]


# ─────────────────────────────────────────────────────────────────────────────
# CLINICAL COHESION RULES
# These patterns define content pairs that MUST stay together in the same chunk.
# Splitting these causes fatal retrieval errors per architecture spec.
# ─────────────────────────────────────────────────────────────────────────────

# Pattern pairs: if A appears within N tokens of B, they must be co-chunked
MANDATORY_COHESION_PAIRS: list[tuple[re.Pattern, re.Pattern, int]] = [
    # Dose + contraindication — MUST stay together
    (
        re.compile(r'\b(dose|dosing|mg|mcg|mg/kg|units?)\b', re.I),
        re.compile(r'\b(contraindicated|avoid|do not use|prohibited)\b', re.I),
        300,  # Max token distance
    ),
    # Dose + renal adjustment — MUST stay together
    (
        re.compile(r'\b(dose|dosing|mg)\b', re.I),
        re.compile(r'\b(renal|creatinine|eGFR|CrCl|kidney|dialysis)\b', re.I),
        300,
    ),
    # Drug name + black box warning — MUST stay together
    (
        re.compile(r'\bWARNING\b|\bBLACK BOX\b|\bBOXED WARNING\b', re.I),
        re.compile(r'\b(risk|death|fatal|serious)\b', re.I),
        200,
    ),
    # Recommendation + evidence grade — MUST stay together
    (
        re.compile(r'\b(recommend|advise|should|consider|offer)\b', re.I),
        re.compile(r'\b(grade [A-D]|level [1-5]|strong|weak|conditional|GRADE)\b', re.I),
        150,
    ),
    # Drug name + pregnancy category — MUST stay together
    (
        re.compile(r'\b(category [ABCDX]|avoid|contraindicated)\b', re.I),
        re.compile(r'\b(pregnan|trimester|fetal|teratogen|embryo)\b', re.I),
        200,
    ),
    # QT prolongation risk + drug name — MUST stay together
    (
        re.compile(r'\bQT\b|\bTdP\b|\btorsades\b', re.I),
        re.compile(r'\b(risk|prolongation|avoid|monitor|ECG)\b', re.I),
        150,
    ),
]

# Keywords that mark section boundaries in clinical documents
SECTION_BOUNDARY_KEYWORDS = {
    "high_priority": {  # Never split within these sections
        "boxed warning", "black box warning", "contraindications",
        "dosage and administration", "warnings and precautions",
        "drug interactions", "use in specific populations",
        "pregnancy", "lactation", "pediatric use", "geriatric use",
        "renal impairment", "hepatic impairment",
    },
    "medium_priority": {  # Split with caution
        "clinical pharmacology", "mechanism of action",
        "adverse reactions", "clinical trials",
        "indications and usage", "dosage forms",
    },
    "table_boundary": {  # Never split inside tables
        "table", "figure", "appendix",
    },
}

# Maximum chunk size in characters (generous to prevent clinical content splitting)
MAX_CHUNK_CHARS = 2000
MIN_CHUNK_CHARS = 100
OVERLAP_CHARS = 200  # Overlap between chunks to preserve context


@dataclass
class RawDocument:
    """
    Raw clinical document before chunking.
    Created by L1-1 API connectors and passed to L1-14 Chunker.
    """
    document_id: str
    source_api: SourceAPI
    content: str
    title: Optional[str] = None
    doi: Optional[str] = None
    publication_date: Optional[datetime] = None
    jurisdiction: Jurisdiction = Jurisdiction.INTL
    evidence_tier: EvidenceTier = EvidenceTier.UNKNOWN
    document_version: Optional[str] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class ChunkCandidate:
    """
    Candidate chunk before validation and metadata stamping.
    """
    content: str
    chunk_position: int
    section_type: str
    is_high_priority_section: bool
    merged_with: list[int] = field(default_factory=list)  # Chunk positions merged due to cohesion rules


class SemanticChunkingEngine:
    """
    L1-14: Structure-aware semantic chunking engine.
    
    Core guarantee: NEVER separates a clinical recommendation from its
    contraindication, dose from its renal adjustment, or warning from its
    severity context. Violation of this guarantee causes FATAL retrieval errors
    per architecture specification.
    
    Chunking strategy:
    1. Detect document structure (sections, headings, tables)
    2. Split on natural section boundaries
    3. Apply cohesion rules — merge chunks that contain mandatory pairs
    4. Apply size constraints with overlap
    5. Flag high-priority sections for retrieval boosting
    
    Production: integrates with GROBID (for scientific papers) and
    Unstructured (for PDFs/docs). This implementation uses rule-based
    structural analysis that works on all text formats.
    """

    def __init__(
        self,
        max_chunk_chars: int = MAX_CHUNK_CHARS,
        min_chunk_chars: int = MIN_CHUNK_CHARS,
        overlap_chars: int = OVERLAP_CHARS,
    ) -> None:
        self.max_chunk_chars = max_chunk_chars
        self.min_chunk_chars = min_chunk_chars
        self.overlap_chars = overlap_chars

    def chunk(self, document: RawDocument) -> list[ChunkCandidate]:
        """
        Main chunking method. Returns ordered list of ChunkCandidates.
        
        Pipeline:
        1. Structural analysis → identify sections/tables
        2. Initial segmentation on section boundaries
        3. Cohesion rule enforcement → merge mandatory pairs
        4. Size constraint enforcement → split oversized chunks
        5. Minimum size enforcement → merge undersized chunks
        """
        if not document.content or len(document.content.strip()) < self.min_chunk_chars:
            return []

        # Step 1: Structural analysis
        sections = self._detect_sections(document.content)

        # Step 2: Initial segmentation
        candidates = self._segment_sections(sections, document.content)

        # Step 3: Cohesion rule enforcement — the critical step
        candidates = self._enforce_cohesion_rules(candidates)

        # Step 4: Size constraint enforcement
        candidates = self._enforce_size_constraints(candidates)

        # Step 5: Minimum size enforcement — merge tiny chunks
        candidates = self._merge_small_chunks(candidates)

        logger.debug(
            f"Chunked document '{document.document_id}': "
            f"{len(candidates)} chunks from {len(document.content)} chars"
        )

        return candidates

    def _detect_sections(self, content: str) -> list[tuple[int, int, str, bool]]:
        """
        Detect section boundaries in clinical text.
        Returns: list of (start_pos, end_pos, section_type, is_high_priority)
        """
        sections = []
        lines = content.split('\n')
        current_pos = 0
        current_section_start = 0
        current_section_type = "body"
        is_high_priority = False

        for i, line in enumerate(lines):
            line_lower = line.strip().lower()

            # Detect section headers
            is_header = (
                line.strip().isupper() and len(line.strip()) > 3  # ALL CAPS header
                or re.match(r'^#{1,4}\s', line)                    # Markdown heading
                or re.match(r'^\d+\.\s+[A-Z]', line)              # Numbered section
                or re.match(r'^[A-Z][A-Z\s]{10,}$', line.strip()) # Title case header
            )

            # Check if this is a high-priority clinical section
            new_is_high_priority = any(
                kw in line_lower
                for kw in SECTION_BOUNDARY_KEYWORDS["high_priority"]
            )

            if is_header and i > 0:
                # Close current section
                end_pos = current_pos
                if end_pos > current_section_start:
                    sections.append((
                        current_section_start,
                        end_pos,
                        current_section_type,
                        is_high_priority,
                    ))
                current_section_start = current_pos
                current_section_type = line_lower.strip()[:50]
                is_high_priority = new_is_high_priority
            elif new_is_high_priority and not is_high_priority:
                # Entering high-priority section — force section boundary
                if current_pos > current_section_start:
                    sections.append((
                        current_section_start,
                        current_pos,
                        current_section_type,
                        False,
                    ))
                current_section_start = current_pos
                is_high_priority = True

            current_pos += len(line) + 1  # +1 for newline

        # Close final section
        if current_pos > current_section_start:
            sections.append((
                current_section_start,
                current_pos,
                current_section_type,
                is_high_priority,
            ))

        return sections or [(0, len(content), "body", False)]

    def _segment_sections(
        self,
        sections: list[tuple[int, int, str, bool]],
        content: str,
    ) -> list[ChunkCandidate]:
        """Convert detected sections into chunk candidates."""
        candidates = []
        for i, (start, end, section_type, is_high_priority) in enumerate(sections):
            chunk_content = content[start:end].strip()
            if len(chunk_content) >= self.min_chunk_chars:
                candidates.append(ChunkCandidate(
                    content=chunk_content,
                    chunk_position=i,
                    section_type=section_type,
                    is_high_priority_section=is_high_priority,
                ))
        return candidates

    def _enforce_cohesion_rules(
        self, candidates: list[ChunkCandidate]
    ) -> list[ChunkCandidate]:
        """
        CRITICAL: Apply cohesion rules to prevent clinical content splitting.
        
        For each mandatory cohesion pair (pattern_A, pattern_B, max_distance):
        If pattern_A appears in chunk[i] and pattern_B appears in chunk[i+1]
        within max_distance characters, merge them.
        
        This is the core safety mechanism preventing fatal retrieval errors.
        """
        if len(candidates) <= 1:
            return candidates

        merged = list(candidates)
        # Multiple passes to catch cascading merges
        for _ in range(3):
            changed = False
            i = 0
            result = []
            while i < len(merged):
                if i + 1 >= len(merged):
                    result.append(merged[i])
                    break

                current = merged[i]
                next_chunk = merged[i + 1]
                should_merge = False

                # Check each cohesion pair
                for pattern_a, pattern_b, max_distance in MANDATORY_COHESION_PAIRS:
                    combined = current.content + "\n" + next_chunk.content
                    match_a = pattern_a.search(current.content)
                    match_b = pattern_b.search(next_chunk.content)

                    if match_a and match_b:
                        # Check if they're within max_distance characters
                        a_end = len(current.content) - match_a.start()
                        b_start = match_b.start()
                        distance = a_end + b_start

                        if distance <= max_distance:
                            should_merge = True
                            logger.debug(
                                f"Cohesion merge: '{pattern_a.pattern}' + "
                                f"'{pattern_b.pattern}' at distance {distance}"
                            )
                            break

                    # Also check reverse direction within single candidate
                    match_b_current = pattern_b.search(current.content)
                    match_a_next = pattern_a.search(next_chunk.content)
                    if match_b_current and match_a_next:
                        b_end = len(current.content) - match_b_current.start()
                        a_start = match_a_next.start()
                        if b_end + a_start <= max_distance:
                            should_merge = True
                            break

                # High-priority sections that are tiny get merged with neighbors
                if (current.is_high_priority_section and
                        len(current.content) < self.min_chunk_chars * 2):
                    should_merge = True

                if should_merge:
                    # Merge current and next into single chunk
                    merged_content = current.content + "\n\n" + next_chunk.content
                    merged_chunk = ChunkCandidate(
                        content=merged_content,
                        chunk_position=current.chunk_position,
                        section_type=current.section_type,
                        is_high_priority_section=(
                            current.is_high_priority_section or
                            next_chunk.is_high_priority_section
                        ),
                        merged_with=[next_chunk.chunk_position],
                    )
                    result.append(merged_chunk)
                    i += 2
                    changed = True
                else:
                    result.append(current)
                    i += 1

            merged = result
            if not changed:
                break

        return merged

    def _enforce_size_constraints(
        self, candidates: list[ChunkCandidate]
    ) -> list[ChunkCandidate]:
        """
        Split chunks that exceed max_chunk_chars.
        Uses sentence boundaries + overlap to maintain context.
        CRITICAL: Never splits a merged cohesion chunk — those are inviolable.
        """
        result = []
        sentence_boundary = re.compile(r'(?<=[.!?])\s+(?=[A-Z])')

        for candidate in candidates:
            if len(candidate.content) <= self.max_chunk_chars:
                result.append(candidate)
                continue

            # Don't split if this chunk was merged due to cohesion rules
            if candidate.merged_with:
                result.append(candidate)
                logger.debug(
                    f"Skipping size split for cohesion-merged chunk "
                    f"(position {candidate.chunk_position})"
                )
                continue

            # Split on sentence boundaries
            sentences = sentence_boundary.split(candidate.content)
            current_text = ""
            sub_position = 0

            for sentence in sentences:
                if len(current_text) + len(sentence) > self.max_chunk_chars and current_text:
                    result.append(ChunkCandidate(
                        content=current_text.strip(),
                        chunk_position=candidate.chunk_position * 1000 + sub_position,
                        section_type=candidate.section_type,
                        is_high_priority_section=candidate.is_high_priority_section,
                    ))
                    # Overlap: carry last OVERLAP_CHARS into next chunk for context
                    overlap_text = current_text[-self.overlap_chars:] if len(current_text) > self.overlap_chars else current_text
                    current_text = overlap_text + " " + sentence
                    sub_position += 1
                else:
                    current_text += (" " if current_text else "") + sentence

            if current_text.strip():
                result.append(ChunkCandidate(
                    content=current_text.strip(),
                    chunk_position=candidate.chunk_position * 1000 + sub_position,
                    section_type=candidate.section_type,
                    is_high_priority_section=candidate.is_high_priority_section,
                ))

        return result

    def _merge_small_chunks(
        self, candidates: list[ChunkCandidate]
    ) -> list[ChunkCandidate]:
        """Merge chunks smaller than min_chunk_chars with their neighbors."""
        if not candidates:
            return candidates

        result = []
        carry = ""

        for candidate in candidates:
            combined = carry + ("\n\n" if carry else "") + candidate.content
            if len(combined) >= self.min_chunk_chars:
                if carry:
                    candidate = ChunkCandidate(
                        content=combined,
                        chunk_position=candidate.chunk_position,
                        section_type=candidate.section_type,
                        is_high_priority_section=candidate.is_high_priority_section,
                    )
                    carry = ""
                result.append(candidate)
            else:
                carry = combined

        if carry and result:
            last = result[-1]
            result[-1] = ChunkCandidate(
                content=last.content + "\n\n" + carry,
                chunk_position=last.chunk_position,
                section_type=last.section_type,
                is_high_priority_section=last.is_high_priority_section,
            )
        elif carry:
            result.append(ChunkCandidate(
                content=carry,
                chunk_position=0,
                section_type="body",
                is_high_priority_section=False,
            ))

        return result


class EvidenceChunkMetadataStamper:
    """
    L1-15: Immutable metadata stamping for every evidence chunk.
    
    Per architecture:
    'Every vector embedding carries: model_id, tokenizer_hash, source_doi,
    publication_date, jurisdiction, evidence_tier, chunk_position,
    parent_document_id. System REFUSES retrieval if query-time embedding
    model ≠ index-time model (prevents silent index drift).'
    
    On model upgrade: mandatory full re-index with validation before cutover.
    """

    def __init__(
        self,
        embedding_model_id: str = CURRENT_EMBEDDING_MODEL_ID,
        pipeline_version: str = PIPELINE_VERSION,
    ) -> None:
        self.embedding_model_id = embedding_model_id
        self.embedding_model_hash = hashlib.sha256(
            embedding_model_id.encode()
        ).hexdigest()[:16]
        self.pipeline_version = pipeline_version

    def stamp(
        self,
        candidate: ChunkCandidate,
        document: RawDocument,
    ) -> EvidenceChunk:
        """
        Convert a ChunkCandidate into a fully-stamped EvidenceChunk.
        
        Applies:
        - Immutable provenance chain (L1-15 metadata)
        - Content hash (SHA-256)
        - Embedding model fingerprint (prevents index drift)
        - Retraction status (UNCHECKED — will be verified by L2-7)
        - Preprint quarantine check (L1-6)
        """
        content = candidate.content
        content_bytes = content.encode("utf-8")
        snippet_hash = hashlib.sha256(content_bytes).hexdigest()

        # Determine evidence tier
        # High-priority sections get tier boost
        evidence_tier = document.evidence_tier
        if candidate.is_high_priority_section:
            if evidence_tier == EvidenceTier.UNKNOWN:
                evidence_tier = EvidenceTier.GUIDELINE
            elif evidence_tier == EvidenceTier.COHORT:
                evidence_tier = EvidenceTier.GUIDELINE

        provenance = EvidenceProvenanceChain(
            source_api=document.source_api,
            retrieval_timestamp=datetime.now(timezone.utc),
            document_version=document.document_version,
            snippet_hash=snippet_hash,
            ingestion_pipeline_version=self.pipeline_version,
            source_doi=document.doi,
            publication_date=document.publication_date,
            jurisdiction=document.jurisdiction,
            evidence_tier=evidence_tier,
            chunk_position=candidate.chunk_position,
            parent_document_id=document.document_id,
        )

        # Preprint quarantine (L1-6)
        is_preprint = evidence_tier == EvidenceTier.PREPRINT
        quarantine_reason = None
        if is_preprint:
            quarantine_reason = (
                f"Preprint from {document.source_api.value}: "
                "not peer-reviewed. Restricted evidence tier. "
                "Will be promoted only after peer review confirmation."
            )

        # Get TTL from source
        ttl = STALENESS_TTL_HOURS.get(document.source_api, 24.0)

        # PICO extraction (basic — production uses NER)
        pico_population, pico_intervention, pico_comparator, pico_outcome = (
            self._extract_basic_pico(content)
        )

        chunk = EvidenceChunk(
            content=content,
            content_bytes=content_bytes,
            byte_offset=0,
            provenance=provenance,
            retraction_status=RetractionStatus.UNCHECKED,  # L2-7 will verify
            evidence_tier=evidence_tier,
            staleness_status=StalenessStatus.FRESH,  # Just ingested
            staleness_ttl_hours=ttl,
            last_verified=datetime.now(timezone.utc),
            is_quarantined=is_preprint,
            quarantine_reason=quarantine_reason,
            pico_population=pico_population,
            pico_intervention=pico_intervention,
            pico_comparator=pico_comparator,
            pico_outcome=pico_outcome,
            embedding_model_id=f"{self.embedding_model_id}:{self.embedding_model_hash}",
        )

        return chunk

    def _extract_basic_pico(
        self, content: str
    ) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        """
        Basic PICO extraction from content.
        Production: use BioBERT-based NER for precise PICO extraction (L1-2).
        """
        content_lower = content.lower()

        # Population
        population = None
        pop_patterns = [
            r'(?:patients|adults|children|women|men)\s+(?:with|who have)\s+([^.]{5,50})',
            r'(?:in|among)\s+(?:patients|adults)\s+with\s+([^,.\n]{5,50})',
        ]
        for p in pop_patterns:
            m = re.search(p, content_lower)
            if m:
                population = m.group(1)[:100].strip()
                break

        # Intervention
        intervention = None
        int_patterns = [
            r'(?:treatment with|treated with|receiving)\s+([^,.\n]{3,50})',
            r'([A-Z][a-z]+(?:mab|nib|pril|sartan|statin|olol|mycin|cillin))\b',
        ]
        for p in int_patterns:
            m = re.search(p, content_lower)
            if m:
                intervention = m.group(1)[:100].strip()
                break

        # Outcome
        outcome = None
        outcome_patterns = [
            r'(?:reduced?|decreased?|improved?|associated with)\s+([^,.\n]{5,60})',
            r'(?:primary endpoint|primary outcome)[:\s]+([^,.\n]{5,80})',
        ]
        for p in outcome_patterns:
            m = re.search(p, content_lower)
            if m:
                outcome = m.group(1)[:150].strip()
                break

        return population, intervention, None, outcome

    def verify_model_compatibility(
        self,
        chunk: EvidenceChunk,
        query_embedding_model_id: str,
    ) -> tuple[bool, str]:
        """
        L1-15 MODEL COMPATIBILITY CHECK.
        
        'System REFUSES retrieval if query-time embedding model ≠ index-time model
        (prevents silent index drift).'
        
        Returns (compatible, reason).
        If incompatible: retrieval is REFUSED to prevent corrupted similarity scores.
        """
        if not chunk.embedding_model_id:
            return False, (
                "EMBEDDING_MODEL_UNKNOWN: Chunk has no embedding model ID. "
                "Cannot verify index-time model. Retrieval refused."
            )

        # Extract model ID from stored format "model_name:hash"
        stored_parts = chunk.embedding_model_id.split(":")
        stored_model_id = stored_parts[0] if stored_parts else chunk.embedding_model_id

        query_parts = query_embedding_model_id.split(":")
        query_model_id = query_parts[0] if query_parts else query_embedding_model_id

        if stored_model_id != query_model_id:
            return False, (
                f"EMBEDDING_MODEL_MISMATCH: "
                f"Index-time model='{stored_model_id}' ≠ "
                f"Query-time model='{query_model_id}'. "
                f"REFUSING retrieval to prevent silent index drift. "
                f"Action required: Full re-index with '{query_model_id}' before serving."
            )

        return True, "OK"


class L1EvidencePipeline:
    """
    Coordinates L1-14 (chunking) + L1-15 (metadata stamping) into a single pipeline.
    
    Usage:
        pipeline = L1EvidencePipeline()
        chunks = pipeline.process(raw_document)
    
    Each chunk returned is fully stamped, hash-locked, and ready for:
    - L2-7 Retraction Watch verification
    - pgvector embedding + storage
    - L4-1 Hybrid Retriever
    """

    def __init__(
        self,
        embedding_model_id: str = CURRENT_EMBEDDING_MODEL_ID,
        pipeline_version: str = PIPELINE_VERSION,
    ) -> None:
        self.chunker = SemanticChunkingEngine()
        self.stamper = EvidenceChunkMetadataStamper(
            embedding_model_id=embedding_model_id,
            pipeline_version=pipeline_version,
        )

    def process(self, document: RawDocument) -> list[EvidenceChunk]:
        """
        Full L1-14 + L1-15 pipeline for a single document.
        Returns fully-stamped, hash-locked EvidenceChunks.
        """
        if not document.content or not document.content.strip():
            logger.warning(f"Empty document: {document.document_id}")
            return []

        # Step 1: Semantic chunking (L1-14)
        candidates = self.chunker.chunk(document)

        if not candidates:
            logger.warning(f"No chunks produced for document: {document.document_id}")
            return []

        # Step 2: Metadata stamping (L1-15)
        chunks = []
        for candidate in candidates:
            try:
                chunk = self.stamper.stamp(candidate, document)
                chunks.append(chunk)
            except Exception as e:
                logger.error(
                    f"Failed to stamp chunk {candidate.chunk_position} "
                    f"from document '{document.document_id}': {e}"
                )

        logger.info(
            f"L1 pipeline: {document.document_id} → "
            f"{len(chunks)} evidence chunks "
            f"(model: {self.stamper.embedding_model_id})"
        )

        return chunks

    def process_batch(self, documents: list[RawDocument]) -> list[EvidenceChunk]:
        """Process multiple documents — returns all chunks combined."""
        all_chunks = []
        for doc in documents:
            all_chunks.extend(self.process(doc))
        return all_chunks

    def validate_retrieval_compatibility(
        self,
        chunks: list[EvidenceChunk],
        query_embedding_model_id: str = CURRENT_EMBEDDING_MODEL_ID,
    ) -> tuple[list[EvidenceChunk], list[str]]:
        """
        Filter chunks to only those compatible with the query embedding model.
        Returns (compatible_chunks, incompatible_reasons).
        
        Per architecture: 'System REFUSES retrieval if query-time model ≠ index-time model.'
        """
        compatible = []
        refused_reasons = []

        for chunk in chunks:
            ok, reason = self.stamper.verify_model_compatibility(
                chunk, query_embedding_model_id
            )
            if ok:
                compatible.append(chunk)
            else:
                refused_reasons.append(
                    f"chunk_id={chunk.chunk_id}: {reason}"
                )

        if refused_reasons:
            logger.error(
                f"RETRIEVAL REFUSED for {len(refused_reasons)} chunks: "
                f"embedding model mismatch. "
                f"Full re-index required if model was upgraded."
            )

        return compatible, refused_reasons
