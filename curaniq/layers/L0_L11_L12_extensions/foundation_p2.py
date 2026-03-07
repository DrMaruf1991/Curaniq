"""
CURANIQ -- Final P2 Modules (Cluster 7: Foundation)

L0-4   Automated PCCP Documentation Generator
L11-4  Offline & Edge Deployment Mode
L12-10 Clinical Outcome Feedback Loop
L12-11 Outcome-Linked Evidence Strength Adjuster

Logic modules. No hardcoded clinical data.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


# =============================================================================
# L0-4: AUTOMATED PCCP DOCUMENTATION GENERATOR
# Pre-Certification Checklist and Process documentation
# Source: ISO 13485:2016, IEC 62304, FDA SaMD guidance
# =============================================================================

@dataclass
class PCCPSection:
    section_id: str
    title: str
    status: str  # "draft", "review", "approved"
    content_template: str
    applicable_standards: list[str]
    auto_populated_fields: list[str] = field(default_factory=list)


class PCCPDocumentationGenerator:
    """
    L0-4: Generates regulatory documentation from system state.

    Automatically populates Pre-Certification Checklist sections from:
    - L0-1 QMS (quality management records)
    - L0-2 Risk Management (risk register)
    - L0-3 Cybersecurity (security controls)
    - L10-4 Benchmark Dashboard (quality metrics)
    - L10-2 Regression results (validation evidence)

    Outputs: structured JSON that maps to ISO 13485 / IEC 62304 / FDA SaMD
    documentation requirements. NOT a regulatory submission — a preparation tool.
    """

    PCCP_SECTIONS = [
        PCCPSection("1.0", "Product Description", "draft",
                    "SaMD definition, intended use, intended users, clinical claims",
                    ["FDA SaMD N41", "IMDRF SaMD WG"]),
        PCCPSection("2.0", "Quality Management System", "draft",
                    "QMS scope, processes, document control, CAPA",
                    ["ISO 13485:2016 §4-8", "21 CFR 820"]),
        PCCPSection("3.0", "Clinical Evaluation", "draft",
                    "Clinical evidence summary, performance data, intended clinical benefits",
                    ["MDR Annex XIV", "MEDDEV 2.7/1 Rev 4"]),
        PCCPSection("4.0", "Software Lifecycle", "draft",
                    "Development process, version control, change management",
                    ["IEC 62304:2006+A1:2015", "FDA Software Guidance"]),
        PCCPSection("5.0", "Risk Management", "draft",
                    "Risk analysis, risk controls, residual risk evaluation",
                    ["ISO 14971:2019", "IEC 80001-1"]),
        PCCPSection("6.0", "Cybersecurity", "draft",
                    "Threat model, security controls, vulnerability management",
                    ["FDA Cybersecurity Guidance 2023", "IEC 81001-5-1"]),
        PCCPSection("7.0", "Validation & Verification", "draft",
                    "Test plans, regression results, benchmark data",
                    ["IEC 62304 §5.7-5.8", "FDA Software Validation"]),
    ]

    def generate_documentation_status(self, qms_data: dict = None,
                                       benchmark_data: dict = None,
                                       regression_data: dict = None) -> dict:
        """Generate PCCP documentation status report."""
        sections = []
        for section in self.PCCP_SECTIONS:
            auto_fields = []

            # Auto-populate from available data
            if section.section_id == "2.0" and qms_data:
                auto_fields.append(f"QMS processes documented: {qms_data.get('process_count', 'N/A')}")
            if section.section_id == "7.0" and benchmark_data:
                auto_fields.append(f"Benchmark pass rate: {benchmark_data.get('overall', 'N/A')}")
            if section.section_id == "7.0" and regression_data:
                auto_fields.append(f"Regression: {regression_data.get('passed', 0)}/{regression_data.get('total', 0)} passed")

            sections.append({
                "section_id": section.section_id,
                "title": section.title,
                "status": section.status,
                "standards": section.applicable_standards,
                "auto_populated": auto_fields,
                "completeness": len(auto_fields) / max(len(section.applicable_standards), 1),
            })

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_sections": len(sections),
            "sections": sections,
            "overall_readiness": sum(s["completeness"] for s in sections) / len(sections) if sections else 0,
        }


# =============================================================================
# L11-4: OFFLINE & EDGE DEPLOYMENT MODE
# =============================================================================

class OfflineCapability(str, Enum):
    FULL_OFFLINE     = "full_offline"      # All features work offline
    DEGRADED         = "degraded"          # CQL + local data only, no LLM
    ONLINE_REQUIRED  = "online_required"   # Needs connectivity


class OfflineEdgeDeployment:
    """
    L11-4: Manages offline/edge deployment for connectivity-limited settings.

    Uzbekistan/CIS context: many hospitals have intermittent internet.
    CURANIQ must degrade gracefully:

    Online: Full pipeline (evidence retrieval + LLM + all gates)
    Degraded: CQL kernel (deterministic) + local drug data (L11-1) + cached evidence
    Offline: Safety checks only (DDI, allergy, dose limits from local data)

    Architecture: All curaniq/data/*.json files are available offline.
    LLM and API calls are the online-dependent components.
    """

    def __init__(self):
        self._mode = OfflineCapability.ONLINE_REQUIRED
        self._last_sync: Optional[datetime] = None
        self._cached_evidence_count = 0

    def check_connectivity(self) -> bool:
        """Check if external APIs are reachable."""
        import urllib.request
        try:
            urllib.request.urlopen("https://api.anthropic.com", timeout=5)
            return True
        except Exception:
            return False

    def determine_mode(self) -> OfflineCapability:
        """Determine current operational mode based on connectivity."""
        if self.check_connectivity():
            self._mode = OfflineCapability.ONLINE_REQUIRED  # Actually means online is available
            return OfflineCapability.FULL_OFFLINE  # Confusing name — means all features

        # Degraded: can we at least use cached data?
        from curaniq.data_loader import get_data_dir
        data_dir = get_data_dir()
        if data_dir.exists() and any(data_dir.glob("*.json")):
            self._mode = OfflineCapability.DEGRADED
            return OfflineCapability.DEGRADED

        self._mode = OfflineCapability.ONLINE_REQUIRED
        return OfflineCapability.ONLINE_REQUIRED

    def get_offline_capabilities(self) -> dict:
        """List what works offline vs what needs connectivity."""
        return {
            "always_available": [
                "CQL Safety Kernel (DDI, allergy, dose checks)",
                "Deterministic safety gates",
                "Local drug availability filter (L11-1)",
                "Clinical calculators (from JSON data)",
                "Medical jargon simplification",
                "Triage gate",
            ],
            "needs_connectivity": [
                "LLM evidence synthesis (Claude/GPT-4o)",
                "PubMed/Cochrane evidence retrieval",
                "FDA FAERS pharmacovigilance",
                "ClinicalTrials.gov search",
                "Real-time evidence monitoring",
            ],
            "cached_available": [
                "Evidence from last sync",
                "All clinical data files (Beers, AWaRe, EML, etc.)",
                "Clinical pathways and scoring systems",
            ],
            "current_mode": self._mode.value,
            "last_sync": self._last_sync.isoformat() if self._last_sync else None,
        }

    def sync_cache(self):
        """Mark a successful sync timestamp."""
        self._last_sync = datetime.now(timezone.utc)


# =============================================================================
# L12-10: CLINICAL OUTCOME FEEDBACK LOOP
# =============================================================================

class OutcomeFeedbackLoop:
    """
    L12-10: Feeds clinical outcomes back into system quality metrics.

    Workflow:
    1. L10-7 records clinical outcomes (positive/negative)
    2. This module links outcomes to the evidence that supported the recommendation
    3. Evidence that led to negative outcomes gets flagged for review
    4. L12-11 adjusts evidence strength based on outcome data

    This is the "learning loop" — CURANIQ improves from real-world results.
    NOT an ML training loop. Evidence strength ADJUSTMENT, not model retraining.
    """

    def __init__(self):
        self._outcome_evidence_links: list[dict] = []

    def link_outcome_to_evidence(self, outcome_record: dict,
                                  evidence_ids_used: list[str]) -> dict:
        """Link a clinical outcome to the evidence that supported the recommendation."""
        link = {
            "link_id": str(uuid4()),
            "outcome_id": outcome_record.get("record_id", ""),
            "outcome_positive": outcome_record.get("outcome_positive", True),
            "evidence_ids": evidence_ids_used,
            "recommendation": outcome_record.get("curaniq_recommendation", ""),
            "linked_at": datetime.now(timezone.utc).isoformat(),
        }
        self._outcome_evidence_links.append(link)

        if not link["outcome_positive"]:
            logger.warning(
                "NEGATIVE OUTCOME linked to evidence: %s. Flagging for review.",
                evidence_ids_used[:3],
            )

        return link

    def get_evidence_outcome_stats(self) -> dict[str, dict]:
        """Get outcome statistics per evidence source."""
        stats: dict[str, dict] = {}
        for link in self._outcome_evidence_links:
            for ev_id in link["evidence_ids"]:
                if ev_id not in stats:
                    stats[ev_id] = {"positive": 0, "negative": 0, "total": 0}
                stats[ev_id]["total"] += 1
                if link["outcome_positive"]:
                    stats[ev_id]["positive"] += 1
                else:
                    stats[ev_id]["negative"] += 1

        # Calculate rates
        for ev_id, s in stats.items():
            s["positive_rate"] = round(s["positive"] / s["total"], 3) if s["total"] else 0

        return stats


# =============================================================================
# L12-11: OUTCOME-LINKED EVIDENCE STRENGTH ADJUSTER
# =============================================================================

class EvidenceStrengthAdjuster:
    """
    L12-11: Adjusts evidence confidence based on real-world outcome data.

    When evidence consistently leads to negative outcomes, its confidence
    modifier is reduced. When evidence consistently leads to positive
    outcomes, confidence is maintained or slightly increased.

    This is NOT overriding GRADE ratings. It's adding a real-world
    performance modifier: GRADE_confidence * outcome_modifier.

    Thresholds:
    - Negative outcome rate >30% with >=5 cases: reduce confidence by 20%
    - Negative outcome rate >50% with >=3 cases: flag for urgent review
    - Positive outcome rate >90% with >=10 cases: confidence boost 5%
    """

    NEGATIVE_THRESHOLD = 0.30  # >30% negative outcomes = reduce
    URGENT_THRESHOLD = 0.50    # >50% = urgent review
    POSITIVE_THRESHOLD = 0.90  # >90% positive = boost
    MIN_CASES_REDUCE = 5
    MIN_CASES_URGENT = 3
    MIN_CASES_BOOST = 10

    def calculate_adjustment(self, evidence_id: str,
                              outcome_stats: dict) -> dict:
        """Calculate confidence adjustment for an evidence source."""
        stats = outcome_stats.get(evidence_id)
        if not stats or stats["total"] == 0:
            return {"evidence_id": evidence_id, "modifier": 1.0, "action": "no_data"}

        positive_rate = stats["positive_rate"]
        total = stats["total"]
        negative_rate = 1 - positive_rate

        if negative_rate >= self.URGENT_THRESHOLD and total >= self.MIN_CASES_URGENT:
            return {
                "evidence_id": evidence_id,
                "modifier": 0.5,
                "action": "urgent_review",
                "reason": f"Negative outcome rate {negative_rate*100:.0f}% across {total} cases",
            }

        if negative_rate >= self.NEGATIVE_THRESHOLD and total >= self.MIN_CASES_REDUCE:
            return {
                "evidence_id": evidence_id,
                "modifier": 0.8,
                "action": "reduce_confidence",
                "reason": f"Negative outcome rate {negative_rate*100:.0f}% across {total} cases",
            }

        if positive_rate >= self.POSITIVE_THRESHOLD and total >= self.MIN_CASES_BOOST:
            return {
                "evidence_id": evidence_id,
                "modifier": 1.05,
                "action": "boost_confidence",
                "reason": f"Positive outcome rate {positive_rate*100:.0f}% across {total} cases",
            }

        return {"evidence_id": evidence_id, "modifier": 1.0, "action": "maintain"}
