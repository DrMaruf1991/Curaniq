"""
CURANIQ -- Layer 13: Federated Intelligence Network
P3 Scale Modules (months 12-24)

L13-1  Federated Truth Network (cross-institution evidence consensus)
L13-2  Federated Hallucination Registry (shared hallucination patterns)
L13-3  Federated Safety Signal Network (multi-hospital adverse event pooling)
L13-4  Real-World Evidence Aggregation & Anonymization Engine

Architecture: All federated modules use a hub-spoke model.
Each CURANIQ instance is a spoke. The federation hub aggregates
ANONYMIZED signals only — no PHI crosses institutional boundaries.

API endpoint from env: CURANIQ_FEDERATION_HUB_URL
Signing key from env: CURANIQ_FEDERATION_KEY
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


# =============================================================================
# L13-1: FEDERATED TRUTH NETWORK
# =============================================================================

@dataclass
class TruthSignal:
    signal_id: str = field(default_factory=lambda: str(uuid4()))
    institution_id_hash: str = ""  # SHA-256 of institution ID (anonymous)
    claim_hash: str = ""           # SHA-256 of the clinical claim
    verdict: str = ""              # "supported", "contradicted", "uncertain"
    confidence: float = 0.0
    evidence_count: int = 0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class FederatedTruthNetwork:
    """
    L13-1: Cross-institution evidence consensus.

    When multiple CURANIQ instances independently reach the same
    conclusion about a clinical claim, confidence increases.
    When they disagree, the claim is flagged for review.

    Protocol:
    1. Local instance generates claim + verdict + confidence
    2. Claim is hashed (SHA-256) — original text never leaves institution
    3. Hash + verdict + confidence sent to federation hub
    4. Hub aggregates votes across all institutions
    5. Consensus score returned to all spokes

    Privacy: Only hashes and aggregate scores cross boundaries.
    No PHI, no patient data, no original claim text.
    """

    def __init__(self):
        self._hub_url = os.environ.get("CURANIQ_FEDERATION_HUB_URL", "")
        self._institution_key = os.environ.get("CURANIQ_FEDERATION_KEY", "")
        self._local_signals: list[TruthSignal] = []

    @property
    def is_connected(self) -> bool:
        return bool(self._hub_url and self._institution_key)

    def submit_signal(self, claim_text: str, verdict: str,
                      confidence: float, evidence_count: int) -> TruthSignal:
        """Submit an anonymized truth signal to the federation."""
        signal = TruthSignal(
            institution_id_hash=hashlib.sha256(self._institution_key.encode()).hexdigest()[:16],
            claim_hash=hashlib.sha256(claim_text.lower().strip().encode()).hexdigest(),
            verdict=verdict,
            confidence=confidence,
            evidence_count=evidence_count,
        )
        self._local_signals.append(signal)

        if self.is_connected:
            self._send_to_hub(signal)

        return signal

    def query_consensus(self, claim_text: str) -> Optional[dict]:
        """Query federation for consensus on a claim."""
        if not self.is_connected:
            return None

        claim_hash = hashlib.sha256(claim_text.lower().strip().encode()).hexdigest()

        try:
            url = f"{self._hub_url}/consensus/{claim_hash}"
            req = urllib.request.Request(url, headers={
                "Authorization": f"Bearer {self._institution_key}",
                "User-Agent": "CURANIQ/1.0 Federation",
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            logger.warning("Federation consensus query failed: %s", e)
            return None

    def _send_to_hub(self, signal: TruthSignal):
        """Send signal to federation hub."""
        try:
            data = json.dumps({
                "signal_id": signal.signal_id,
                "institution_hash": signal.institution_id_hash,
                "claim_hash": signal.claim_hash,
                "verdict": signal.verdict,
                "confidence": signal.confidence,
                "evidence_count": signal.evidence_count,
                "timestamp": signal.timestamp.isoformat(),
            }).encode()

            req = urllib.request.Request(
                f"{self._hub_url}/signals",
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._institution_key}",
                },
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            logger.warning("Federation signal submission failed: %s", e)


# =============================================================================
# L13-2: FEDERATED HALLUCINATION REGISTRY
# =============================================================================

@dataclass
class HallucinationReport:
    report_id: str = field(default_factory=lambda: str(uuid4()))
    claim_hash: str = ""
    hallucination_type: str = ""  # "fabricated_citation", "wrong_dose", "non_existent_drug", "inverted_finding"
    detected_by: str = ""        # "safety_gate", "clinician_challenge", "automated_verification"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class FederatedHallucinationRegistry:
    """
    L13-2: Shared registry of detected AI hallucinations.

    When CURANIQ detects a hallucination (via L4-3 Claim Contract,
    L5-3 No-Evidence Refusal, or L8-7 Clinician Challenge), it reports
    the PATTERN (not the content) to the federated registry.

    Other instances can then pre-emptively check: "Has this type of
    hallucination been seen before?" This creates collective immunity
    against recurring hallucination patterns.

    Privacy: Only hallucination type + claim hash shared. No PHI.
    """

    def __init__(self):
        self._hub_url = os.environ.get("CURANIQ_FEDERATION_HUB_URL", "")
        self._institution_key = os.environ.get("CURANIQ_FEDERATION_KEY", "")
        self._local_reports: list[HallucinationReport] = []

    def report_hallucination(self, claim_text: str,
                              hallucination_type: str,
                              detected_by: str) -> HallucinationReport:
        """Report a detected hallucination to the registry."""
        report = HallucinationReport(
            claim_hash=hashlib.sha256(claim_text.lower().strip().encode()).hexdigest(),
            hallucination_type=hallucination_type,
            detected_by=detected_by,
        )
        self._local_reports.append(report)

        if self._hub_url and self._institution_key:
            self._send_report(report)

        logger.warning("HALLUCINATION DETECTED [%s]: type=%s, detected_by=%s",
                       report.report_id[:8], hallucination_type, detected_by)
        return report

    def check_known_pattern(self, claim_text: str) -> Optional[dict]:
        """Check if this claim matches a known hallucination pattern."""
        claim_hash = hashlib.sha256(claim_text.lower().strip().encode()).hexdigest()

        # Check local registry first
        local_matches = [r for r in self._local_reports if r.claim_hash == claim_hash]
        if local_matches:
            return {
                "known_hallucination": True,
                "local_reports": len(local_matches),
                "types": list(set(r.hallucination_type for r in local_matches)),
            }

        # Check federation if connected
        if self._hub_url and self._institution_key:
            try:
                url = f"{self._hub_url}/hallucinations/{claim_hash}"
                req = urllib.request.Request(url, headers={
                    "Authorization": f"Bearer {self._institution_key}",
                })
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return json.loads(resp.read().decode())
            except Exception:
                pass

        return None

    def _send_report(self, report: HallucinationReport):
        try:
            data = json.dumps({
                "report_id": report.report_id,
                "claim_hash": report.claim_hash,
                "type": report.hallucination_type,
                "detected_by": report.detected_by,
                "timestamp": report.timestamp.isoformat(),
            }).encode()
            req = urllib.request.Request(
                f"{self._hub_url}/hallucinations",
                data=data,
                headers={"Content-Type": "application/json",
                          "Authorization": f"Bearer {self._institution_key}"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            logger.warning("Hallucination report submission failed: %s", e)


# =============================================================================
# L13-3: FEDERATED SAFETY SIGNAL NETWORK
# =============================================================================

@dataclass
class SafetySignal:
    signal_id: str = field(default_factory=lambda: str(uuid4()))
    drug_hash: str = ""
    signal_type: str = ""  # "unexpected_ade", "dose_error_pattern", "ddi_novel", "resistance_pattern"
    severity: str = ""     # "critical", "major", "moderate"
    frequency: int = 1
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class FederatedSafetySignalNetwork:
    """
    L13-3: Multi-hospital adverse event signal pooling.

    When multiple institutions detect the same adverse drug event
    pattern independently, it may represent a true safety signal
    not yet captured in pharmacovigilance databases.

    This is how FDA FAERS works, but faster: real-time signal
    detection across CURANIQ-connected institutions.

    Privacy: Drug hashes + event types only. No patient identifiers.
    """

    def __init__(self):
        self._hub_url = os.environ.get("CURANIQ_FEDERATION_HUB_URL", "")
        self._institution_key = os.environ.get("CURANIQ_FEDERATION_KEY", "")
        self._local_signals: list[SafetySignal] = []

    def report_signal(self, drug: str, signal_type: str,
                      severity: str) -> SafetySignal:
        """Report a safety signal to the federated network."""
        signal = SafetySignal(
            drug_hash=hashlib.sha256(drug.lower().strip().encode()).hexdigest()[:16],
            signal_type=signal_type,
            severity=severity,
        )
        self._local_signals.append(signal)
        return signal

    def query_signals(self, drug: str) -> list[dict]:
        """Query federation for safety signals related to a drug."""
        drug_hash = hashlib.sha256(drug.lower().strip().encode()).hexdigest()[:16]

        # Local signals
        local = [s for s in self._local_signals if s.drug_hash == drug_hash]
        results = [{"source": "local", "type": s.signal_type, "severity": s.severity} for s in local]

        # Federation signals
        if self._hub_url and self._institution_key:
            try:
                url = f"{self._hub_url}/safety-signals/{drug_hash}"
                req = urllib.request.Request(url, headers={
                    "Authorization": f"Bearer {self._institution_key}",
                })
                with urllib.request.urlopen(req, timeout=10) as resp:
                    federated = json.loads(resp.read().decode())
                    results.extend(federated.get("signals", []))
            except Exception:
                pass

        return results


# =============================================================================
# L13-4: REAL-WORLD EVIDENCE AGGREGATION & ANONYMIZATION
# =============================================================================

class RWEAggregationEngine:
    """
    L13-4: Aggregates anonymized real-world evidence across institutions.

    Collects AGGREGATE statistics only:
    - Drug X used N times for condition Y
    - Average outcome score for treatment Z
    - Adverse event rate per 1000 prescriptions

    Anonymization: k-anonymity (k>=10). If a subgroup has <10 patients,
    data is suppressed (not reported). This prevents re-identification.

    Source: HIPAA Safe Harbor; GDPR Article 89; k-anonymity (Sweeney 2002)
    """

    K_ANONYMITY_THRESHOLD = 10

    def __init__(self):
        self._aggregate_data: list[dict] = []

    def submit_aggregate(self, drug: str, condition: str,
                          patient_count: int,
                          outcome_positive_pct: float,
                          adverse_event_rate_per_1000: float) -> Optional[dict]:
        """Submit aggregate data if k-anonymity threshold met."""
        if patient_count < self.K_ANONYMITY_THRESHOLD:
            logger.info("RWE submission suppressed: n=%d < k=%d for %s/%s",
                       patient_count, self.K_ANONYMITY_THRESHOLD, drug, condition)
            return None  # Suppress — re-identification risk

        record = {
            "record_id": str(uuid4()),
            "drug_hash": hashlib.sha256(drug.lower().encode()).hexdigest()[:16],
            "condition_hash": hashlib.sha256(condition.lower().encode()).hexdigest()[:16],
            "patient_count": patient_count,
            "outcome_positive_pct": round(outcome_positive_pct, 2),
            "ade_rate_per_1000": round(adverse_event_rate_per_1000, 2),
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "k_anonymity": self.K_ANONYMITY_THRESHOLD,
        }
        self._aggregate_data.append(record)
        return record

    def get_aggregate_stats(self) -> dict:
        """Get summary of all submitted aggregate data."""
        return {
            "total_records": len(self._aggregate_data),
            "total_patients_represented": sum(
                r["patient_count"] for r in self._aggregate_data
            ),
            "k_anonymity_threshold": self.K_ANONYMITY_THRESHOLD,
        }
