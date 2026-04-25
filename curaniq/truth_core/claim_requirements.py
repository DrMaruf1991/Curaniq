"""Claim-type evidence requirements for doctor-grade output."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from curaniq.models.schemas import ClaimType, EvidenceSourceType, EvidenceTier


@dataclass(frozen=True)
class ClaimRequirement:
    claim_type: ClaimType
    min_sources: int
    min_confidence: float
    fail_closed: bool
    allowed_tiers: set[EvidenceTier] = field(default_factory=set)
    required_source_types_any: set[EvidenceSourceType] = field(default_factory=set)
    note: str = ""


CLAIM_REQUIREMENTS: dict[ClaimType, ClaimRequirement] = {
    ClaimType.DOSING: ClaimRequirement(
        ClaimType.DOSING, 1, 0.90, True,
        {EvidenceTier.GUIDELINE, EvidenceTier.SYSTEMATIC_REVIEW, EvidenceTier.RCT},
        {EvidenceSourceType.DAILYMED, EvidenceSourceType.OPENFDA, EvidenceSourceType.NICE, EvidenceSourceType.UZ_MOH, EvidenceSourceType.LICENSED_DB},
        "Dosing requires official label/formulary/guideline-grade source; PubMed alone is insufficient.",
    ),
    ClaimType.CONTRAINDICATION: ClaimRequirement(
        ClaimType.CONTRAINDICATION, 1, 0.90, True,
        {EvidenceTier.GUIDELINE, EvidenceTier.SYSTEMATIC_REVIEW, EvidenceTier.RCT},
        {EvidenceSourceType.DAILYMED, EvidenceSourceType.OPENFDA, EvidenceSourceType.NICE, EvidenceSourceType.LACTMED, EvidenceSourceType.UZ_MOH, EvidenceSourceType.LICENSED_DB},
        "Contraindications require official safety or guideline source.",
    ),
    ClaimType.DRUG_INTERACTION: ClaimRequirement(
        ClaimType.DRUG_INTERACTION, 1, 0.90, True,
        {EvidenceTier.GUIDELINE, EvidenceTier.SYSTEMATIC_REVIEW, EvidenceTier.RCT},
        {EvidenceSourceType.DAILYMED, EvidenceSourceType.OPENFDA, EvidenceSourceType.LICENSED_DB},
        "Drug interactions require official label or licensed interaction knowledge base.",
    ),
    ClaimType.SAFETY_WARNING: ClaimRequirement(
        ClaimType.SAFETY_WARNING, 1, 0.90, True,
        {EvidenceTier.GUIDELINE, EvidenceTier.SYSTEMATIC_REVIEW, EvidenceTier.RCT},
        {EvidenceSourceType.DAILYMED, EvidenceSourceType.OPENFDA, EvidenceSourceType.WHO, EvidenceSourceType.NICE, EvidenceSourceType.LACTMED},
        "Safety warnings require regulator/guideline source.",
    ),
    ClaimType.SAFETY_SIGNAL: ClaimRequirement(
        ClaimType.SAFETY_SIGNAL, 1, 0.80, False,
        {EvidenceTier.GUIDELINE, EvidenceTier.SYSTEMATIC_REVIEW, EvidenceTier.RCT, EvidenceTier.COHORT},
        {EvidenceSourceType.PUBMED, EvidenceSourceType.OPENFDA, EvidenceSourceType.DAILYMED, EvidenceSourceType.WHO, EvidenceSourceType.NICE},
    ),
    ClaimType.EFFICACY: ClaimRequirement(
        ClaimType.EFFICACY, 1, 0.80, False,
        {EvidenceTier.GUIDELINE, EvidenceTier.SYSTEMATIC_REVIEW, EvidenceTier.RCT, EvidenceTier.NEGATIVE_TRIAL},
        {EvidenceSourceType.PUBMED, EvidenceSourceType.NICE, EvidenceSourceType.WHO, EvidenceSourceType.CLINICALTRIALS},
    ),
    ClaimType.DIAGNOSTIC: ClaimRequirement(
        ClaimType.DIAGNOSTIC, 1, 0.80, False,
        {EvidenceTier.GUIDELINE, EvidenceTier.SYSTEMATIC_REVIEW, EvidenceTier.RCT, EvidenceTier.COHORT},
        {EvidenceSourceType.PUBMED, EvidenceSourceType.NICE, EvidenceSourceType.WHO, EvidenceSourceType.UZ_MOH},
    ),
    ClaimType.MONITORING: ClaimRequirement(
        ClaimType.MONITORING, 1, 0.80, False,
        {EvidenceTier.GUIDELINE, EvidenceTier.SYSTEMATIC_REVIEW, EvidenceTier.RCT, EvidenceTier.COHORT},
        {EvidenceSourceType.PUBMED, EvidenceSourceType.DAILYMED, EvidenceSourceType.OPENFDA, EvidenceSourceType.NICE, EvidenceSourceType.UZ_MOH},
    ),
    ClaimType.PROGNOSIS: ClaimRequirement(
        ClaimType.PROGNOSIS, 1, 0.70, False,
        {EvidenceTier.SYSTEMATIC_REVIEW, EvidenceTier.RCT, EvidenceTier.COHORT},
        {EvidenceSourceType.PUBMED, EvidenceSourceType.CLINICALTRIALS},
    ),
    ClaimType.GENERAL: ClaimRequirement(
        ClaimType.GENERAL, 1, 0.70, False,
        {EvidenceTier.GUIDELINE, EvidenceTier.SYSTEMATIC_REVIEW, EvidenceTier.RCT, EvidenceTier.COHORT, EvidenceTier.EXPERT_OPINION},
        {EvidenceSourceType.PUBMED, EvidenceSourceType.NICE, EvidenceSourceType.WHO, EvidenceSourceType.DAILYMED, EvidenceSourceType.OPENFDA},
    ),
    ClaimType.UNKNOWN: ClaimRequirement(ClaimType.UNKNOWN, 1, 0.90, True, set(), set(), "Unknown clinical claim types fail closed."),
}


_HIGH_RISK_PATTERNS: list[tuple[re.Pattern[str], ClaimType]] = [
    (re.compile(r"\b(dose|dosing|mg|mcg|units?|IU|per\s*kg|renal dose|eGFR|CrCl)\b", re.I), ClaimType.DOSING),
    (re.compile(r"\b(contraindicated|avoid|do not use|black box|boxed warning|teratogenic|pregnan|breastfeed|lactation)\b", re.I), ClaimType.CONTRAINDICATION),
    (re.compile(r"\b(interaction|interacts|combined with|coadminister|co-administer|QT|torsade|CYP)\b", re.I), ClaimType.DRUG_INTERACTION),
]


def infer_claim_type_from_query(text: str) -> ClaimType:
    for pattern, claim_type in _HIGH_RISK_PATTERNS:
        if pattern.search(text or ""):
            return claim_type
    return ClaimType.GENERAL


def is_high_risk_claim_type(claim_type: ClaimType) -> bool:
    req = CLAIM_REQUIREMENTS.get(claim_type)
    return bool(req and req.fail_closed)
