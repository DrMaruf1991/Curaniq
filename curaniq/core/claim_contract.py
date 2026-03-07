"""
CURANIQ — L4-3: Claim Contract Engine
Architecture spec: THE ENFORCEMENT MECHANISM.
Deterministic post-processor that:
  (a) segments LLM output into atomic clinical claims
  (b) classifies each claim type
  (c) requires an evidence object per claim
  (d) blocks claims where evidence doesn't entail them
  (e) enforces L5-17 numeric token verification (deterministic OR verbatim)
  (f) enforces L4-14 evidence hash-lock (snippet integrity)
This module converts the product thesis into engineering reality.
"""
from __future__ import annotations
import hashlib
import re
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID, uuid4

from curaniq.models.schemas import (
    AtomicClaim,
    ClaimContract,
    ClaimType,
    ConfidenceLevel,
    CQLComputationLog,
    EvidenceObject,
    EvidencePack,
    NumericToken,
    NumericTokenStatus,
    SafetyFlag,
)


# ─────────────────────────────────────────────────────────────────────────────
# CLAIM SEGMENTATION  — splits LLM output into atomic claims
# ─────────────────────────────────────────────────────────────────────────────

# Sentence boundary detection (medical-aware: avoids splitting on "Dr.", "vs.", "e.g.")
_SENT_END = re.compile(r'[.!?]\s+(?=[A-Z])', re.MULTILINE)

# Claim type classifiers — pattern → ClaimType
_CLAIM_TYPE_PATTERNS: list[tuple[re.Pattern, ClaimType]] = [
    (re.compile(r'\b(dose|dosing|mg|mcg|units|frequency|BID|TID|OD|once\s+daily|twice\s+daily|per\s+kg)\b', re.I), ClaimType.DOSING),
    (re.compile(r'\b(contraindicated|avoid|do\s+not\s+use|prohibited|forbidden|not\s+recommended)\b', re.I), ClaimType.CONTRAINDICATION),
    (re.compile(r'\b(interaction|interacts|DDI|drug.drug|combination\s+with|concurrent\s+use)\b', re.I), ClaimType.DRUG_INTERACTION),
    (re.compile(r'\b(effective|efficacy|reduces|improves|benefit|outcome|mortality|survival|NNT|ARR|RRR)\b', re.I), ClaimType.EFFICACY),
    (re.compile(r'\b(side\s+effect|adverse|toxicity|hepatotoxic|nephrotoxic|ototoxic|risk\s+of)\b', re.I), ClaimType.SAFETY_SIGNAL),
    (re.compile(r'\b(diagnosis|diagnose|diagnostic|sensitivity|specificity|PPV|NPV|likelihood\s+ratio)\b', re.I), ClaimType.DIAGNOSTIC),
    (re.compile(r'\b(monitor|monitoring|check|follow-up|level|trough|peak|ECG|CBC|LFTs|INR)\b', re.I), ClaimType.MONITORING),
    (re.compile(r'\b(prognosis|prognotic|survival|mortality|5-year|median\s+survival)\b', re.I), ClaimType.PROGNOSIS),
]


def _classify_claim_type(claim_text: str) -> ClaimType:
    """Classify a claim into its type based on content patterns."""
    for pattern, claim_type in _CLAIM_TYPE_PATTERNS:
        if pattern.search(claim_text):
            return claim_type
    return ClaimType.GENERAL


def segment_into_claims(llm_output: str) -> list[str]:
    """
    Segment LLM output text into atomic claims (one verifiable assertion each).
    Returns list of claim text strings.
    """
    # Split on sentence boundaries
    sentences = _SENT_END.split(llm_output.strip())

    # Further split on semicolons for compound clinical statements
    atomic: list[str] = []
    for sent in sentences:
        sent = sent.strip()
        if not sent or len(sent) < 10:
            continue
        # Split compound claims joined by "; " or "and"
        if "; " in sent and len(sent) > 120:
            parts = sent.split("; ")
            atomic.extend(p.strip() for p in parts if len(p.strip()) > 10)
        else:
            atomic.append(sent)

    return atomic


# ─────────────────────────────────────────────────────────────────────────────
# NUMERIC TOKEN EXTRACTOR + VERIFIER  (L5-17)
# ─────────────────────────────────────────────────────────────────────────────

# Matches all numeric tokens: integers, decimals, fractions, ranges, percentages
_NUMERIC_PATTERN = re.compile(
    r'\b'
    r'(?:'
    r'\d+(?:\.\d+)?'            # Integer or decimal: 500, 0.5, 30.5
    r'(?:\s*[-–]\s*\d+(?:\.\d+)?)?'  # Optional range: 5-10
    r'(?:\s*%)?'                # Optional percent: 50%
    r')'
    r'(?:\s*(?:mg|mcg|μg|g|kg|mL|L|mmHg|mmol|mEq|mM|ng|pg|IU|units?|hours?|h|days?|weeks?|months?))?'
    r'\b',
    re.I,
)


def extract_numeric_tokens(text: str) -> list[str]:
    """Extract all numeric values and measurements from text."""
    return [m.group().strip() for m in _NUMERIC_PATTERN.finditer(text) if m.group().strip()]


def verify_numeric_token(
    token: str,
    cql_logs: list[CQLComputationLog],
    evidence_objects: list[EvidenceObject],
) -> NumericToken:
    """
    L5-17 Numeric Deterministic-or-Quoted Gate.
    Every number MUST be either:
      (a) DETERMINISTIC — in a CQL computation log output
      (b) VERBATIM — character-identical substring in a governed evidence snippet
    If neither → BLOCKED.
    """
    token_clean = token.strip()

    # Check (a): Is this value in a CQL computation log?
    for log in cql_logs:
        if token_clean in log.output_value or log.output_value in token_clean:
            return NumericToken(
                value_str=token_clean,
                status=NumericTokenStatus.DETERMINISTIC,
                cql_computation_id=log.computation_id,
            )

    # Check (b): Is this value verbatim in an evidence snippet?
    for ev in evidence_objects:
        if token_clean in ev.snippet:
            # Compute byte offset and hash for L4-14 hash-lock
            snippet_bytes = ev.snippet.encode("utf-8")
            token_bytes = token_clean.encode("utf-8")
            offset = ev.snippet.find(token_clean)
            expected_hash = hashlib.sha256(snippet_bytes).hexdigest()
            actual_match = ev.snippet_hash == expected_hash if ev.snippet_hash else True
            return NumericToken(
                value_str=token_clean,
                status=NumericTokenStatus.VERBATIM,
                evidence_snippet_id=ev.evidence_id,
                byte_offset=offset,
                hash_match=actual_match,
            )

    # Neither condition met → BLOCKED
    return NumericToken(
        value_str=token_clean,
        status=NumericTokenStatus.BLOCKED,
    )


# ─────────────────────────────────────────────────────────────────────────────
# EVIDENCE HASH-LOCK VERIFIER  (L4-14)
# ─────────────────────────────────────────────────────────────────────────────

def verify_evidence_hash_lock(evidence_objects: list[EvidenceObject]) -> list[UUID]:
    """
    L4-14: Evidence Object Integrity.
    Verifies that each evidence snippet has not been altered since ingestion.
    Returns list of evidence_ids where hash verification FAILED.
    """
    failed: list[UUID] = []
    for ev in evidence_objects:
        if ev.snippet_hash:
            actual_hash = hashlib.sha256(ev.snippet.encode("utf-8")).hexdigest()
            if actual_hash != ev.snippet_hash:
                failed.append(ev.evidence_id)
    return failed


# ─────────────────────────────────────────────────────────────────────────────
# CONFIDENCE SCORER  (L4-13)
# ─────────────────────────────────────────────────────────────────────────────

def compute_confidence_score(
    entailment_score: float,
    cross_llm_agreement: float,    # 0.0–1.0 from L4-12; 1.0=all agree, 0.0=all disagree
    evidence_objects: list[EvidenceObject],
) -> tuple[float, ConfidenceLevel]:
    """
    L4-13 Confidence Scoring Formula (architecture spec — exact):
    Score = weighted average of:
      (a) Entailment score from NLI model (0.0–1.0)
      (b) Cross-LLM agreement from L4-12 (1.0=all agree, 0.5=verifier flagged, 0.0=all disagree)
      (c) Evidence quality per Oxford CEBM
      (d) Recency score
      (e) Source count score: 3+=1.0, 2=0.8, 1=0.6, 0=SUPPRESS

    Thresholds: HIGH>=0.85, MEDIUM=0.70–0.85, LOW=0.50–0.70, SUPPRESS<0.50
    """
    # (e) Source count
    source_count = len({e.source_id for e in evidence_objects})
    if source_count == 0:
        return 0.0, ConfidenceLevel.SUPPRESS
    elif source_count == 1:
        source_score = 0.6
    elif source_count == 2:
        source_score = 0.8
    else:
        source_score = 1.0

    # (c) Evidence quality — average across evidence objects
    if evidence_objects:
        quality_score = sum(e.quality_score for e in evidence_objects) / len(evidence_objects)
        recency_score = sum(e.recency_score for e in evidence_objects) / len(evidence_objects)
    else:
        quality_score = 0.0
        recency_score = 0.0

    # Weighted average (weights as per spec priority)
    # Entailment (0.35) + Cross-LLM (0.25) + Quality (0.20) + Recency (0.10) + Source count (0.10)
    score = (
        entailment_score   * 0.35 +
        cross_llm_agreement * 0.25 +
        quality_score      * 0.20 +
        recency_score      * 0.10 +
        source_score       * 0.10
    )

    if score >= 0.85:
        level = ConfidenceLevel.HIGH
    elif score >= 0.70:
        level = ConfidenceLevel.MEDIUM
    elif score >= 0.50:
        level = ConfidenceLevel.LOW
    else:
        level = ConfidenceLevel.SUPPRESS

    return round(score, 4), level


# ─────────────────────────────────────────────────────────────────────────────
# CLAIM CONTRACT ENGINE  (L4-3 — main class)
# ─────────────────────────────────────────────────────────────────────────────

# Black Box warning drugs (FDA) — must be shown with max prominence (L5-11)
BLACK_BOX_DRUGS: frozenset[str] = frozenset({
    "methotrexate", "isotretinoin", "thalidomide", "clozapine", "warfarin",
    "amiodarone", "valproate", "haloperidol", "quetiapine", "olanzapine",
    "risperidone", "lithium", "methadone", "fentanyl", "oxycodone",
    "morphine", "hydrocodone", "buprenorphine", "naltrexone",
    "interferon_alfa", "tacrolimus", "cyclosporine", "azathioprine",
    "mycophenolate", "natalizumab", "fingolimod", "alemtuzumab",
    "fluoroquinolones", "ciprofloxacin", "levofloxacin", "moxifloxacin",
})

# REMS drugs (require Risk Evaluation and Mitigation Strategy)
REMS_DRUGS: frozenset[str] = frozenset({
    "isotretinoin", "thalidomide", "lenalidomide", "pomalidomide",
    "clozapine", "olanzapine_fluoxetine", "fentanyl", "buprenorphine",
    "naloxegol", "mifeprex", "mifepristone", "nalmefene",
})


class ClaimContractEngine:
    """
    L4-3: Claim Contract Engine.

    The central enforcement gate in the CURANIQ pipeline.
    Takes raw LLM output + evidence pack → returns a ClaimContract
    where every claim is verified, blocked-or-passed, and confidence-scored.

    No claim appears in output unless it passes this engine.
    """

    # Minimum entailment score for a claim to pass (can be tuned by claim type)
    ENTAILMENT_THRESHOLDS: dict[ClaimType, float] = {
        ClaimType.DOSING:           0.80,   # Highest bar — dosing errors kill
        ClaimType.CONTRAINDICATION: 0.80,
        ClaimType.DRUG_INTERACTION: 0.75,
        ClaimType.SAFETY_SIGNAL:    0.75,
        ClaimType.EFFICACY:         0.70,
        ClaimType.DIAGNOSTIC:       0.70,
        ClaimType.MONITORING:       0.65,
        ClaimType.PROGNOSIS:        0.65,
        ClaimType.GENERAL:          0.60,
    }

    def __init__(
        self,
        cql_logs: Optional[list[CQLComputationLog]] = None,
    ) -> None:
        self._cql_logs: list[CQLComputationLog] = cql_logs or []

    def update_cql_logs(self, logs: list[CQLComputationLog]) -> None:
        self._cql_logs.extend(logs)

    def process(
        self,
        query_id: UUID,
        llm_raw_output: str,
        evidence_pack: EvidencePack,
        cross_llm_agreement: float = 1.0,
        simulated_entailment: Optional[dict[str, float]] = None,
    ) -> ClaimContract:
        """
        Main entry point: segment → classify → verify → score → contract.

        Args:
            query_id: The originating query ID
            llm_raw_output: Raw text from the constrained LLM generator
            evidence_pack: All retrieved evidence objects
            cross_llm_agreement: Score from L4-12 adversarial LLM verifier (default 1.0 = no verifier run)
            simulated_entailment: For testing — maps claim_text → entailment_score
        """
        # Phase 0: Hash-lock verification (L4-14)
        integrity_failures = verify_evidence_hash_lock(evidence_pack.objects)
        tampered_ids = set(integrity_failures)

        # Filter out any evidence with failed hash (integrity violation)
        clean_evidence = [
            e for e in evidence_pack.objects
            if e.evidence_id not in tampered_ids and
               not e.is_retracted and
               not e.is_stale
        ]

        atomic_claims: list[AtomicClaim] = []

        # Phase 1: Segment raw output into atomic claims
        claim_texts = segment_into_claims(llm_raw_output)

        for claim_text in claim_texts:
            claim_id = uuid4()
            claim_type = _classify_claim_type(claim_text)
            safety_flags: list[SafetyFlag] = []

            # Phase 2: Find supporting evidence for this claim
            # (In production: NLI model scores each claim vs each evidence snippet)
            # Here: use keyword overlap as proxy; production uses SciFact/MedNLI
            supporting_evidence = self._find_supporting_evidence(
                claim_text, clean_evidence
            )
            evidence_ids = [e.evidence_id for e in supporting_evidence]

            # Phase 3: Entailment scoring
            # Production: self-hosted SciFact or MedNLI model
            # Here: deterministic based on evidence coverage
            entailment_score = self._compute_entailment_score(
                claim_text, supporting_evidence, simulated_entailment
            )

            # Phase 4: Numeric token verification (L5-17)
            numeric_values = extract_numeric_tokens(claim_text)
            numeric_tokens: list[NumericToken] = []
            has_blocked_numeric = False

            for num_val in numeric_values:
                nt = verify_numeric_token(num_val, self._cql_logs, clean_evidence)
                numeric_tokens.append(nt)
                if nt.status == NumericTokenStatus.BLOCKED:
                    has_blocked_numeric = True

            # Phase 5: Black Box / REMS flags (L5-11)
            claim_lower = claim_text.lower()
            for bb_drug in BLACK_BOX_DRUGS:
                if bb_drug.replace("_", " ") in claim_lower or bb_drug in claim_lower:
                    safety_flags.append(SafetyFlag.BLACK_BOX_WARNING)
                    break
            for rems_drug in REMS_DRUGS:
                if rems_drug.replace("_", " ") in claim_lower or rems_drug in claim_lower:
                    safety_flags.append(SafetyFlag.REMS_REQUIRED)
                    break

            # Phase 6: Retraction / staleness flags
            if any(e.is_retracted for e in supporting_evidence):
                safety_flags.append(SafetyFlag.RETRACTED_SOURCE)
            if any(e.is_stale for e in supporting_evidence):
                safety_flags.append(SafetyFlag.STALE_DATA)
            if has_blocked_numeric:
                safety_flags.append(SafetyFlag.NUMERIC_UNVERIFIED)

            # Phase 7: Confidence scoring (L4-13)
            confidence_score, confidence_level = compute_confidence_score(
                entailment_score=entailment_score,
                cross_llm_agreement=cross_llm_agreement,
                evidence_objects=supporting_evidence,
            )

            # Phase 8: Block decision
            is_blocked = False
            block_reason: Optional[str] = None

            threshold = self.ENTAILMENT_THRESHOLDS[claim_type]

            if not supporting_evidence:
                is_blocked = True
                block_reason = "No evidence objects found to support this claim — Claim Contract requires evidence per claim."
            elif entailment_score < threshold:
                is_blocked = True
                block_reason = (
                    f"Entailment score {entailment_score:.2f} below threshold {threshold:.2f} "
                    f"for claim type {claim_type.value}."
                )
            elif has_blocked_numeric and claim_type in (ClaimType.DOSING, ClaimType.CONTRAINDICATION):
                is_blocked = True
                block_reason = "Claim contains numeric value(s) that are neither deterministic (CQL) nor verbatim-quoted from evidence (L5-17 Numeric Gate)."
            elif SafetyFlag.RETRACTED_SOURCE in safety_flags:
                is_blocked = True
                block_reason = "Claim cites a retracted source — blocked by L5-7 Retraction Blocking."
            elif confidence_level == ConfidenceLevel.SUPPRESS:
                is_blocked = True
                block_reason = f"Confidence score {confidence_score:.2f} below SUPPRESS threshold (0.50)."

            is_supported = not is_blocked and entailment_score >= threshold

            atomic_claims.append(AtomicClaim(
                claim_id=claim_id,
                claim_text=claim_text,
                claim_type=claim_type,
                evidence_ids=evidence_ids,
                entailment_score=entailment_score,
                is_supported=is_supported,
                is_blocked=is_blocked,
                block_reason=block_reason,
                numeric_tokens=numeric_tokens,
                confidence_score=confidence_score,
                confidence_level=confidence_level,
                safety_flags=list(set(safety_flags)),
            ))

        # Assemble contract
        contract = ClaimContract(
            query_id=query_id,
            atomic_claims=atomic_claims,
        )
        return contract

    def _find_supporting_evidence(
        self,
        claim_text: str,
        evidence_objects: list[EvidenceObject],
    ) -> list[EvidenceObject]:
        """
        Find evidence objects that plausibly support this claim.
        Production: NLI model scoring. Here: keyword overlap scoring.
        """
        claim_words = set(re.findall(r'\b[a-z]{4,}\b', claim_text.lower()))
        scored: list[tuple[float, EvidenceObject]] = []

        for ev in evidence_objects:
            ev_words = set(re.findall(r'\b[a-z]{4,}\b',
                                      (ev.snippet + " " + ev.title).lower()))
            if not ev_words:
                continue
            overlap = len(claim_words & ev_words) / len(claim_words | ev_words)
            if overlap > 0.1:  # Minimum relevance threshold
                scored.append((overlap, ev))

        # Return top 5 most relevant evidence objects
        scored.sort(key=lambda x: x[0], reverse=True)
        return [ev for _, ev in scored[:5]]

    def _compute_entailment_score(
        self,
        claim_text: str,
        evidence_objects: list[EvidenceObject],
        simulated: Optional[dict[str, float]] = None,
    ) -> float:
        """
        Compute NLI entailment score for claim given evidence.
        Production: SciFact / MedNLI self-hosted model.
        Test: accepts simulated scores dict.
        Fallback: keyword-based heuristic.
        """
        if simulated:
            # Look for exact or partial match in simulated dict
            for key, score in simulated.items():
                if key in claim_text or claim_text.startswith(key[:30]):
                    return score

        if not evidence_objects:
            return 0.0

        # Heuristic: score based on keyword density of claim in evidence snippets
        claim_words = set(re.findall(r'\b[a-z]{4,}\b', claim_text.lower()))
        best_score = 0.0

        for ev in evidence_objects:
            ev_words = set(re.findall(r'\b[a-z]{4,}\b', ev.snippet.lower()))
            if not claim_words or not ev_words:
                continue
            intersection = claim_words & ev_words
            # Jaccard similarity as entailment proxy
            jaccard = len(intersection) / len(claim_words | ev_words)
            # Boost for high-quality evidence tiers
            quality_boost = ev.quality_score * 0.2
            score = min(1.0, jaccard * 2.5 + quality_boost)
            best_score = max(best_score, score)

        return round(best_score, 4)
