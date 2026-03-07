"""
CURANIQ — Medical Evidence Operating System

L9-3  Citation Provenance Graph (Claim → Evidence Card → Source)
L10-2 Synthetic Patient Regression (Synthea-based CI/CD testing)
L10-4 Benchmark Dashboard (public quality metrics)

Architecture: L9-3 provides click-through provenance from any claim
to its original evidence. L10-2 runs nightly regression against
synthetic patients. L10-4 publishes quality metrics publicly.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# L9-3: CITATION PROVENANCE GRAPH
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ProvenanceNode:
    """A node in the citation provenance graph."""
    node_id: str = field(default_factory=lambda: str(uuid4()))
    node_type: str = ""  # "claim", "evidence", "source", "rule", "computation"
    label: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class ProvenanceEdge:
    """A directed edge: source_node → target_node with relationship type."""
    source_id: str = ""
    target_id: str = ""
    relationship: str = ""  # "entailed_by", "computed_by", "sourced_from", "verified_by"
    confidence: float = 1.0


@dataclass
class ProvenanceTrace:
    """Complete provenance trace for one query response."""
    trace_id: str = field(default_factory=lambda: str(uuid4()))
    query_id: str = ""
    nodes: list[ProvenanceNode] = field(default_factory=list)
    edges: list[ProvenanceEdge] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class CitationProvenanceGraph:
    """
    L9-3: Builds a directed acyclic graph (DAG) of citation provenance.

    For every claim in the response:
    Claim → (entailed_by) → Evidence Snippet → (sourced_from) → Original Source
         → (verified_by) → NLI Model / Adversarial Jury
         → (computed_by) → CQL Rule (if deterministic)

    This enables:
    1. Click-through: user clicks claim → sees evidence card → sees original paper
    2. Incident investigation: trace any claim back to its full evidence chain
    3. Regulatory audit: demonstrate that every claim has provenance
    """

    def __init__(self):
        self._traces: dict[str, ProvenanceTrace] = {}

    def build_trace(
        self,
        query_id: str,
        claims: list[dict],
        evidence_objects: list[dict],
        cql_logs: list[dict],
        jury_results: Optional[list[dict]] = None,
    ) -> ProvenanceTrace:
        """Build a complete provenance trace for a query response."""
        trace = ProvenanceTrace(query_id=query_id)

        # Create evidence source nodes
        source_nodes: dict[str, str] = {}
        for ev in evidence_objects:
            # Source node (e.g., PubMed article)
            source_id = ev.get("source_id", str(uuid4()))
            source_node = ProvenanceNode(
                node_type="source",
                label=ev.get("title", "Unknown Source"),
                metadata={
                    "source_id": source_id,
                    "url": ev.get("url", ""),
                    "published": ev.get("published_date", ""),
                    "source_type": ev.get("source_type", ""),
                },
            )
            trace.nodes.append(source_node)
            source_nodes[source_id] = source_node.node_id

            # Evidence snippet node
            evidence_node = ProvenanceNode(
                node_type="evidence",
                label=ev.get("snippet", "")[:100] + "...",
                metadata={
                    "evidence_id": ev.get("evidence_id", ""),
                    "snippet_hash": hashlib.sha256(
                        ev.get("snippet", "").encode()
                    ).hexdigest()[:16],
                    "grade": ev.get("grade", ""),
                },
            )
            trace.nodes.append(evidence_node)

            # Edge: evidence → source
            trace.edges.append(ProvenanceEdge(
                source_id=evidence_node.node_id,
                target_id=source_node.node_id,
                relationship="sourced_from",
            ))

        # Create CQL computation nodes
        cql_node_ids: dict[str, str] = {}
        for cql_log in cql_logs:
            cql_node = ProvenanceNode(
                node_type="computation",
                label=f"CQL: {cql_log.get('rule_id', '')}",
                metadata={
                    "rule_id": cql_log.get("rule_id", ""),
                    "rule_version": cql_log.get("rule_version", ""),
                    "formula": cql_log.get("formula_applied", ""),
                    "output": cql_log.get("output_value", ""),
                },
            )
            trace.nodes.append(cql_node)
            cql_node_ids[cql_log.get("rule_id", "")] = cql_node.node_id

        # Create claim nodes with edges to evidence
        for claim in claims:
            claim_node = ProvenanceNode(
                node_type="claim",
                label=claim.get("claim_text", "")[:100],
                metadata={
                    "claim_type": claim.get("claim_type", ""),
                    "confidence": claim.get("confidence_score", 0.0),
                    "is_blocked": claim.get("is_blocked", False),
                },
            )
            trace.nodes.append(claim_node)

            # Edges: claim → evidence
            for ev_id in claim.get("evidence_ids", []):
                ev_node = next(
                    (n for n in trace.nodes
                     if n.node_type == "evidence"
                     and n.metadata.get("evidence_id") == str(ev_id)),
                    None,
                )
                if ev_node:
                    trace.edges.append(ProvenanceEdge(
                        source_id=claim_node.node_id,
                        target_id=ev_node.node_id,
                        relationship="entailed_by",
                        confidence=claim.get("confidence_score", 0.0),
                    ))

            # Edge: claim → CQL (if claim has deterministic computation)
            for nt in claim.get("numeric_tokens", []):
                rule_id = nt.get("cql_rule_id", "")
                if rule_id and rule_id in cql_node_ids:
                    trace.edges.append(ProvenanceEdge(
                        source_id=claim_node.node_id,
                        target_id=cql_node_ids[rule_id],
                        relationship="computed_by",
                    ))

        # Store trace
        self._traces[query_id] = trace
        return trace

    def get_trace(self, query_id: str) -> Optional[ProvenanceTrace]:
        return self._traces.get(query_id)

    def get_claim_provenance(self, query_id: str, claim_index: int) -> list[dict]:
        """Get full provenance chain for a specific claim."""
        trace = self._traces.get(query_id)
        if not trace:
            return []

        claim_nodes = [n for n in trace.nodes if n.node_type == "claim"]
        if claim_index >= len(claim_nodes):
            return []

        claim_node = claim_nodes[claim_index]
        chain = [{"type": "claim", "label": claim_node.label, "metadata": claim_node.metadata}]

        # Follow edges from claim
        for edge in trace.edges:
            if edge.source_id == claim_node.node_id:
                target = next((n for n in trace.nodes if n.node_id == edge.target_id), None)
                if target:
                    entry = {
                        "type": target.node_type,
                        "relationship": edge.relationship,
                        "label": target.label,
                        "metadata": target.metadata,
                    }
                    chain.append(entry)

                    # Follow one more level (evidence → source)
                    for inner_edge in trace.edges:
                        if inner_edge.source_id == target.node_id:
                            inner_target = next(
                                (n for n in trace.nodes if n.node_id == inner_edge.target_id), None
                            )
                            if inner_target:
                                chain.append({
                                    "type": inner_target.node_type,
                                    "relationship": inner_edge.relationship,
                                    "label": inner_target.label,
                                    "metadata": inner_target.metadata,
                                })

        return chain


# ─────────────────────────────────────────────────────────────────────────────
# L10-2: SYNTHETIC PATIENT REGRESSION
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SyntheticPatientCase:
    """A synthetic patient test case for regression testing."""
    case_id: str
    patient_demographics: dict
    conditions: list[str]
    medications: list[str]
    query: str
    expected_safety_flags: list[str] = field(default_factory=list)
    expected_refusal: bool = False
    expected_contains: list[str] = field(default_factory=list)
    expected_excludes: list[str] = field(default_factory=list)


@dataclass
class RegressionResult:
    case_id: str
    passed: bool = False
    execution_time_ms: float = 0.0
    safety_flags_correct: bool = True
    refusal_correct: bool = True
    content_correct: bool = True
    failure_reasons: list[str] = field(default_factory=list)


class SyntheticPatientRegression:
    """
    L10-2: Nightly CI/CD regression against synthetic patients.

    Uses Synthea-style synthetic patient data to test CURANIQ against
    known-correct clinical scenarios. Every pipeline change must pass
    regression before deployment.

    Test categories:
    1. Dose safety: known renal/hepatic adjustments
    2. DDI detection: known drug interactions
    3. Contraindications: known absolute contraindications
    4. Edge cases: pregnancy, pediatric, geriatric, dialysis
    5. Refusal correctness: should refuse when evidence insufficient
    """

    # Built-in regression cases (not hardcoded clinical data —
    # these are TEST SCENARIOS with expected pipeline behavior)
    REGRESSION_SUITE: list[SyntheticPatientCase] = [
        SyntheticPatientCase(
            case_id="REG-001-RENAL",
            patient_demographics={"age": 72, "sex": "M", "weight_kg": 68},
            conditions=["CKD stage 4", "type 2 diabetes"],
            medications=["metformin 1000mg BID"],
            query="Is metformin safe for this patient?",
            expected_safety_flags=["RENAL_ADJUSTMENT"],
            expected_contains=["egfr", "renal", "dose"],
        ),
        SyntheticPatientCase(
            case_id="REG-002-DDI",
            patient_demographics={"age": 55, "sex": "F", "weight_kg": 70},
            conditions=["atrial fibrillation", "fungal infection"],
            medications=["warfarin 5mg daily", "fluconazole 200mg daily"],
            query="Any interactions between these medications?",
            expected_safety_flags=["DRUG_INTERACTION"],
            expected_contains=["interaction", "inr", "monitor"],
        ),
        SyntheticPatientCase(
            case_id="REG-003-PREGNANCY",
            patient_demographics={"age": 28, "sex": "F", "weight_kg": 65},
            conditions=["pregnancy week 14", "epilepsy"],
            medications=["valproic acid 500mg BID"],
            query="Is valproic acid safe in pregnancy?",
            expected_safety_flags=["PREGNANCY_RISK", "TERATOGEN"],
            expected_contains=["contraindicated", "pregnancy", "neural tube"],
        ),
        SyntheticPatientCase(
            case_id="REG-004-QT",
            patient_demographics={"age": 65, "sex": "M", "weight_kg": 80},
            conditions=["heart failure", "pneumonia"],
            medications=["amiodarone 200mg daily", "azithromycin 500mg daily"],
            query="QT risk with these medications?",
            expected_safety_flags=["QT_PROLONGATION"],
            expected_contains=["qt", "ecg", "risk"],
        ),
        SyntheticPatientCase(
            case_id="REG-005-PEDIATRIC",
            patient_demographics={"age": 3, "sex": "M", "weight_kg": 14},
            conditions=["otitis media"],
            medications=["amoxicillin"],
            query="Amoxicillin dose for this child?",
            expected_safety_flags=["PEDIATRIC_DOSING"],
            expected_contains=["mg/kg", "weight"],
        ),
        SyntheticPatientCase(
            case_id="REG-006-REFUSAL",
            patient_demographics={"age": 45, "sex": "F", "weight_kg": 60},
            conditions=["rare disease XYZ"],
            medications=[],
            query="What is the treatment for rare disease XYZ?",
            expected_refusal=True,
        ),
    ]

    def __init__(self):
        self._results: list[RegressionResult] = []

    def run_regression(
        self,
        pipeline_fn,
        cases: Optional[list[SyntheticPatientCase]] = None,
    ) -> dict[str, Any]:
        """
        Run regression suite against a pipeline function.
        pipeline_fn takes (query_text, patient_context) → dict with response.
        """
        test_cases = cases or self.REGRESSION_SUITE
        results = []

        for case in test_cases:
            start = time.perf_counter()
            result = RegressionResult(case_id=case.case_id)

            try:
                response = pipeline_fn(case.query, {
                    "demographics": case.patient_demographics,
                    "conditions": case.conditions,
                    "medications": case.medications,
                })

                elapsed = (time.perf_counter() - start) * 1000
                result.execution_time_ms = elapsed

                # Check refusal
                actual_refused = response.get("refused", False)
                result.refusal_correct = actual_refused == case.expected_refusal
                if not result.refusal_correct:
                    result.failure_reasons.append(
                        f"Refusal mismatch: expected={case.expected_refusal}, actual={actual_refused}"
                    )

                # Check safety flags
                actual_flags = response.get("safety_flags", [])
                for expected_flag in case.expected_safety_flags:
                    if expected_flag not in actual_flags:
                        result.safety_flags_correct = False
                        result.failure_reasons.append(f"Missing safety flag: {expected_flag}")

                # Check content
                output_text = response.get("summary_text", "").lower()
                for expected in case.expected_contains:
                    if expected.lower() not in output_text:
                        result.content_correct = False
                        result.failure_reasons.append(f"Missing expected content: '{expected}'")

                for excluded in case.expected_excludes:
                    if excluded.lower() in output_text:
                        result.content_correct = False
                        result.failure_reasons.append(f"Contains excluded content: '{excluded}'")

                result.passed = (
                    result.safety_flags_correct
                    and result.refusal_correct
                    and result.content_correct
                )

            except Exception as e:
                result.passed = False
                result.failure_reasons.append(f"Pipeline exception: {e}")

            results.append(result)

        self._results = results

        passed = sum(1 for r in results if r.passed)
        return {
            "total": len(results),
            "passed": passed,
            "failed": len(results) - passed,
            "pass_rate": passed / len(results) if results else 0.0,
            "failures": [
                {"case": r.case_id, "reasons": r.failure_reasons}
                for r in results if not r.passed
            ],
            "avg_execution_ms": (
                sum(r.execution_time_ms for r in results) / len(results)
                if results else 0.0
            ),
        }


# ─────────────────────────────────────────────────────────────────────────────
# L10-4: BENCHMARK DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BenchmarkMetric:
    name: str
    value: float
    target: float
    unit: str = ""
    passed: bool = False
    measured_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class BenchmarkDashboard:
    """
    L10-4: Public benchmark dashboard for CURANIQ quality metrics.

    Tracks and publishes:
    - Citation Correctness Rate
    - Claim-Evidence Entailment Score
    - Medication Safety Error Rate
    - Retraction Contamination Rate
    - Guideline Concordance Rate
    - Staleness Lag (median hours)
    - Refusal Appropriateness
    - DDI Detection Accuracy
    """

    METRIC_DEFINITIONS: dict[str, dict] = {
        "citation_correctness": {"target": 0.95, "unit": "rate", "description": "% citations real, relevant, current, supporting"},
        "entailment_score": {"target": 0.90, "unit": "rate", "description": "% evidence actually supports claim"},
        "medication_safety_error_rate": {"target": 0.001, "unit": "per_1000", "description": "Errors per 1,000 queries"},
        "retraction_contamination": {"target": 0.0, "unit": "rate", "description": "% outputs citing retracted sources"},
        "guideline_concordance": {"target": 0.85, "unit": "rate", "description": "% agreement with guidelines"},
        "staleness_lag_hours": {"target": 24.0, "unit": "hours", "description": "Median hours from release to availability"},
        "refusal_appropriateness": {"target": 0.90, "unit": "rate", "description": "Correct refuse/answer ratio"},
        "ddi_detection_sensitivity": {"target": 0.95, "unit": "rate", "description": "DDI true positive rate"},
        "ddi_detection_specificity": {"target": 0.90, "unit": "rate", "description": "DDI true negative rate"},
        "edge_case_detection": {"target": 0.85, "unit": "rate", "description": "% high-risk contexts correctly flagged"},
    }

    def __init__(self):
        self._metrics: dict[str, BenchmarkMetric] = {}
        self._history: list[dict] = []

    def record_metric(self, metric_name: str, value: float) -> Optional[BenchmarkMetric]:
        """Record a benchmark measurement."""
        definition = self.METRIC_DEFINITIONS.get(metric_name)
        if not definition:
            logger.warning("Unknown benchmark metric: %s", metric_name)
            return None

        target = definition["target"]
        # For error rates, lower is better; for everything else, higher is better
        if "error" in metric_name or "contamination" in metric_name or "lag" in metric_name:
            passed = value <= target
        else:
            passed = value >= target

        metric = BenchmarkMetric(
            name=metric_name,
            value=round(value, 4),
            target=target,
            unit=definition["unit"],
            passed=passed,
        )
        self._metrics[metric_name] = metric
        self._history.append({
            "metric": metric_name,
            "value": value,
            "target": target,
            "passed": passed,
            "timestamp": metric.measured_at.isoformat(),
        })
        return metric

    def get_dashboard(self) -> dict[str, Any]:
        """Generate the public benchmark dashboard data."""
        metrics_data = {}
        for name, definition in self.METRIC_DEFINITIONS.items():
            metric = self._metrics.get(name)
            metrics_data[name] = {
                "description": definition["description"],
                "target": definition["target"],
                "current": metric.value if metric else None,
                "passed": metric.passed if metric else None,
                "unit": definition["unit"],
            }

        total = len(self._metrics)
        passed = sum(1 for m in self._metrics.values() if m.passed)

        return {
            "dashboard_version": "1.0",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "overall_score": passed / total if total > 0 else 0.0,
            "metrics_measured": total,
            "metrics_passing": passed,
            "metrics": metrics_data,
        }
