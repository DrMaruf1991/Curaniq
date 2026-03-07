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

