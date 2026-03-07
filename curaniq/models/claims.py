"""
CURANIQ — Claim Models (Compatibility Shim)
Re-exports from schemas.py for backward compatibility.
"""
# Re-export shared classes from schemas.py (canonical source)
from curaniq.models.schemas import (  # noqa: F401
    ClaimType,
    AtomicClaim,
    ClaimContract,
    ClaimVerdict,
    VerifierDecision,
    SnippetClaimBinding,
    ConfidenceLevel,
)

# High-risk claim types — trigger full adversarial verification (L4-12)
HIGH_RISK_CLAIM_TYPES: set[ClaimType] = {
    ClaimType.DOSING,
    ClaimType.CONTRAINDICATION,
    ClaimType.DRUG_INTERACTION,
}

# ─────────────────────────────────────────────────────────────────
# ADDITIONAL MODELS (unique to claims layer)
# ─────────────────────────────────────────────────────────────────

from pydantic import BaseModel, Field
from typing import Optional


class ClinicalQueryRequest(BaseModel):
    """Incoming clinical query for API layer."""
    query_text: str
    user_role: str = "clinician"
    mode: Optional[str] = None
    jurisdiction: str = "INT"
    session_id: Optional[str] = None


class ClinicalQueryResponse(BaseModel):
    """Outgoing response wrapper for API layer."""
    query_id: str
    claims: list[dict] = Field(default_factory=list)
    refused: bool = False
    refusal_reason: Optional[str] = None
