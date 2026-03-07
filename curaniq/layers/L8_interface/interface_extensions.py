"""
CURANIQ -- Layer 8: Clinician Experience & Interface
P2 Interface Extensions

L8-2   Visual Reasoning Maps (claim->evidence->source DAG for UI)
L8-3   Token-Level Uncertainty Visualization
L8-6   Evidence Watchlist + Delta Mode
L8-7   Clinician Challenge Button
L8-9   Patient Education Material Generator
L8-10  Medical Calculator Hub (from curaniq/data/medical_calculators.json)
L8-11  Clinician Review UI & Cryptographic Signing
L8-13  Medical Translation Pipeline & Back-Translation Verifier

All clinical data from JSON data files. No hardcoded formulas.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


# =============================================================================
# L8-2: VISUAL REASONING MAPS
# Transforms the L9-3 CitationProvenanceGraph into UI-renderable DAG
# =============================================================================

@dataclass
class ReasoningNode:
    node_id: str
    node_type: str  # "claim", "evidence", "source", "safety_gate", "cql_rule"
    label: str
    confidence: float = 0.0
    metadata: dict = field(default_factory=dict)


@dataclass
class ReasoningEdge:
    source_id: str
    target_id: str
    relation: str  # "supports", "contradicts", "derived_from", "verified_by"
    strength: float = 1.0


class VisualReasoningMapBuilder:
    """
    L8-2: Builds UI-renderable reasoning DAG from pipeline trace.

    Transforms the internal CitationProvenanceGraph (L9-3) into a
    simplified DAG that the frontend can render as an interactive
    reasoning map showing: Query -> Claims -> Evidence -> Sources
    with safety gate nodes overlaid.
    """

    def build_map(self, claims: list[dict], evidence_objects: list[dict],
                  safety_results: list[dict],
                  cql_results: dict = None) -> dict:
        """Build reasoning map from pipeline outputs."""
        nodes: list[dict] = []
        edges: list[dict] = []

        # Query node (root)
        query_id = "query_root"
        nodes.append({"id": query_id, "type": "query", "label": "Clinical Query"})

        # Claim nodes
        for i, claim in enumerate(claims):
            claim_id = f"claim_{i}"
            nodes.append({
                "id": claim_id,
                "type": "claim",
                "label": claim.get("text", "")[:80],
                "confidence": claim.get("confidence", 0.0),
                "verdict": claim.get("verdict", ""),
            })
            edges.append({"source": query_id, "target": claim_id, "relation": "generates"})

            # Evidence supporting this claim
            for j, ev_id in enumerate(claim.get("evidence_ids", [])):
                ev_node_id = f"evidence_{ev_id}"
                if not any(n["id"] == ev_node_id for n in nodes):
                    ev = next((e for e in evidence_objects if str(e.get("id")) == str(ev_id)), {})
                    nodes.append({
                        "id": ev_node_id,
                        "type": "evidence",
                        "label": ev.get("title", f"Evidence {ev_id}")[:60],
                        "source": ev.get("source", ""),
                        "year": ev.get("year"),
                    })
                edges.append({
                    "source": ev_node_id, "target": claim_id,
                    "relation": claim.get("citation_intent", "supports"),
                })

        # Safety gate nodes
        for gate in safety_results:
            gate_id = f"gate_{gate.get('name', 'unknown')}"
            nodes.append({
                "id": gate_id,
                "type": "safety_gate",
                "label": gate.get("name", ""),
                "passed": gate.get("passed", True),
                "reason": gate.get("reason", ""),
            })
            edges.append({"source": gate_id, "target": query_id, "relation": "verified_by"})

        return {"nodes": nodes, "edges": edges, "generated_at": datetime.now(timezone.utc).isoformat()}


# =============================================================================
# L8-3: TOKEN-LEVEL UNCERTAINTY VISUALIZATION
# =============================================================================

@dataclass
class UncertaintySegment:
    text: str
    confidence: float  # 0-1
    category: str      # "high_confidence", "moderate", "low_confidence", "uncertain"


class TokenUncertaintyVisualizer:
    """
    L8-3: Maps confidence scores to text segments for UI highlighting.

    When the LLM generates output with claim-level confidence from L4-13,
    this module maps those scores to text spans so the UI can render
    color-coded uncertainty (green=high confidence, yellow=moderate, red=low).
    """

    THRESHOLDS = {"high_confidence": 0.8, "moderate": 0.5, "low_confidence": 0.3}

    def segment_output(self, text: str, claim_confidences: list[dict]) -> list[UncertaintySegment]:
        """Map claim-level confidence to text segments."""
        if not claim_confidences:
            return [UncertaintySegment(text=text, confidence=0.5, category="moderate")]

        segments = []
        remaining = text

        for claim in sorted(claim_confidences, key=lambda c: text.find(c.get("text", "")[:20])):
            claim_text = claim.get("text", "")
            conf = claim.get("confidence", 0.5)
            idx = remaining.find(claim_text[:30])

            if idx > 0:
                # Text before this claim
                segments.append(UncertaintySegment(
                    text=remaining[:idx], confidence=0.5, category="moderate"))
                remaining = remaining[idx:]

            if idx >= 0:
                category = (
                    "high_confidence" if conf >= self.THRESHOLDS["high_confidence"]
                    else "moderate" if conf >= self.THRESHOLDS["moderate"]
                    else "low_confidence" if conf >= self.THRESHOLDS["low_confidence"]
                    else "uncertain"
                )
                seg_len = min(len(claim_text), len(remaining))
                segments.append(UncertaintySegment(
                    text=remaining[:seg_len], confidence=conf, category=category))
                remaining = remaining[seg_len:]

        if remaining:
            segments.append(UncertaintySegment(text=remaining, confidence=0.5, category="moderate"))

        return segments


# =============================================================================
# L8-6: EVIDENCE WATCHLIST + DELTA MODE
# =============================================================================

@dataclass
class WatchlistEntry:
    entry_id: str = field(default_factory=lambda: str(uuid4()))
    query: str = ""
    drugs: list[str] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_checked: Optional[datetime] = None
    last_change_detected: Optional[datetime] = None


class EvidenceWatchlist:
    """
    L8-6: Monitors evidence changes for clinician-subscribed topics.

    Clinicians subscribe to drug/condition combinations. When new evidence
    appears (via L1-16 RealTimeEvidenceMonitor) or a guideline updates,
    the watchlist generates a delta notification.
    """

    def __init__(self):
        self._entries: dict[str, WatchlistEntry] = {}

    def subscribe(self, query: str, drugs: list[str] = None,
                  conditions: list[str] = None) -> WatchlistEntry:
        entry = WatchlistEntry(query=query, drugs=drugs or [], conditions=conditions or [])
        self._entries[entry.entry_id] = entry
        return entry

    def unsubscribe(self, entry_id: str) -> bool:
        return self._entries.pop(entry_id, None) is not None

    def check_for_updates(self, new_evidence_topics: list[str]) -> list[dict]:
        """Check if any watchlist entries have new evidence."""
        notifications = []
        topics_lower = {t.lower() for t in new_evidence_topics}

        for entry in self._entries.values():
            entry_terms = {d.lower() for d in entry.drugs} | {c.lower() for c in entry.conditions}
            matches = entry_terms & topics_lower
            if matches:
                entry.last_change_detected = datetime.now(timezone.utc)
                notifications.append({
                    "entry_id": entry.entry_id,
                    "query": entry.query,
                    "matched_topics": list(matches),
                    "detected_at": entry.last_change_detected.isoformat(),
                })
        return notifications

    def get_active_watches(self) -> list[WatchlistEntry]:
        return list(self._entries.values())


# =============================================================================
# L8-7: CLINICIAN CHALLENGE BUTTON
# =============================================================================

@dataclass
class ChallengeRecord:
    challenge_id: str = field(default_factory=lambda: str(uuid4()))
    query_id: str = ""
    clinician_id: str = ""
    claim_challenged: str = ""
    reason: str = ""
    clinician_evidence: str = ""  # What the clinician thinks is correct
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    resolution: Optional[str] = None
    resolved_at: Optional[datetime] = None


class ClinicianChallengeHandler:
    """
    L8-7: Handles clinician challenges to CURANIQ outputs.

    When a clinician disagrees with a claim, they press "Challenge" and
    provide their reasoning. This feeds into:
    - L10-11 Clinician Trust Dashboard (override tracking)
    - L10-8 Clinician Feedback Loop (continuous improvement)
    - L9-1 Audit Ledger (immutable record)
    """

    def __init__(self):
        self._challenges: list[ChallengeRecord] = []

    def submit_challenge(self, query_id: str, clinician_id: str,
                         claim: str, reason: str,
                         clinician_evidence: str = "") -> ChallengeRecord:
        record = ChallengeRecord(
            query_id=query_id, clinician_id=clinician_id,
            claim_challenged=claim, reason=reason,
            clinician_evidence=clinician_evidence,
        )
        self._challenges.append(record)
        logger.warning("CHALLENGE submitted by %s on query %s: %s",
                       clinician_id, query_id, reason[:100])
        return record

    def get_unresolved(self) -> list[ChallengeRecord]:
        return [c for c in self._challenges if c.resolution is None]

    def resolve(self, challenge_id: str, resolution: str) -> bool:
        ch = next((c for c in self._challenges if c.challenge_id == challenge_id), None)
        if not ch:
            return False
        ch.resolution = resolution
        ch.resolved_at = datetime.now(timezone.utc)
        return True


# =============================================================================
# L8-9: PATIENT EDUCATION MATERIAL GENERATOR
# =============================================================================

class ReadabilityLevel(str, Enum):
    GRADE_6   = "grade_6"    # Simple language, short sentences
    GRADE_8   = "grade_8"    # Standard patient education
    GRADE_12  = "grade_12"   # Health-literate patient
    CLINICAL  = "clinical"   # Full clinical language


class PatientEducationGenerator:
    """
    L8-9: Generates patient-friendly versions of clinical information.
    Jargon mappings loaded from curaniq/data/medical_jargon_simplify.json.
    """

    def __init__(self):
        from curaniq.data_loader import load_json_data
        raw = load_json_data("medical_jargon_simplify.json")
        self._simplify_map: dict[str, str] = raw.get("mappings", {})
        logger.info("PatientEducationGenerator: %d jargon mappings", len(self._simplify_map))

    def simplify(self, clinical_text: str,
                 level: ReadabilityLevel = ReadabilityLevel.GRADE_8) -> str:
        """Simplify clinical text for patient education."""
        text = clinical_text

        if level in (ReadabilityLevel.GRADE_6, ReadabilityLevel.GRADE_8):
            for medical, plain in self._simplify_map.items():
                text = re.sub(rf'\b{re.escape(medical)}\b', plain, text, flags=re.I)

            # Add patient framing
            text = text + "\n\nPlease talk to your doctor or pharmacist if you have any questions."

        # Remove dosing for patient mode
        if level == ReadabilityLevel.GRADE_6:
            text = re.sub(r'\b\d+\s*(mg|mcg|g|mL|units?|IU)\b', '[dose]', text)
            text = re.sub(r'\b(BID|TID|QID|OD|q\d+h|twice daily|three times)\b',
                         '[frequency]', text, flags=re.I)

        return text


# =============================================================================
# L8-10: MEDICAL CALCULATOR HUB
# All formulas from curaniq/data/medical_calculators.json
# =============================================================================

class MedicalCalculatorHub:
    """
    L8-10: Deterministic clinical calculators from data file.
    Formulas loaded from curaniq/data/medical_calculators.json.
    Add new calculators by editing JSON — no code change needed.
    """

    def __init__(self):
        from curaniq.data_loader import load_json_data
        raw = load_json_data("medical_calculators.json")
        self._calculators = raw.get("calculators", {})
        logger.info("MedicalCalculatorHub: %d calculators", len(self._calculators))

    def calculate(self, calculator_name: str, inputs: dict) -> Optional[dict]:
        """Run a clinical calculator with given inputs."""
        calc = self._calculators.get(calculator_name)
        if not calc:
            return None

        formula = calc.get("formula")
        if not formula:
            return {"error": "Calculator has no evaluable formula", "name": calculator_name}

        try:
            result = self._safe_eval(formula, inputs)
        except Exception as e:
            return {"error": f"Calculation failed: {e}", "name": calculator_name}

        # Find interpretation
        interpretation = ""
        for interp in calc.get("interpretation", []):
            r = interp.get("range", [0, 999])
            if len(r) == 2 and r[0] <= result < r[1]:
                interpretation = interp.get("label", "")
                break

        return {
            "name": calculator_name,
            "full_name": calc.get("full_name", ""),
            "result": round(result, 2) if isinstance(result, float) else result,
            "unit": calc.get("unit", ""),
            "interpretation": interpretation,
            "source": calc.get("source", ""),
        }

    def _safe_eval(self, formula: str, variables: dict) -> float:
        """
        Safe formula evaluation using AST parsing.
        Only allows: numbers, variables, arithmetic (+,-,*,/,**), comparisons, ternary.
        BLOCKS: function calls, imports, attribute access, subscripts.
        """
        import ast
        import operator

        SAFE_OPS = {
            ast.Add: operator.add,
            ast.Sub: operator.sub,
            ast.Mult: operator.mul,
            ast.Div: operator.truediv,
            ast.Pow: operator.pow,
            ast.USub: operator.neg,
            ast.Mod: operator.mod,
        }

        SAFE_COMPARE = {
            ast.Eq: operator.eq,
            ast.NotEq: operator.ne,
            ast.Lt: operator.lt,
            ast.LtE: operator.le,
            ast.Gt: operator.gt,
            ast.GtE: operator.ge,
        }

        def _eval_node(node):
            if isinstance(node, ast.Expression):
                return _eval_node(node.body)
            elif isinstance(node, ast.Constant):
                if isinstance(node.value, (int, float)):
                    return node.value
                elif isinstance(node.value, str):
                    return node.value
                raise ValueError(f"Unsupported constant type: {type(node.value)}")
            elif isinstance(node, ast.Name):
                if node.id in variables:
                    return variables[node.id]
                raise ValueError(f"Unknown variable: {node.id}")
            elif isinstance(node, ast.BinOp):
                op_fn = SAFE_OPS.get(type(node.op))
                if not op_fn:
                    raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
                return op_fn(_eval_node(node.left), _eval_node(node.right))
            elif isinstance(node, ast.UnaryOp):
                op_fn = SAFE_OPS.get(type(node.op))
                if not op_fn:
                    raise ValueError(f"Unsupported unary op: {type(node.op).__name__}")
                return op_fn(_eval_node(node.operand))
            elif isinstance(node, ast.Compare):
                left = _eval_node(node.left)
                for op, comparator in zip(node.ops, node.comparators):
                    comp_fn = SAFE_COMPARE.get(type(op))
                    if not comp_fn:
                        raise ValueError(f"Unsupported comparison: {type(op).__name__}")
                    right = _eval_node(comparator)
                    if not comp_fn(left, right):
                        return False
                    left = right
                return True
            elif isinstance(node, ast.IfExp):
                # Ternary: a if condition else b
                if _eval_node(node.test):
                    return _eval_node(node.body)
                return _eval_node(node.orelse)
            else:
                raise ValueError(f"Unsafe AST node: {type(node).__name__}")

        tree = ast.parse(formula, mode='eval')
        return float(_eval_node(tree))

    def get_available(self) -> list[dict]:
        return [
            {"name": k, "full_name": v.get("full_name", ""),
             "inputs": list(v.get("inputs", {}).keys())}
            for k, v in self._calculators.items()
        ]


# =============================================================================
# L8-11: CLINICIAN REVIEW UI & CRYPTOGRAPHIC SIGNING
# =============================================================================

@dataclass
class SignedReview:
    review_id: str = field(default_factory=lambda: str(uuid4()))
    query_id: str = ""
    clinician_id: str = ""
    action: str = ""        # "approved", "modified", "rejected"
    modifications: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    signature: str = ""     # HMAC-SHA256 of review content


class ClinicianReviewSigner:
    """
    L8-11: Cryptographic signing of clinician review decisions.

    Every clinician approval/modification/rejection is signed with
    HMAC-SHA256 using an institution-specific secret. This creates
    a tamper-evident audit trail for regulatory compliance.

    Secret: from env CURANIQ_REVIEW_SECRET (fail-closed if not set).
    """

    def __init__(self):
        self._secret = os.environ.get("CURANIQ_REVIEW_SECRET", "").encode()
        if not self._secret:
            logger.warning("CURANIQ_REVIEW_SECRET not set — reviews will not be cryptographically signed")

    def sign_review(self, query_id: str, clinician_id: str,
                    action: str, modifications: str = "") -> SignedReview:
        """Create a cryptographically signed review record."""
        review = SignedReview(
            query_id=query_id, clinician_id=clinician_id,
            action=action, modifications=modifications,
        )

        # Content to sign
        content = (
            f"{review.review_id}|{query_id}|{clinician_id}|"
            f"{action}|{modifications}|{review.timestamp.isoformat()}"
        )

        if self._secret:
            review.signature = hmac.new(
                self._secret, content.encode(), hashlib.sha256,
            ).hexdigest()
        else:
            review.signature = f"UNSIGNED:{hashlib.sha256(content.encode()).hexdigest()[:16]}"

        return review

    def verify_signature(self, review: SignedReview) -> bool:
        """Verify a review's cryptographic signature."""
        if not self._secret or review.signature.startswith("UNSIGNED:"):
            return False

        content = (
            f"{review.review_id}|{review.query_id}|{review.clinician_id}|"
            f"{review.action}|{review.modifications}|{review.timestamp.isoformat()}"
        )
        expected = hmac.new(self._secret, content.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(review.signature, expected)


# =============================================================================
# L8-13: BACK-TRANSLATION VERIFIER
# =============================================================================

class BackTranslationVerifier:
    """
    L8-13: Verifies translation quality via back-translation.

    Flow: Original (EN) -> Translate to RU/UZ -> Back-translate to EN
    -> Compare original vs back-translated for semantic preservation.

    Connected to L8-5 Multilingual Clinical Interface.
    Critical safety check: medical negations MUST survive round-trip.
    Example: "do NOT take" -> "НЕ принимайте" -> "do NOT take" (correct)
             "do NOT take" -> "принимайте" -> "take" (FAILED — lost negation)
    """

    NEGATION_PATTERNS: list[re.Pattern] = [
        re.compile(r'\b(not|never|no|don\'t|do not|cannot|should not|must not|avoid|stop|discontinue|contraindicated)\b', re.I),
    ]

    DOSE_PATTERNS: list[re.Pattern] = [
        re.compile(r'\b(\d+(?:\.\d+)?)\s*(mg|mcg|g|mL|units?|IU)\b', re.I),
    ]

    def verify_round_trip(self, original: str, back_translated: str) -> dict:
        """Compare original and back-translated text for semantic preservation."""
        result = {
            "original": original,
            "back_translated": back_translated,
            "passed": True,
            "failures": [],
        }

        # Check negation preservation
        orig_negations = sum(len(p.findall(original)) for p in self.NEGATION_PATTERNS)
        back_negations = sum(len(p.findall(back_translated)) for p in self.NEGATION_PATTERNS)

        if orig_negations > 0 and back_negations < orig_negations:
            result["passed"] = False
            result["failures"].append({
                "type": "lost_negation",
                "severity": "critical",
                "detail": f"Original has {orig_negations} negation(s), back-translation has {back_negations}. "
                         "Medical negation may have been lost in translation.",
            })

        # Check dose preservation
        orig_doses = set()
        back_doses = set()
        for p in self.DOSE_PATTERNS:
            orig_doses.update(m.group() for m in p.finditer(original))
            back_doses.update(m.group() for m in p.finditer(back_translated))

        lost_doses = orig_doses - back_doses
        if lost_doses:
            result["passed"] = False
            result["failures"].append({
                "type": "lost_dose",
                "severity": "high",
                "detail": f"Doses lost in translation: {', '.join(lost_doses)}",
            })

        return result
