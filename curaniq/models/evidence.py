"""
CURANIQ — Evidence Models (Compatibility Shim)
Re-exports from schemas.py for backward compatibility.
All classes defined in schemas.py are the canonical source.
"""
# Re-export shared enums and classes from schemas.py (canonical source)
from curaniq.models.schemas import (  # noqa: F401
    EvidenceTier,
    Jurisdiction,
    EvidenceObject,
    EvidencePack,
    SourceAPI,
    StalenessStatus,
    RetractionStatus,
)

# Backward compatibility alias
EvidenceChunk = EvidenceObject

# ─────────────────────────────────────────────────────────────────
# ADDITIONAL MODELS (unique to evidence layer, not in schemas.py)
# ─────────────────────────────────────────────────────────────────

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field


class EvidenceProvenanceChain(BaseModel):
    """Immutable provenance chain per L4-14."""
    source_api:                SourceAPI
    retrieval_timestamp:       datetime
    document_version:          Optional[str] = None
    snippet_hash:              str
    ingestion_pipeline_version: str = "1.0.0"
    source_doi:                Optional[str] = None
    publication_date:          Optional[datetime] = None
    jurisdiction:              Jurisdiction = Jurisdiction.INT
    evidence_tier:             EvidenceTier = EvidenceTier.COHORT
    chunk_position:            int = 0
    parent_document_id:        str = Field(default_factory=lambda: str(uuid.uuid4()))

    model_config = {"frozen": True}

    def is_complete(self) -> bool:
        required = [self.source_api, self.retrieval_timestamp, self.snippet_hash]
        return all(f is not None for f in required)

    def verify_hash(self, content_bytes: bytes) -> bool:
        return hashlib.sha256(content_bytes).hexdigest() == self.snippet_hash


# Evidence quality scores per Oxford CEBM
CEBM_SCORE: dict[EvidenceTier, float] = {
    EvidenceTier.SYSTEMATIC_REVIEW: 1.0,
    EvidenceTier.RCT:               0.9,
    EvidenceTier.GUIDELINE:         0.85,
    EvidenceTier.COHORT:            0.7,
    EvidenceTier.CASE_REPORT:       0.5,
    EvidenceTier.EXPERT_OPINION:    0.3,
    EvidenceTier.PREPRINT:          0.0,
}

# Staleness TTL per source (hours)
STALENESS_TTL_HOURS: dict[SourceAPI, float] = {
    SourceAPI.OPENFDA_LABELS:   1.0,
    SourceAPI.DAILYMED_SPL:     48.0,
    SourceAPI.PUBMED:           24.0,
    SourceAPI.CROSSREF:         0.0,
    SourceAPI.RETRACTION_WATCH: 0.0,
    SourceAPI.OPENFDA_FAERS:    336.0,
    SourceAPI.NICE_GUIDELINES:  720.0,
    SourceAPI.COCHRANE:         720.0,
    SourceAPI.LACTMED:          168.0,
    SourceAPI.CLINICAL_TRIALS:  6.0,
}

# Sources where expired TTL = REFUSE (safety-critical)
FAIL_CLOSED_SOURCES: set[SourceAPI] = {
    SourceAPI.OPENFDA_LABELS,
    SourceAPI.OPENFDA_FAERS,
    SourceAPI.CROSSREF,
    SourceAPI.RETRACTION_WATCH,
    SourceAPI.DAILYMED_SPL,
}
