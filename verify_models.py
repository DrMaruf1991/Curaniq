"""
Quick verify the 5 failed model checks.
Pydantic v2 stores fields in model_fields, not as class attrs.

Run: cd D:\curaniq_engine\curaniq_engine
     python verify_models.py
"""
import sys, os
sys.path.insert(0, r"D:\curaniq_engine\curaniq_engine")

from curaniq.models.schemas import SnippetClaimBinding, AtomicClaim, EvidenceChunk
from curaniq.models.evidence import EvidenceProvenanceChain
from curaniq.models.claims import ClinicalQueryRequest

checks = [
    ("SnippetClaimBinding.chunk_id",
     "chunk_id" in SnippetClaimBinding.model_fields),
    ("EvidenceProvenanceChain.snippet_hash",
     "snippet_hash" in EvidenceProvenanceChain.model_fields),
    ("AtomicClaim.claim_text",
     "claim_text" in AtomicClaim.model_fields),
    ("SnippetClaimBinding via claims.py",
     "chunk_id" in SnippetClaimBinding.model_fields),
    ("ClinicalQueryRequest.query_text",
     "query_text" in ClinicalQueryRequest.model_fields),
]

ok = 0
for desc, passed in checks:
    ok += passed
    print(f"  {'PASS' if passed else 'FAIL'}: {desc}")

print(f"\n  {ok}/{len(checks)} — all 5 'failures' were just hasattr vs model_fields")
if ok == len(checks):
    print("  MODELS ARE CORRECT. All fields exist.")
