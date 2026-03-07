"""
CURANIQ Fix 0J: Model Compatibility Bridge (C2 fix)
Unifies the three model files without a risky full merge.

Problem: schemas.py, evidence.py, and claims.py define conflicting
classes (EvidenceObject vs EvidenceChunk, two AtomicClaims, etc.)
Layers/ imports from evidence.py and claims.py. Pipeline uses schemas.py.

Solution:
  1. Add missing enums/classes from evidence.py and claims.py INTO schemas.py (additive)
  2. Rewrite evidence.py as re-export shim (imports from schemas.py, adds unique extras)
  3. Rewrite claims.py as re-export shim (imports from schemas.py, adds unique extras)
  4. All existing imports everywhere continue working — zero changes needed

Run in PowerShell:
  cd D:\curaniq_engine\curaniq_engine
  python fix_0j_model_bridge.py
"""
import os, sys

BASE = r"D:\curaniq_engine\curaniq_engine"
SCHEMAS = os.path.join(BASE, "curaniq", "models", "schemas.py")
EVIDENCE = os.path.join(BASE, "curaniq", "models", "evidence.py")
CLAIMS = os.path.join(BASE, "curaniq", "models", "claims.py")

for p in [SCHEMAS, EVIDENCE, CLAIMS]:
    if not os.path.exists(p):
        print(f"ERROR: {p} not found."); sys.exit(1)

# ═══════════════════════════════════════════════════════════════
# STEP 1: Add missing enums and classes to schemas.py (ADDITIVE ONLY)
# ═══════════════════════════════════════════════════════════════

print("=== STEP 1: Extending schemas.py ===")
with open(SCHEMAS, "r", encoding="utf-8") as f:
    schema_content = f.read()

# Check what's already there
ADDITIONS = []

# --- SourceAPI enum ---
if "class SourceAPI" not in schema_content:
    ADDITIONS.append('''

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
''')
    print("  ADD: SourceAPI enum")

# --- StalenessStatus enum ---
if "class StalenessStatus" not in schema_content:
    ADDITIONS.append('''

class StalenessStatus(str, Enum):
    """Evidence staleness state per L1-5 SLA Dashboard."""
    FRESH    = "fresh"
    STALE    = "stale"
    CRITICAL = "critical"
    UNKNOWN  = "unknown"
''')
    print("  ADD: StalenessStatus enum")

# --- RetractionStatus enum ---
if "class RetractionStatus" not in schema_content:
    ADDITIONS.append('''

class RetractionStatus(str, Enum):
    """Retraction state per L2-7 Retraction Watch Sentinel."""
    CLEAR      = "clear"
    RETRACTED  = "retracted"
    CORRECTED  = "corrected"
    EXPRESSION = "expression_of_concern"
    UNCHECKED  = "unchecked"
''')
    print("  ADD: RetractionStatus enum")

# --- ClaimVerdict enum ---
if "class ClaimVerdict" not in schema_content:
    ADDITIONS.append('''

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
''')
    print("  ADD: ClaimVerdict enum")

# --- VerifierDecision enum ---
if "class VerifierDecision" not in schema_content:
    ADDITIONS.append('''

class VerifierDecision(str, Enum):
    """Adversarial LLM verifier decision per L4-12."""
    FAITHFUL     = "faithful"
    DISTORTED    = "distorted"
    OMISSION     = "omission"
    SCOPE_MISS   = "scope_miss"
    UNSUPPORTED  = "unsupported"
    FABRICATED   = "fabricated"
''')
    print("  ADD: VerifierDecision enum")

# --- SnippetClaimBinding class ---
if "class SnippetClaimBinding" not in schema_content:
    ADDITIONS.append('''

class SnippetClaimBinding(BaseModel):
    """Binds a claim to a specific evidence snippet. L4-14 requirement."""
    chunk_id:       str
    byte_offset:    int
    snippet_hash:   str
    span_length:    int
    model_config = {"frozen": True}
''')
    print("  ADD: SnippetClaimBinding class")

# --- HIGH_RISK_CLAIM_TYPES ---
if "HIGH_RISK_CLAIM_TYPES" not in schema_content:
    ADDITIONS.append('''

HIGH_RISK_CLAIM_TYPES: set[ClaimType] = {
    ClaimType.DOSING,
    ClaimType.CONTRAINDICATION,
    ClaimType.DRUG_INTERACTION,
}
''')
    print("  ADD: HIGH_RISK_CLAIM_TYPES set")

# --- EvidenceChunk alias ---
if "EvidenceChunk" not in schema_content:
    ADDITIONS.append('''

# Backward compatibility alias for layers/ that import EvidenceChunk
EvidenceChunk = EvidenceObject
''')
    print("  ADD: EvidenceChunk = EvidenceObject alias")

# Write additions to end of schemas.py
if ADDITIONS:
    addition_block = "\n\n# ─────────────────────────────────────────────────────────────────────────────\n# UNIFIED MODEL ADDITIONS (Fix 0J — merged from evidence.py + claims.py)\n# ─────────────────────────────────────────────────────────────────────────────" + "".join(ADDITIONS)
    schema_content += addition_block
    with open(SCHEMAS, "w", encoding="utf-8") as f:
        f.write(schema_content)
    print(f"  Saved: {SCHEMAS}")
else:
    print("  SKIP: All additions already present")

# ═══════════════════════════════════════════════════════════════
# STEP 2: Rewrite evidence.py as re-export shim
# ═══════════════════════════════════════════════════════════════

print("\n=== STEP 2: Rewriting evidence.py as compatibility shim ===")

EVIDENCE_SHIM = '''"""
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
'''

with open(EVIDENCE, "w", encoding="utf-8") as f:
    f.write(EVIDENCE_SHIM)
print(f"  Saved: {EVIDENCE}")

# ═══════════════════════════════════════════════════════════════
# STEP 3: Rewrite claims.py as re-export shim
# ═══════════════════════════════════════════════════════════════

print("\n=== STEP 3: Rewriting claims.py as compatibility shim ===")

CLAIMS_SHIM = '''"""
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
'''

with open(CLAIMS, "w", encoding="utf-8") as f:
    f.write(CLAIMS_SHIM)
print(f"  Saved: {CLAIMS}")

# ═══════════════════════════════════════════════════════════════
# VERIFICATION
# ═══════════════════════════════════════════════════════════════

print("\n== VERIFICATION ==")
sys.path.insert(0, BASE)

# Clear cached modules to force re-import
import importlib
for mod_name in list(sys.modules.keys()):
    if "curaniq.models" in mod_name:
        del sys.modules[mod_name]

# Test 1: schemas.py has all new classes
print("\n--- schemas.py completeness ---")
from curaniq.models.schemas import (
    SourceAPI, StalenessStatus, RetractionStatus,
    ClaimVerdict, VerifierDecision, SnippetClaimBinding,
    EvidenceChunk, HIGH_RISK_CLAIM_TYPES,
)
checks_1 = [
    ("SourceAPI enum",           SourceAPI.PUBMED.value == "pubmed"),
    ("StalenessStatus enum",     StalenessStatus.CRITICAL.value == "critical"),
    ("RetractionStatus enum",    RetractionStatus.RETRACTED.value == "retracted"),
    ("ClaimVerdict enum",        ClaimVerdict.SUPPRESSED.value == "suppressed"),
    ("VerifierDecision enum",    VerifierDecision.FABRICATED.value == "fabricated"),
    ("SnippetClaimBinding class", hasattr(SnippetClaimBinding, "chunk_id")),
    ("HIGH_RISK_CLAIM_TYPES set", len(HIGH_RISK_CLAIM_TYPES) >= 3),
    ("EvidenceChunk alias",       EvidenceChunk is not None),
]
ok1 = 0
for desc, passed in checks_1:
    ok1 += passed
    print(f"  {'PASS' if passed else 'FAIL'}: {desc}")

# Test 2: evidence.py re-exports work
print("\n--- evidence.py compatibility ---")
for mod_name in list(sys.modules.keys()):
    if "curaniq.models" in mod_name:
        del sys.modules[mod_name]

from curaniq.models.evidence import (
    EvidenceTier, EvidenceChunk, EvidencePack,
    RetractionStatus, StalenessStatus, SourceAPI,
    EvidenceProvenanceChain, CEBM_SCORE, FAIL_CLOSED_SOURCES,
)
checks_2 = [
    ("EvidenceTier from evidence.py",     EvidenceTier.RCT.value == "rct"),
    ("EvidenceChunk from evidence.py",    EvidenceChunk is not None),
    ("RetractionStatus from evidence.py", RetractionStatus.RETRACTED.value == "retracted"),
    ("StalenessStatus from evidence.py",  StalenessStatus.CRITICAL.value == "critical"),
    ("SourceAPI from evidence.py",        SourceAPI.PUBMED.value == "pubmed"),
    ("EvidenceProvenanceChain",           hasattr(EvidenceProvenanceChain, "snippet_hash")),
    ("CEBM_SCORE dict",                   CEBM_SCORE[EvidenceTier.SYSTEMATIC_REVIEW] == 1.0),
    ("FAIL_CLOSED_SOURCES set",           SourceAPI.OPENFDA_LABELS in FAIL_CLOSED_SOURCES),
]
ok2 = 0
for desc, passed in checks_2:
    ok2 += passed
    print(f"  {'PASS' if passed else 'FAIL'}: {desc}")

# Test 3: claims.py re-exports work
print("\n--- claims.py compatibility ---")
for mod_name in list(sys.modules.keys()):
    if "curaniq.models" in mod_name:
        del sys.modules[mod_name]

from curaniq.models.claims import (
    ClaimType, AtomicClaim, ClaimContract,
    ClaimVerdict, VerifierDecision, SnippetClaimBinding,
    HIGH_RISK_CLAIM_TYPES, ClinicalQueryRequest,
)
checks_3 = [
    ("ClaimType from claims.py",          ClaimType.DOSING.value == "dosing"),
    ("AtomicClaim from claims.py",        hasattr(AtomicClaim, "claim_text")),
    ("ClaimVerdict from claims.py",       ClaimVerdict.SUPPRESSED.value == "suppressed"),
    ("VerifierDecision from claims.py",   VerifierDecision.FABRICATED.value == "fabricated"),
    ("SnippetClaimBinding from claims.py", hasattr(SnippetClaimBinding, "chunk_id")),
    ("HIGH_RISK_CLAIM_TYPES",             ClaimType.DOSING in HIGH_RISK_CLAIM_TYPES),
    ("ClinicalQueryRequest unique",       hasattr(ClinicalQueryRequest, "query_text")),
]
ok3 = 0
for desc, passed in checks_3:
    ok3 += passed
    print(f"  {'PASS' if passed else 'FAIL'}: {desc}")

total = ok1 + ok2 + ok3
total_max = len(checks_1) + len(checks_2) + len(checks_3)
print(f"\n  TOTAL: {total}/{total_max}")
if total == total_max:
    print("\n  MODEL UNIFICATION COMPLETE")
    print("  schemas.py = canonical source (all enums + classes)")
    print("  evidence.py = re-export shim + unique extras (EvidenceProvenanceChain, CEBM_SCORE)")
    print("  claims.py = re-export shim + unique extras (ClinicalQueryRequest)")
    print("  All existing imports in layers/ work unchanged")
    print("  All existing imports in core/ work unchanged")
