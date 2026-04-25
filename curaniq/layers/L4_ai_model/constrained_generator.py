"""
CURANIQ — Medical Evidence Operating System
Layer 4: AI Model Layer

L4-2  Constrained Generator (evidence-locked LLM structured output)
L4-4  Citation Verifier (post-generation claim → evidence mapping)
"""
from __future__ import annotations
import hashlib, json, logging, re, uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from curaniq.models.evidence import EvidenceChunk, EvidencePack, RetractionStatus, StalenessStatus
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# L4-2: CONSTRAINED GENERATOR
# Architecture: 'LLM produces structured schema ONLY. No free-form narration
# until claims verified. Evidence IDs must match pack. Cite-or-Suppress enforced.'
# ─────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT_TEMPLATE = """You are CURANIQ — a Medical Evidence Operating System.

ABSOLUTE RULES — violation causes immediate output rejection:
1. Every clinical claim MUST cite evidence using chunk_id from the EVIDENCE PACK below.
2. If you cannot cite a claim with a chunk_id from the pack, you MUST suppress it (set suppressed=true).
3. Do NOT invent chunk_ids. Do NOT cite evidence not in the pack.
4. Do NOT make clinical recommendations without supporting evidence in the pack.
5. Numeric values (doses, lab thresholds, durations) MUST match their cited source exactly.
6. If evidence is conflicting, cite both and state the conflict explicitly.
7. NEVER output absolute statements of certainty. Use "evidence suggests", "guidelines recommend", "based on [source]".
8. If the question cannot be answered with the provided evidence, say so explicitly.

EVIDENCE PACK (closed set — only these chunk_ids are valid citations):
{evidence_pack_json}

QUERY: {query}

PATIENT CONTEXT (if provided):
{patient_context}

Respond ONLY with valid JSON matching this schema:
{{
  "response_id": "<uuid>",
  "mode": "<point_of_care|literature_review|living_dossier|decision_session|patient_safe>",
  "triage_flag": "<none|urgent|emergency>",
  "claims": [
    {{
      "claim_id": "<uuid>",
      "claim_text": "<the clinical statement>",
      "chunk_ids": ["<id1>", "<id2>"],
      "certainty": "<high|moderate|low|very_low>",
      "suppressed": false,
      "suppression_reason": null,
      "numeric_value": null,
      "numeric_unit": null,
      "numeric_source_chunk_id": null
    }}
  ],
  "safety_flags": ["<flag1>", "<flag2>"],
  "evidence_gaps": ["<what we don't know>"],
  "staleness_display": "<PubMed: 2h ago | openFDA: 45m ago>",
  "confidence_overall": 0.0,
  "jurisdiction": "<uk|us|uz|eu|intl>"
}}"""


@dataclass
class GeneratedClaim:
    claim_id:       str
    claim_text:     str
    chunk_ids:      list[str]
    certainty:      str
    suppressed:     bool
    suppression_reason: Optional[str]
    numeric_value:  Optional[float]
    numeric_unit:   Optional[str]
    numeric_source_chunk_id: Optional[str]


@dataclass
class GeneratorOutput:
    response_id:        str
    mode:               str
    triage_flag:        str
    claims:             list[GeneratedClaim]
    safety_flags:       list[str]
    evidence_gaps:      list[str]
    staleness_display:  str
    confidence_overall: float
    jurisdiction:       str
    raw_json:           dict
    generation_time_ms: float = 0.0
    llm_model_used:     str = ""


class ConstrainedGenerator:
    """
    L4-2: Evidence-locked constrained LLM generator.

    The LLM operates inside a closed evidence pack — it CANNOT cite evidence
    outside the pack. All output is structured JSON with claim-level citations.
    Free-form narration is assembled AFTER claim verification by L4-4.

    Production: uses Anthropic Claude Sonnet as primary, GPT-4o + Gemini Flash
    as fallbacks (L6-3 Multi-LLM Router handles selection).

    Current: mock structured output for testing pipeline integrity.
    """

    def __init__(self, llm_client: Optional[Any] = None) -> None:
        self._llm = llm_client   # Injected at startup — None = mock mode

    def generate(
        self,
        query: str,
        evidence_pack: EvidencePack,
        patient_context: Optional[dict] = None,
        mode: str = "point_of_care",
        jurisdiction: str = "intl",
    ) -> GeneratorOutput:
        """
        Generate evidence-locked structured output.
        Returns GeneratorOutput with claim-level citations.
        """
        t_start = datetime.now(timezone.utc)

        # Build evidence pack JSON for prompt
        pack_json = self._build_pack_json(evidence_pack)
        patient_str = json.dumps(patient_context or {}, ensure_ascii=False)

        prompt = SYSTEM_PROMPT_TEMPLATE.format(
            evidence_pack_json=json.dumps(pack_json, ensure_ascii=False, indent=2),
            query=query,
            patient_context=patient_str,
        )

        if self._llm is None:
            # Mock mode — returns structured skeleton for pipeline testing
            raw = self._mock_output(query, evidence_pack, mode, jurisdiction)
        else:
            raw = self._call_llm(prompt)

        t_end = datetime.now(timezone.utc)
        gen_ms = (t_end - t_start).total_seconds() * 1000

        return self._parse_output(raw, gen_ms)

    def _build_pack_json(self, pack: EvidencePack) -> list[dict]:
        """Serialize evidence pack for prompt injection."""
        items = []
        for chunk in pack.get_citable_chunks()[:20]:  # Limit to 20 chunks per prompt
            items.append({
                "chunk_id": chunk.chunk_id,
                "source": chunk.provenance.source_api.value,
                "tier": chunk.evidence_tier.value,
                "jurisdiction": chunk.provenance.jurisdiction.value,
                "freshness": chunk.staleness_status.value,
                "doi": chunk.provenance.source_doi,
                "pub_date": chunk.provenance.publication_date.isoformat()
                    if chunk.provenance.publication_date else None,
                "content_preview": chunk.content[:600],
            })
        return items

    def _call_llm(self, prompt: str) -> dict:
        """Call the real LLM (injected at runtime)."""
        try:
            response = self._llm.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text
            text_clean = re.sub(r'^```json\s*|```$', '', text.strip(), flags=re.MULTILINE)
            return json.loads(text_clean)
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            return self._error_output(str(e))

    def _mock_output(
        self, query: str, pack: EvidencePack, mode: str, jurisdiction: str
    ) -> dict:
        """Mock structured output for pipeline testing."""
        citable = pack.get_citable_chunks()
        claims = []
        if citable:
            claims.append({
                "claim_id": str(uuid.uuid4()),
                "claim_text": f"[Mock] Based on retrieved evidence: {query[:100]}",
                "chunk_ids": [citable[0].chunk_id],
                "certainty": "moderate",
                "suppressed": False,
                "suppression_reason": None,
                "numeric_value": None,
                "numeric_unit": None,
                "numeric_source_chunk_id": None,
            })
        return {
            "response_id": str(uuid.uuid4()),
            "mode": mode,
            "triage_flag": "none",
            "claims": claims,
            "safety_flags": [],
            "evidence_gaps": ["[Mock mode — real LLM not connected]"],
            "staleness_display": pack.staleness_display(),
            "confidence_overall": 0.5,
            "jurisdiction": jurisdiction,
        }

    def _error_output(self, error: str) -> dict:
        return {
            "response_id": str(uuid.uuid4()),
            "mode": "error",
            "triage_flag": "none",
            "claims": [{
                "claim_id": str(uuid.uuid4()),
                "claim_text": "LLM generation failed — evidence pack available but generation error occurred.",
                "chunk_ids": [],
                "certainty": "very_low",
                "suppressed": True,
                "suppression_reason": f"LLM error: {error[:200]}",
                "numeric_value": None,
                "numeric_unit": None,
                "numeric_source_chunk_id": None,
            }],
            "safety_flags": ["GENERATION_ERROR"],
            "evidence_gaps": [f"Generation failed: {error}"],
            "staleness_display": "",
            "confidence_overall": 0.0,
            "jurisdiction": "intl",
        }

    def _parse_output(self, raw: dict, gen_ms: float) -> GeneratorOutput:
        """Parse raw LLM JSON into GeneratorOutput dataclass."""
        claims = []
        for c in raw.get("claims", []):
            claims.append(GeneratedClaim(
                claim_id=c.get("claim_id", str(uuid.uuid4())),
                claim_text=c.get("claim_text", ""),
                chunk_ids=c.get("chunk_ids", []),
                certainty=c.get("certainty", "low"),
                suppressed=c.get("suppressed", False),
                suppression_reason=c.get("suppression_reason"),
                numeric_value=c.get("numeric_value"),
                numeric_unit=c.get("numeric_unit"),
                numeric_source_chunk_id=c.get("numeric_source_chunk_id"),
            ))
        return GeneratorOutput(
            response_id=raw.get("response_id", str(uuid.uuid4())),
            mode=raw.get("mode", "point_of_care"),
            triage_flag=raw.get("triage_flag", "none"),
            claims=claims,
            safety_flags=raw.get("safety_flags", []),
            evidence_gaps=raw.get("evidence_gaps", []),
            staleness_display=raw.get("staleness_display", ""),
            confidence_overall=float(raw.get("confidence_overall", 0.0)),
            jurisdiction=raw.get("jurisdiction", "intl"),
            raw_json=raw,
            generation_time_ms=gen_ms,
        )


# ─────────────────────────────────────────────────────────────────────────────
# L4-4: CITATION VERIFIER
# Architecture: 'Post-generation: checks claims map to actual text in chunk.
# Catches chunk_id correct but text misrepresents source (subtle hallucination).'
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CitationVerificationResult:
    claim_id:           str
    claim_text:         str
    verified:           bool
    verification_type:  str    # "exact_match"|"semantic_match"|"partial_match"|"no_match"
    matched_text:       Optional[str]
    mismatch_details:   Optional[str]
    numeric_verified:   bool = True   # Numeric values match source exactly


class CitationVerifier:
    """
    L4-4: Post-generation citation verifier.

    Checks that each claim's cited chunk_id actually supports the claim text.
    Catches subtle hallucinations where the chunk_id is valid but the claim
    misrepresents or over-states what the evidence actually says.

    Verification levels:
    1. Chunk_id exists in pack (L4-14 handles this — pre-check)
    2. Key claim terms appear in the cited chunk (semantic overlap)
    3. Numeric values in the claim match the cited source exactly (critical)
    4. Claim does not contradict cited source (contradiction detection)
    """

    def verify(
        self,
        output: GeneratorOutput,
        chunk_registry: dict[str, EvidenceChunk],
    ) -> list[CitationVerificationResult]:
        """Verify all claims in a GeneratorOutput against their cited chunks."""
        results = []
        for claim in output.claims:
            if claim.suppressed:
                continue
            result = self._verify_claim(claim, chunk_registry)
            results.append(result)
        return results

    def _verify_claim(
        self,
        claim: GeneratedClaim,
        chunk_registry: dict[str, EvidenceChunk],
    ) -> CitationVerificationResult:
        claim_lower = claim.claim_text.lower()
        claim_terms = set(re.findall(r'\b[a-z]{4,}\b', claim_lower))

        # Collect content of all cited chunks
        cited_content = ""
        for chunk_id in claim.chunk_ids:
            chunk = chunk_registry.get(chunk_id)
            if chunk:
                cited_content += " " + chunk.content.lower()

        if not cited_content.strip():
            return CitationVerificationResult(
                claim_id=claim.claim_id,
                claim_text=claim.claim_text,
                verified=False,
                verification_type="no_match",
                matched_text=None,
                mismatch_details="No cited chunks found in registry",
            )

        # Semantic overlap check
        if claim_terms:
            overlap = sum(1 for t in claim_terms if t in cited_content)
            overlap_ratio = overlap / len(claim_terms)
        else:
            overlap_ratio = 0.0

        # Numeric verification — most critical
        numeric_verified = True
        numeric_mismatch = None
        if claim.numeric_value is not None and claim.numeric_source_chunk_id:
            source_chunk = chunk_registry.get(claim.numeric_source_chunk_id)
            if source_chunk:
                value_str = str(claim.numeric_value)
                if value_str not in source_chunk.content:
                    numeric_verified = False
                    numeric_mismatch = (
                        f"Numeric value {claim.numeric_value} {claim.numeric_unit or ''} "
                        f"not found verbatim in cited chunk {claim.numeric_source_chunk_id}. "
                        "Potential hallucinated dose/threshold."
                    )

        # Contradiction check — claim asserts X but source says NOT X
        contradiction_detected = False
        if claim_terms:
            negation_patterns = [
                re.compile(r'\b(not|no|never|avoid|contraindicated|do not)\s+' + re.escape(t) + r'\b', re.I)
                for t in list(claim_terms)[:5]
            ]
            for pat in negation_patterns:
                if pat.search(cited_content) and not pat.search(claim_lower):
                    contradiction_detected = True
                    break

        # Determine result
        if contradiction_detected:
            return CitationVerificationResult(
                claim_id=claim.claim_id,
                claim_text=claim.claim_text,
                verified=False,
                verification_type="no_match",
                matched_text=None,
                mismatch_details="CONTRADICTION: claim may contradict its cited source.",
                numeric_verified=numeric_verified,
            )

        if not numeric_verified:
            return CitationVerificationResult(
                claim_id=claim.claim_id,
                claim_text=claim.claim_text,
                verified=False,
                verification_type="partial_match",
                matched_text=None,
                mismatch_details=numeric_mismatch,
                numeric_verified=False,
            )

        if overlap_ratio >= 0.6:
            vtype = "semantic_match" if overlap_ratio < 0.9 else "exact_match"
            return CitationVerificationResult(
                claim_id=claim.claim_id,
                claim_text=claim.claim_text,
                verified=True,
                verification_type=vtype,
                matched_text=f"Overlap ratio: {overlap_ratio:.0%}",
                mismatch_details=None,
                numeric_verified=numeric_verified,
            )
        elif overlap_ratio >= 0.3:
            return CitationVerificationResult(
                claim_id=claim.claim_id,
                claim_text=claim.claim_text,
                verified=False,
                verification_type="partial_match",
                matched_text=None,
                mismatch_details=f"Low term overlap ({overlap_ratio:.0%}) — claim may exceed evidence scope.",
                numeric_verified=numeric_verified,
            )
        else:
            return CitationVerificationResult(
                claim_id=claim.claim_id,
                claim_text=claim.claim_text,
                verified=False,
                verification_type="no_match",
                matched_text=None,
                mismatch_details=f"Insufficient evidence overlap ({overlap_ratio:.0%}) — claim cannot be verified.",
                numeric_verified=numeric_verified,
            )

    def filter_verified_claims(
        self,
        output: GeneratorOutput,
        verification_results: list[CitationVerificationResult],
    ) -> GeneratorOutput:
        """Remove unverified claims from output (Cite-or-Suppress enforcement)."""
        unverified_ids = {r.claim_id for r in verification_results if not r.verified}
        for claim in output.claims:
            if claim.claim_id in unverified_ids:
                claim.suppressed = True
                matched = next((r for r in verification_results if r.claim_id == claim.claim_id), None)
                claim.suppression_reason = matched.mismatch_details if matched else "Citation verification failed"
        return output


# Backward-compatible alias expected by curaniq.core.pipeline
ConstrainedLLMGenerator = ConstrainedGenerator
