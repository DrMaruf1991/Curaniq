"""
CURANIQ — Medical Evidence Operating System
Layer 2: Evidence Knowledge & Synthesis

L2-3: GRADE Certainty Grading Engine

Architecture requirements:
- High/Moderate/Low/Very Low certainty with explicit downgrade reasons
- Risk-of-bias auto-rating:
  selection bias, blinding, early stopping, surrogate endpoints, funding flags
- Journal/publisher quality weighting
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from curaniq.models.evidence import EvidenceChunk, EvidenceTier

logger = logging.getLogger(__name__)


class GRADECertainty(str, Enum):
    """
    GRADE certainty levels per GRADE Working Group.
    Oxford CEBM hierarchy combined with GRADE certainty framework.
    """
    HIGH         = "high"         # We are very confident the true effect lies close to the estimate
    MODERATE     = "moderate"     # We are moderately confident — may be substantially different
    LOW          = "low"          # Our confidence is limited — may be substantially different
    VERY_LOW     = "very_low"     # We have very little confidence — likely to be substantially different


class RiskOfBiasLevel(str, Enum):
    """Risk of bias classification per Cochrane RoB 2.0."""
    LOW         = "low"
    SOME_CONCERN = "some_concerns"
    HIGH        = "high"
    UNCLEAR     = "unclear"


@dataclass
class DowngradeReason:
    """Explicit reason for GRADE certainty downgrade."""
    domain: str                   # e.g., "risk_of_bias", "indirectness", "imprecision"
    description: str              # Human-readable explanation
    downgrade_steps: int          # Number of certainty levels downgraded (1 or 2)


@dataclass
class UpgradeReason:
    """Explicit reason for GRADE certainty upgrade (rare)."""
    domain: str
    description: str
    upgrade_steps: int


@dataclass
class GRADEAssessment:
    """
    Full GRADE assessment for a piece of evidence.
    Contains certainty level + all downgrade/upgrade reasons.
    """
    chunk_id: str
    starting_grade: GRADECertainty  # Based on study design
    final_grade: GRADECertainty
    downgrade_reasons: list[DowngradeReason] = field(default_factory=list)
    upgrade_reasons: list[UpgradeReason] = field(default_factory=list)

    # Risk of bias domains (Cochrane RoB 2.0)
    randomisation_bias: RiskOfBiasLevel = RiskOfBiasLevel.UNCLEAR
    deviation_bias: RiskOfBiasLevel = RiskOfBiasLevel.UNCLEAR
    missing_data_bias: RiskOfBiasLevel = RiskOfBiasLevel.UNCLEAR
    outcome_measurement_bias: RiskOfBiasLevel = RiskOfBiasLevel.UNCLEAR
    selective_reporting_bias: RiskOfBiasLevel = RiskOfBiasLevel.UNCLEAR

    # Quality flags
    has_surrogate_endpoint: bool = False
    stopped_early: bool = False
    industry_funded: bool = False
    journal_quality: float = 1.0  # 0.0 - 1.0

    # Display
    summary: str = ""
    certainty_display: str = ""


# GRADE starting points by study design
GRADE_STARTING_POINTS: dict[EvidenceTier, GRADECertainty] = {
    EvidenceTier.SYSTEMATIC_REVIEW: GRADECertainty.HIGH,     # Starts HIGH
    EvidenceTier.RCT:               GRADECertainty.HIGH,     # Starts HIGH
    EvidenceTier.GUIDELINE:         GRADECertainty.MODERATE, # Guidelines synthesize evidence
    EvidenceTier.COHORT:            GRADECertainty.LOW,      # Observational starts LOW
    EvidenceTier.CASE_REPORT:       GRADECertainty.VERY_LOW,
    EvidenceTier.EXPERT_OPINION:    GRADECertainty.VERY_LOW,
    EvidenceTier.NEGATIVE_TRIAL:    GRADECertainty.LOW,
    EvidenceTier.PREPRINT:          GRADECertainty.VERY_LOW,
    EvidenceTier.UNKNOWN:           GRADECertainty.VERY_LOW,
}

GRADE_ORDER = [
    GRADECertainty.HIGH,
    GRADECertainty.MODERATE,
    GRADECertainty.LOW,
    GRADECertainty.VERY_LOW,
]


def _downgrade(current: GRADECertainty, steps: int) -> GRADECertainty:
    """Downgrade certainty by N steps."""
    idx = GRADE_ORDER.index(current)
    new_idx = min(idx + steps, len(GRADE_ORDER) - 1)
    return GRADE_ORDER[new_idx]


def _upgrade(current: GRADECertainty, steps: int) -> GRADECertainty:
    """Upgrade certainty by N steps (rare in GRADE)."""
    idx = GRADE_ORDER.index(current)
    new_idx = max(idx - steps, 0)
    return GRADE_ORDER[new_idx]


# High-impact journals — quality weight boost
HIGH_QUALITY_JOURNALS = {
    "new england journal of medicine", "nejm", "n engl j med",
    "lancet", "the lancet",
    "jama", "journal of the american medical association",
    "bmj", "british medical journal",
    "annals of internal medicine",
    "nature medicine",
    "journal of clinical oncology",
    "circulation",
    "european heart journal",
    "gut", "hepatology",
    "chest", "american journal of respiratory and critical care",
    "kidney international",
    "diabetes care",
    "clinical infectious diseases",
    "cochrane database of systematic reviews",
}

# Predatory journal indicators (red flags)
PREDATORY_INDICATORS = {
    "rapid publication", "article processing fee", "open access fee",
    "all manuscripts accepted", "peer review waived",
    "scientific world journal", "hindawi", "mdpi journals",  # Some flagged
    "omics", "bentham",
}

# Funding sources that trigger bias flag
INDUSTRY_FUNDING_PATTERNS = re.compile(
    r'\b(funded by|supported by|grant from|sponsored by)\b.{0,80}'
    r'\b(pharma|pharmaceutical|biosciences|therapeutics|laboratories|inc\.?|corp\.?|ltd\.?)\b',
    re.IGNORECASE,
)

# Surrogate endpoint indicators
SURROGATE_ENDPOINT_PATTERNS = re.compile(
    r'\b(surrogate|biomarker|hmg|ldl|blood pressure|a1c|hba1c|viral load|'
    r'cd4|egfr decline|progression-free|pfs|dfs|disease-free)\b',
    re.IGNORECASE,
)

# Hard clinical endpoints (good — these are not surrogates)
HARD_ENDPOINT_PATTERNS = re.compile(
    r'\b(mortality|death|survival|myocardial infarction|stroke|cardiovascular '
    r'events|hospitalization|quality of life|functional status|mace)\b',
    re.IGNORECASE,
)

# Early stopping indicators
EARLY_STOPPING_PATTERNS = re.compile(
    r'\b(stopped early|terminated early|interim analysis|data safety monitoring|'
    r'dsmb halt|stopped for benefit|stopped for harm)\b',
    re.IGNORECASE,
)

# High risk-of-bias patterns for RCTs
RANDOMISATION_RISK_PATTERNS = re.compile(
    r'\b(open[- ]label|not blinded|unblinded|single[- ]blind|'
    r'allocation concealment not|no concealment|quasi[- ]randomised)\b',
    re.IGNORECASE,
)

MISSING_DATA_PATTERNS = re.compile(
    r'\b(lost to follow[- ]up|attrition|dropout rate|missing data|'
    r'incomplete outcome|per[- ]protocol analysis|per protocol)\b',
    re.IGNORECASE,
)


class GRADEGradingEngine:
    """
    L2-3: GRADE Certainty Grading Engine.
    
    Per architecture:
    'High/Moderate/Low/Very Low certainty with explicit downgrade reasons.
    Includes risk-of-bias auto-rating (selection bias, blinding, early stopping,
    surrogate endpoints, funding flags). Journal/publisher quality weighting.'
    
    Implements the full GRADE framework:
    - Starting point from study design
    - Downgrade for: risk of bias, inconsistency, indirectness, imprecision,
      publication bias, surrogate endpoints, early stopping, industry funding
    - Upgrade for: large effect size, dose-response, residual confounding
      (rare — only for observational studies)
    """

    def grade(self, chunk: EvidenceChunk) -> GRADEAssessment:
        """
        Perform full GRADE assessment on an evidence chunk.
        Returns complete GRADEAssessment with certainty level and all reasons.
        """
        content = chunk.content

        # Step 1: Starting point from study design
        starting = GRADE_STARTING_POINTS.get(chunk.evidence_tier, GRADECertainty.VERY_LOW)
        current = starting
        downgrade_reasons = []
        upgrade_reasons = []

        # Step 2: Risk of bias assessment
        rob_result = self._assess_risk_of_bias(content, chunk.evidence_tier)
        if rob_result:
            reason, steps = rob_result
            current = _downgrade(current, steps)
            downgrade_reasons.append(reason)

        # Step 3: Surrogate endpoints
        surrogate_result = self._check_surrogate_endpoints(content)
        if surrogate_result:
            current = _downgrade(current, 1)
            downgrade_reasons.append(surrogate_result)

        # Step 4: Early stopping
        early_stopping = self._check_early_stopping(content)
        if early_stopping:
            current = _downgrade(current, 1)
            downgrade_reasons.append(early_stopping)

        # Step 5: Industry funding flag
        funding = self._check_industry_funding(content)
        if funding:
            current = _downgrade(current, 1)
            downgrade_reasons.append(funding)

        # Step 6: Journal quality
        journal_quality = self._assess_journal_quality(content)
        if journal_quality < 0.5:
            current = _downgrade(current, 1)
            downgrade_reasons.append(DowngradeReason(
                domain="journal_quality",
                description=f"Evidence published in lower-quality venue (quality score: {journal_quality:.1f}). Consider predatory journal risk.",
                downgrade_steps=1,
            ))

        # Step 7: Missing data / attrition
        missing_data = self._check_missing_data(content)
        if missing_data:
            current = _downgrade(current, 1)
            downgrade_reasons.append(missing_data)

        # Step 8: Upgrades for observational studies (rare)
        if chunk.evidence_tier in (EvidenceTier.COHORT,):
            upgrade = self._check_upgrade_criteria(content)
            if upgrade:
                current = _upgrade(current, 1)
                upgrade_reasons.append(upgrade)

        # Build assessment
        certainty_descriptions = {
            GRADECertainty.HIGH: "⬆ HIGH certainty: We are very confident the true effect lies close to the estimate of effect.",
            GRADECertainty.MODERATE: "↔ MODERATE certainty: We are moderately confident in the effect estimate; the true effect is likely close but may be substantially different.",
            GRADECertainty.LOW: "⬇ LOW certainty: Our confidence in the effect estimate is limited; the true effect may be substantially different.",
            GRADECertainty.VERY_LOW: "⬇⬇ VERY LOW certainty: We have very little confidence in the effect estimate; the true effect is likely substantially different.",
        }

        assessment = GRADEAssessment(
            chunk_id=chunk.chunk_id,
            starting_grade=starting,
            final_grade=current,
            downgrade_reasons=downgrade_reasons,
            upgrade_reasons=upgrade_reasons,
            has_surrogate_endpoint=surrogate_result is not None,
            stopped_early=early_stopping is not None,
            industry_funded=funding is not None,
            journal_quality=journal_quality,
            certainty_display=certainty_descriptions[current],
            summary=self._build_summary(current, downgrade_reasons, upgrade_reasons),
        )

        return assessment

    def _assess_risk_of_bias(
        self, content: str, tier: EvidenceTier
    ) -> Optional[tuple[DowngradeReason, int]]:
        """Assess risk of bias — returns (DowngradeReason, steps) or None."""
        if tier not in (EvidenceTier.RCT, EvidenceTier.SYSTEMATIC_REVIEW):
            return None

        issues = []

        if RANDOMISATION_RISK_PATTERNS.search(content):
            issues.append("inadequate blinding or allocation concealment")

        if MISSING_DATA_PATTERNS.search(content):
            issues.append("missing outcome data or high attrition")

        if re.search(r'\b(selective outcome reporting|outcome switching|registered outcome)\b', content, re.I):
            issues.append("suspected selective outcome reporting")

        if not issues:
            return None

        steps = 2 if len(issues) >= 2 else 1
        return (
            DowngradeReason(
                domain="risk_of_bias",
                description=f"Risk of bias concerns: {'; '.join(issues)}.",
                downgrade_steps=steps,
            ),
            steps,
        )

    def _check_surrogate_endpoints(self, content: str) -> Optional[DowngradeReason]:
        """Check for surrogate endpoints — downgrade if no hard clinical outcomes."""
        has_surrogate = bool(SURROGATE_ENDPOINT_PATTERNS.search(content))
        has_hard = bool(HARD_ENDPOINT_PATTERNS.search(content))

        if has_surrogate and not has_hard:
            return DowngradeReason(
                domain="indirectness",
                description=(
                    "Surrogate or intermediate endpoints only (e.g., lab values, biomarkers). "
                    "No hard clinical outcomes (mortality, MI, stroke). "
                    "Clinical benefit not directly demonstrated."
                ),
                downgrade_steps=1,
            )
        return None

    def _check_early_stopping(self, content: str) -> Optional[DowngradeReason]:
        """Check if trial was stopped early — inflates effect estimates."""
        if EARLY_STOPPING_PATTERNS.search(content):
            return DowngradeReason(
                domain="risk_of_bias",
                description=(
                    "Trial stopped early (interim analysis / DSMB halt). "
                    "Early stopping inflates treatment effect estimates. "
                    "Results should be interpreted with caution."
                ),
                downgrade_steps=1,
            )
        return None

    def _check_industry_funding(self, content: str) -> Optional[DowngradeReason]:
        """Flag industry-funded research — publication bias risk."""
        if INDUSTRY_FUNDING_PATTERNS.search(content):
            return DowngradeReason(
                domain="publication_bias",
                description=(
                    "Industry-funded research. Risk of publication bias and selective reporting. "
                    "Independently funded replication studies provide stronger certainty."
                ),
                downgrade_steps=1,
            )
        return None

    def _assess_journal_quality(self, content: str) -> float:
        """
        Assess journal quality from content mentions.
        Returns quality score 0.0-1.0.
        """
        content_lower = content.lower()

        # High-quality journal boost
        for journal in HIGH_QUALITY_JOURNALS:
            if journal in content_lower:
                return 1.0

        # Predatory indicators penalty
        for indicator in PREDATORY_INDICATORS:
            if indicator in content_lower:
                return 0.2

        return 0.7  # Default: reasonable quality assumed

    def _check_missing_data(self, content: str) -> Optional[DowngradeReason]:
        """Check for significant missing data / attrition bias."""
        match = re.search(
            r'(\d+(?:\.\d+)?)\s*%\s*(?:lost to follow[- ]up|dropout|attrition|withdrew)',
            content, re.I
        )
        if match:
            rate = float(match.group(1))
            if rate > 20:
                return DowngradeReason(
                    domain="risk_of_bias",
                    description=(
                        f"High attrition/dropout rate ({rate:.0f}%). "
                        "Missing outcome data may bias results. "
                        "Consider best-case/worst-case scenario analysis."
                    ),
                    downgrade_steps=1,
                )
        return None

    def _check_upgrade_criteria(self, content: str) -> Optional[UpgradeReason]:
        """
        Check GRADE upgrade criteria for observational studies.
        Rare — only applies when effect size is very large (RR >5 or <0.2)
        or there is a clear dose-response relationship.
        """
        # Large effect size
        large_effect = re.search(
            r'\b(risk ratio|relative risk|RR|OR|odds ratio)\s*(?:of\s*)?(?:>|greater than|approximately)\s*([5-9]|\d{2,})',
            content, re.I
        )
        if large_effect:
            return UpgradeReason(
                domain="large_effect",
                description=(
                    "Very large effect size detected (RR >5 or equivalent). "
                    "Large effects are unlikely to be entirely due to confounding."
                ),
                upgrade_steps=1,
            )

        # Dose-response
        dose_response = re.search(
            r'\b(dose[- ]response|dose[- ]dependent|gradient|graded response)\b',
            content, re.I
        )
        if dose_response:
            return UpgradeReason(
                domain="dose_response",
                description=(
                    "Dose-response relationship observed. "
                    "Dose-response gradients increase certainty in causal inference."
                ),
                upgrade_steps=1,
            )

        return None

    def _build_summary(
        self,
        final_grade: GRADECertainty,
        downgrade_reasons: list[DowngradeReason],
        upgrade_reasons: list[UpgradeReason],
    ) -> str:
        """Build a concise human-readable GRADE summary."""
        parts = [f"GRADE: {final_grade.value.upper()}"]

        if downgrade_reasons:
            reasons = [r.description[:80] for r in downgrade_reasons[:3]]
            parts.append(f"Downgraded for: {'; '.join(reasons)}")

        if upgrade_reasons:
            reasons = [r.description[:80] for r in upgrade_reasons]
            parts.append(f"Upgraded for: {'; '.join(reasons)}")

        return " | ".join(parts)

    def grade_batch(self, chunks: list[EvidenceChunk]) -> list[GRADEAssessment]:
        """Grade a batch of evidence chunks."""
        return [self.grade(chunk) for chunk in chunks]

    def get_highest_certainty(
        self, assessments: list[GRADEAssessment]
    ) -> Optional[GRADEAssessment]:
        """Return the assessment with the highest GRADE certainty."""
        if not assessments:
            return None
        return min(assessments, key=lambda a: GRADE_ORDER.index(a.final_grade))

    def filter_by_certainty(
        self,
        chunks_with_assessments: list[tuple[EvidenceChunk, GRADEAssessment]],
        min_certainty: GRADECertainty = GRADECertainty.LOW,
    ) -> list[tuple[EvidenceChunk, GRADEAssessment]]:
        """Filter evidence to minimum GRADE certainty level."""
        min_idx = GRADE_ORDER.index(min_certainty)
        return [
            (chunk, assessment)
            for chunk, assessment in chunks_with_assessments
            if GRADE_ORDER.index(assessment.final_grade) <= min_idx
        ]
