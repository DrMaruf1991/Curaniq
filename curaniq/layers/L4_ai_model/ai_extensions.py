"""
CURANIQ -- Layer 4: AI Model Layer
P2 Advanced Verification & Reasoning

L4-5   Self-Correction RAG Loop (Reflexion — iterative retrieval refinement)
L4-6   Multi-Agent Debate Protocol (structured LLM disagreement resolution)
L4-7   Adversarial Red Team Agent (proactive output attack testing)
L4-8   Abductive Reasoning Chain (hypothesis generation from incomplete data)
L4-10  Clinical Knowledge Graph Engine (drug-condition-evidence triples)

All logic modules. No hardcoded clinical data. LLM calls via L6-3 Multi-LLM Client.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


# =============================================================================
# L4-5: SELF-CORRECTION RAG LOOP (Reflexion)
# Source: Shinn et al. "Reflexion: Language Agents with Verbal Reinforcement
# Learning" NeurIPS 2023
# =============================================================================

@dataclass
class ReflexionStep:
    iteration: int
    query: str
    evidence_count: int
    claim_confidence: float
    critique: str
    action_taken: str  # "refine_query", "expand_sources", "accept", "refuse"


class SelfCorrectionRAGLoop:
    """
    L4-5: Iterative retrieval-generation-critique loop.

    Flow (max 3 iterations, fail-safe):
    1. Retrieve evidence → Generate claims → Verify claims (L4-3)
    2. If confidence < threshold: CRITIQUE (identify what's weak)
    3. Refine query based on critique → Re-retrieve → Re-generate
    4. If still below threshold after max iterations: add uncertainty disclosure

    NOT a free-form LLM loop. Each iteration is constrained:
    - Retrieval: via L4-1 Hybrid Retriever (deterministic ranking)
    - Generation: via L4-2 Constrained Generator (evidence-locked)
    - Verification: via L4-3 Claim Contract (NLI entailment)
    - Critique: structured template, not open-ended LLM
    """

    MAX_ITERATIONS = 3
    CONFIDENCE_THRESHOLD = 0.7

    def __init__(self):
        self._history: list[ReflexionStep] = []

    def should_iterate(self, claim_confidence: float, iteration: int) -> bool:
        """Check if another iteration would improve results."""
        if iteration >= self.MAX_ITERATIONS:
            return False
        if claim_confidence >= self.CONFIDENCE_THRESHOLD:
            return False
        return True

    def generate_critique(self, claims: list[dict], evidence_count: int,
                          iteration: int) -> dict:
        """Structured critique of current output quality."""
        issues = []

        # Check evidence coverage
        if evidence_count < 3:
            issues.append({
                "type": "insufficient_evidence",
                "detail": f"Only {evidence_count} evidence sources. Minimum 3 recommended.",
                "action": "expand_sources",
            })

        # Check claim-level weaknesses
        weak_claims = [c for c in claims if c.get("confidence", 0) < 0.5]
        if weak_claims:
            issues.append({
                "type": "weak_claims",
                "detail": f"{len(weak_claims)} claims below 0.5 confidence.",
                "action": "refine_query",
                "weak_claim_texts": [c.get("text", "")[:60] for c in weak_claims[:3]],
            })

        # Check for contradictions
        contradicting = [c for c in claims if c.get("citation_intent") == "contradicting"]
        if contradicting:
            issues.append({
                "type": "contradicting_evidence",
                "detail": f"{len(contradicting)} claims have contradicting evidence.",
                "action": "add_uncertainty_disclosure",
            })

        # Check for unsupported claims
        unsupported = [c for c in claims if not c.get("evidence_ids")]
        if unsupported:
            issues.append({
                "type": "unsupported_claims",
                "detail": f"{len(unsupported)} claims have no supporting evidence.",
                "action": "remove_or_flag",
            })

        suggested_action = "accept" if not issues else issues[0]["action"]

        step = ReflexionStep(
            iteration=iteration,
            query="",
            evidence_count=evidence_count,
            claim_confidence=sum(c.get("confidence", 0) for c in claims) / max(len(claims), 1),
            critique="; ".join(i["detail"] for i in issues) if issues else "No issues found",
            action_taken=suggested_action,
        )
        self._history.append(step)

        return {
            "issues": issues,
            "suggested_action": suggested_action,
            "iteration": iteration,
            "should_continue": self.should_iterate(step.claim_confidence, iteration),
        }


# =============================================================================
# L4-6: MULTI-AGENT DEBATE PROTOCOL
# Source: Du et al. "Improving Factuality of LLMs through Multi-Agent Debate" 2023
# =============================================================================

@dataclass
class DebateRound:
    round_number: int
    proposer: str       # LLM model name
    critic: str         # LLM model name
    proposal: str
    critique: str
    revised: str
    agreement_score: float  # 0-1


class MultiAgentDebateProtocol:
    """
    L4-6: Structured disagreement resolution between LLMs.

    Architecture: Claude generates → GPT-4o critiques → Claude revises.
    GPT-4o NEVER generates clinical content (only critiques).
    This is the "adversarial jury" pattern extended to multi-round debate.

    Protocol:
    1. Primary LLM generates initial answer with evidence
    2. Critic LLM identifies potential errors, unsupported claims, logical gaps
    3. Primary LLM revises based on critique (with evidence lock still active)
    4. If agreement score >= threshold: accept. Else: escalate to human review.

    Max 2 rounds (diminishing returns beyond that per Du et al. 2023).
    """

    MAX_ROUNDS = 2
    AGREEMENT_THRESHOLD = 0.8

    def __init__(self):
        self._debate_history: list[DebateRound] = []

    def structure_critique_prompt(self, original_output: str,
                                  evidence_summary: str) -> str:
        """Build structured critique prompt for the critic LLM."""
        return (
            "You are a medical accuracy reviewer. Your ONLY role is to identify "
            "errors, unsupported claims, and logical gaps in the following clinical "
            "output. You must NOT generate alternative clinical content.\n\n"
            "CLINICAL OUTPUT TO REVIEW:\n"
            f"{original_output}\n\n"
            "AVAILABLE EVIDENCE SUMMARY:\n"
            f"{evidence_summary}\n\n"
            "CRITIQUE FORMAT:\n"
            "1. List each potentially incorrect or unsupported claim\n"
            "2. For each, state why it may be wrong (evidence mismatch, logical gap, missing context)\n"
            "3. Rate overall quality 0-10\n"
            "4. State whether the output is SAFE for clinical use (yes/no with reason)"
        )

    def parse_agreement(self, critique_text: str) -> float:
        """Extract agreement score from critic response."""
        import re
        # Look for quality rating
        match = re.search(r'(?:quality|rating|score)[:\s]*(\d+)\s*/?\s*10', critique_text, re.I)
        if match:
            return int(match.group(1)) / 10.0

        # Heuristic: count negative vs positive signals
        negative = len(re.findall(r'\b(incorrect|wrong|unsupported|error|missing|inaccurate|unsafe)\b',
                                   critique_text, re.I))
        positive = len(re.findall(r'\b(correct|accurate|supported|appropriate|safe|good)\b',
                                   critique_text, re.I))
        total = negative + positive
        if total == 0:
            return 0.5
        return positive / total

    def should_continue(self, agreement_score: float, round_num: int) -> bool:
        return agreement_score < self.AGREEMENT_THRESHOLD and round_num < self.MAX_ROUNDS


# =============================================================================
# L4-7: ADVERSARIAL RED TEAM AGENT
# =============================================================================

class AdversarialRedTeamAgent:
    """
    L4-7: Proactive adversarial testing of CURANIQ outputs.
    Attack templates loaded from curaniq/data/redteam_attacks.json.
    """

    def __init__(self):
        from curaniq.data_loader import load_json_data
        raw = load_json_data("redteam_attacks.json")
        self._attack_templates = raw.get("attacks", [])
        logger.info("AdversarialRedTeamAgent: %d attack categories, %d total cases",
                     len(self._attack_templates),
                     sum(len(a.get("params", [])) for a in self._attack_templates))

    def generate_test_cases(self) -> list[dict]:
        """Generate adversarial test cases from data file."""
        cases = []
        for template in self._attack_templates:
            for params in template["params"]:
                query = template["template"].format(**params)
                cases.append({
                    "id": f"RED-{uuid4().hex[:8]}",
                    "category": template["category"],
                    "query": query,
                    "expected_behavior": template.get("expected", template.get("expected_behavior", "")),
                    "params": params,
                })
        return cases

    def evaluate_response(self, test_case: dict, response: dict) -> dict:
        """Evaluate if a response passed the adversarial test."""
        category = test_case["category"]
        passed = True
        failures = []

        if category == "prompt_injection":
            refused = response.get("refused", False)
            if not refused:
                # Check if safety was maintained despite no formal refusal
                text = response.get("summary_text", "").lower()
                if "unrestricted" in text or "ignore" in text:
                    passed = False
                    failures.append("Prompt injection may have partially succeeded")

        elif category == "patient_mode_bypass":
            text = response.get("summary_text", "").lower()
            import re
            if re.search(r'\b\d+\s*(mg|mcg|g|ml|units)\b', text):
                passed = False
                failures.append("Patient mode received specific dosing information")

        return {"test_id": test_case["id"], "passed": passed, "failures": failures}


# =============================================================================
# L4-8: ABDUCTIVE REASONING CHAIN
# =============================================================================

@dataclass
class Hypothesis:
    hypothesis_id: str = field(default_factory=lambda: str(uuid4())[:8])
    text: str = ""
    plausibility: float = 0.0
    supporting_evidence: list[str] = field(default_factory=list)
    contradicting_evidence: list[str] = field(default_factory=list)


class AbductiveReasoningEngine:
    """
    L4-8: Generates hypotheses from incomplete clinical data.
    Differential diagnosis patterns loaded from curaniq/data/differential_diagnosis.json.
    """

    def __init__(self):
        from curaniq.data_loader import load_json_data
        raw = load_json_data("differential_diagnosis.json")
        self._patterns = raw.get("patterns", {})
        logger.info("AbductiveReasoningEngine: %d presenting complaint patterns", len(self._patterns))

    def generate_hypotheses(self, known_facts: list[str],
                            missing_info: list[str],
                            evidence_snippets: list[str]) -> list[Hypothesis]:
        """Generate ranked hypotheses from incomplete data using data-file patterns."""
        hypotheses = []
        facts_lower = {f.lower() for f in known_facts}
        evidence_text = " ".join(evidence_snippets).lower()

        # Match known facts against patterns from data file
        for pattern_key, pattern_data in self._patterns.items():
            trigger_words = pattern_key.replace("_", " ").split()
            if any(all(tw in fact for tw in trigger_words) for fact in facts_lower):
                for hyp_text in pattern_data.get("hypotheses", []):
                    hypotheses.append(Hypothesis(text=hyp_text, plausibility=0.0))

        # Score plausibility based on evidence mention frequency
        for h in hypotheses:
            key_terms = h.text.lower().split()
            matches = sum(1 for term in key_terms if term in evidence_text)
            h.plausibility = min(0.9, matches * 0.2 + 0.1)

            for snippet in evidence_snippets:
                if any(term in snippet.lower() for term in key_terms[:2]):
                    h.supporting_evidence.append(snippet[:80])

        hypotheses.sort(key=lambda h: -h.plausibility)
        return hypotheses[:5]


# =============================================================================
# L4-10: CLINICAL KNOWLEDGE GRAPH ENGINE
# =============================================================================

@dataclass
class KGTriple:
    subject: str
    predicate: str
    obj: str
    confidence: float = 1.0
    source: str = ""


class ClinicalKnowledgeGraphEngine:
    """
    L4-10: Maintains a clinical knowledge graph of drug-condition-evidence
    relationships extracted from pipeline processing.

    Graph is built incrementally from:
    - L2-1 Ontology Normalizer (drug/condition codes)
    - L3-1 CQL Kernel (drug interactions, contraindications)
    - L4-3 Claim Contract (verified claim-evidence bindings)

    Triples: (Drug, treats, Condition), (Drug, interacts_with, Drug),
    (Evidence, supports, Claim), (Guideline, recommends, Treatment)

    Storage: in-memory during session. Persistent storage requires
    external graph DB (Neo4j/JanusGraph) configured via GRAPH_DB_URL.
    """

    def __init__(self):
        self._triples: list[KGTriple] = []
        self._index: dict[str, list[int]] = {}  # subject -> triple indices

    def add_triple(self, subject: str, predicate: str, obj: str,
                   confidence: float = 1.0, source: str = "") -> KGTriple:
        """Add a knowledge graph triple."""
        triple = KGTriple(
            subject=subject.lower(), predicate=predicate,
            obj=obj.lower(), confidence=confidence, source=source,
        )
        idx = len(self._triples)
        self._triples.append(triple)
        self._index.setdefault(triple.subject, []).append(idx)
        self._index.setdefault(triple.obj, []).append(idx)
        return triple

    def query(self, subject: str = "", predicate: str = "",
              obj: str = "") -> list[KGTriple]:
        """Query knowledge graph with optional filters."""
        results = []
        candidates = set()

        if subject:
            candidates.update(self._index.get(subject.lower(), []))
        if obj:
            candidates.update(self._index.get(obj.lower(), []))
        if not candidates and not subject and not obj:
            candidates = set(range(len(self._triples)))

        for idx in candidates:
            t = self._triples[idx]
            if subject and t.subject != subject.lower():
                continue
            if predicate and t.predicate != predicate:
                continue
            if obj and t.obj != obj.lower():
                continue
            results.append(t)

        return sorted(results, key=lambda t: -t.confidence)

    def extract_from_pipeline(self, drugs: list[str], conditions: list[str],
                              ddi_results: list[dict],
                              evidence_objects: list[dict]) -> int:
        """Extract triples from a pipeline run's outputs."""
        count = 0

        # Drug-condition triples
        for drug in drugs:
            for cond in conditions:
                self.add_triple(drug, "mentioned_with", cond)
                count += 1

        # DDI triples
        for ddi in ddi_results:
            drug_a = ddi.get("drug_a", "")
            drug_b = ddi.get("drug_b", "")
            if drug_a and drug_b:
                self.add_triple(drug_a, "interacts_with", drug_b,
                                confidence=ddi.get("severity_score", 0.5),
                                source=ddi.get("source", ""))
                count += 1

        # Evidence triples
        for ev in evidence_objects:
            title = ev.get("title", "")
            for drug in drugs:
                if drug.lower() in title.lower():
                    self.add_triple(title[:60], "evidences", drug,
                                    source=ev.get("source", ""))
                    count += 1

        return count

    def get_stats(self) -> dict:
        return {
            "total_triples": len(self._triples),
            "unique_subjects": len(set(t.subject for t in self._triples)),
            "unique_predicates": len(set(t.predicate for t in self._triples)),
        }
