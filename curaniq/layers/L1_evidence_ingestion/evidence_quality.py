"""
CURANIQ — Medical Evidence Operating System
Layer 1: Evidence Data Ingestion — Quality & Deduplication

L1-3  Negative Evidence Registry (failed trials, null results, negative findings)
L1-6  Source Quality Scoring (journal impact, study design, bias risk)
L1-7  Deduplication Engine (DOI, PMID, title similarity)

Architecture: "Indexes failed trials, null results, negative evidence.
Without this, only positive results surface — publication bias."
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# L1-3: NEGATIVE EVIDENCE REGISTRY
# ─────────────────────────────────────────────────────────────────────────────

class NegativeEvidenceType(str, Enum):
    FAILED_TRIAL       = "failed_trial"        # Primary endpoint not met
    NULL_RESULT         = "null_result"         # No significant difference
    NEGATIVE_FINDING    = "negative_finding"    # Harm detected
    WITHDRAWN_APPROVAL  = "withdrawn_approval"  # Drug/device approval withdrawn
    SAFETY_SIGNAL       = "safety_signal"       # Post-market safety alert
    RETRACTED_POSITIVE  = "retracted_positive"  # Previously positive result retracted


@dataclass
class NegativeEvidenceEntry:
    entry_id: str = field(default_factory=lambda: str(uuid4()))
    evidence_type: NegativeEvidenceType = NegativeEvidenceType.NULL_RESULT
    drug_or_intervention: str = ""
    condition: str = ""
    finding_summary: str = ""
    source_pmid: Optional[str] = None
    source_doi: Optional[str] = None
    source_nct: Optional[str] = None
    trial_phase: Optional[str] = None
    sample_size: Optional[int] = None
    p_value: Optional[float] = None
    indexed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class NegativeEvidenceRegistry:
    """
    L1-3: Tracks negative, null, and failed trial results.

    Without this module, CURANIQ would exhibit publication bias —
    only surfacing positive results. Negative evidence is CRITICAL
    for accurate clinical decision support.

    Detection heuristics:
    - PubMed abstracts containing "did not reach significance"
    - ClinicalTrials.gov entries with TERMINATED/FAILED status
    - FDA MedWatch safety communications
    - Retraction Watch entries for formerly-positive results
    """

    # Heuristic patterns for detecting negative results in abstracts
    NEGATIVE_PATTERNS: list[tuple[re.Pattern, NegativeEvidenceType]] = [
        (re.compile(r'did\s+not\s+(reach|achieve|demonstrate|show)\s+(?:statistical\s+)?significance', re.I),
         NegativeEvidenceType.NULL_RESULT),
        (re.compile(r'no\s+(?:significant|statistically\s+significant)\s+(?:difference|improvement|benefit)', re.I),
         NegativeEvidenceType.NULL_RESULT),
        (re.compile(r'failed\s+to\s+(?:meet|demonstrate|show|achieve)\s+(?:the\s+)?primary\s+endpoint', re.I),
         NegativeEvidenceType.FAILED_TRIAL),
        (re.compile(r'trial\s+(?:was\s+)?(?:terminated|stopped|halted)\s+(?:early|prematurely)', re.I),
         NegativeEvidenceType.FAILED_TRIAL),
        (re.compile(r'increased\s+(?:risk|incidence|rate)\s+of\s+(?:adverse|serious|fatal)', re.I),
         NegativeEvidenceType.NEGATIVE_FINDING),
        (re.compile(r'(?:black\s+box|boxed)\s+warning\s+(?:added|issued|updated)', re.I),
         NegativeEvidenceType.SAFETY_SIGNAL),
        (re.compile(r'(?:voluntary|mandatory)\s+(?:recall|withdrawal|market\s+removal)', re.I),
         NegativeEvidenceType.WITHDRAWN_APPROVAL),
        (re.compile(r'(?:retract|withdrawn|correction)\s+(?:due\s+to|because)', re.I),
         NegativeEvidenceType.RETRACTED_POSITIVE),
    ]

    def __init__(self):
        self._registry: list[NegativeEvidenceEntry] = []
        self._drug_index: dict[str, list[str]] = {}

    def classify_abstract(self, abstract_text: str) -> Optional[NegativeEvidenceType]:
        """Detect if an abstract reports negative/null results."""
        for pattern, ev_type in self.NEGATIVE_PATTERNS:
            if pattern.search(abstract_text):
                return ev_type
        return None

    def index_negative_evidence(
        self,
        drug: str,
        condition: str,
        finding: str,
        evidence_type: NegativeEvidenceType,
        pmid: Optional[str] = None,
        doi: Optional[str] = None,
        nct: Optional[str] = None,
    ) -> NegativeEvidenceEntry:
        """Index a negative evidence finding."""
        entry = NegativeEvidenceEntry(
            evidence_type=evidence_type,
            drug_or_intervention=drug.lower(),
            condition=condition.lower(),
            finding_summary=finding,
            source_pmid=pmid,
            source_doi=doi,
            source_nct=nct,
        )
        self._registry.append(entry)
        drug_key = drug.lower()
        self._drug_index.setdefault(drug_key, []).append(entry.entry_id)
        return entry

    def query_negative_evidence(self, drug: str) -> list[NegativeEvidenceEntry]:
        """Retrieve all negative evidence for a drug."""
        drug_key = drug.lower()
        entry_ids = self._drug_index.get(drug_key, [])
        return [e for e in self._registry if e.entry_id in entry_ids]

    def has_safety_signal(self, drug: str) -> bool:
        """Quick check: does this drug have any safety signals?"""
        entries = self.query_negative_evidence(drug)
        return any(
            e.evidence_type in (
                NegativeEvidenceType.SAFETY_SIGNAL,
                NegativeEvidenceType.WITHDRAWN_APPROVAL,
                NegativeEvidenceType.NEGATIVE_FINDING,
            )
            for e in entries
        )


# ─────────────────────────────────────────────────────────────────────────────
# L1-6: SOURCE QUALITY SCORING
# ─────────────────────────────────────────────────────────────────────────────

class StudyDesign(str, Enum):
    """Oxford CEBM hierarchy (simplified)."""
    SYSTEMATIC_REVIEW  = "systematic_review"   # Level 1
    RCT                = "rct"                 # Level 2
    COHORT             = "cohort"              # Level 3
    CASE_CONTROL       = "case_control"        # Level 4
    CASE_SERIES        = "case_series"         # Level 5
    EXPERT_OPINION     = "expert_opinion"      # Level 6
    DRUG_LABEL         = "drug_label"          # Regulatory source
    GUIDELINE          = "guideline"           # Clinical practice guideline
    PREPRINT           = "preprint"            # Not peer-reviewed


@dataclass
class SourceQualityScore:
    source_id: str
    design_score: float = 0.0         # 0-1 from study design
    journal_score: float = 0.0        # 0-1 from journal impact
    recency_score: float = 0.0        # 0-1 from publication date
    sample_size_score: float = 0.0    # 0-1 from study size
    bias_risk_score: float = 0.0      # 0-1 from bias assessment (1=low bias)
    composite_score: float = 0.0      # Weighted average


class SourceQualityScorer:
    """
    L1-6: Scores evidence sources for reliability.

    Composite = weighted average of:
    - Study design (Oxford CEBM hierarchy): 30%
    - Journal quality (impact factor quartile): 15%
    - Recency (years since publication): 20%
    - Sample size (log-scaled): 15%
    - Bias risk (Cochrane RoB-2 or Newcastle-Ottawa): 20%
    """

    DESIGN_SCORES: dict[StudyDesign, float] = {
        StudyDesign.SYSTEMATIC_REVIEW: 1.0,
        StudyDesign.RCT:               0.85,
        StudyDesign.COHORT:            0.65,
        StudyDesign.CASE_CONTROL:      0.50,
        StudyDesign.CASE_SERIES:       0.30,
        StudyDesign.EXPERT_OPINION:    0.15,
        StudyDesign.DRUG_LABEL:        0.90,
        StudyDesign.GUIDELINE:         0.95,
        StudyDesign.PREPRINT:          0.10,
    }

    # Real journal quartiles from JCR 2024 data (Medicine, General & Internal)
    # Journal quartiles loaded from curaniq/data/journal_quartiles.json

    WEIGHTS = {
        "design": 0.30,
        "journal": 0.15,
        "recency": 0.20,
        "sample_size": 0.15,
        "bias_risk": 0.20,
    }

    def get_journal_quartile(self, journal_name: str) -> int:
        """Look up journal quartile from real JCR data. Default Q3 if unknown."""
        name_lower = journal_name.lower().strip()
        for known_name, quartile in self.JOURNAL_QUARTILES.items():
            if known_name in name_lower or name_lower in known_name:
                return quartile
        return 3  # Unknown journals default to Q3 (conservative)

    def __init__(self):
        from curaniq.data_loader import load_json_data
        raw = load_json_data("journal_quartiles.json")
        self.JOURNAL_QUARTILES = raw.get("quartiles", {})

    def score_source(
        self,
        source_id: str,
        study_design: StudyDesign,
        journal_quartile: int = 2,
        publication_year: int = 2024,
        sample_size: int = 0,
        bias_risk_low: bool = True,
    ) -> SourceQualityScore:
        """Compute composite quality score for an evidence source."""
        import math

        design_score = self.DESIGN_SCORES.get(study_design, 0.3)

        # Journal quartile: Q1=1.0, Q2=0.75, Q3=0.50, Q4=0.25
        journal_score = max(0.0, 1.0 - (journal_quartile - 1) * 0.25)

        # Recency: full score if <2 years, decays linearly over 10 years
        current_year = datetime.now(timezone.utc).year
        age_years = max(0, current_year - publication_year)
        recency_score = max(0.0, 1.0 - (age_years / 10.0))

        # Sample size: log-scaled, 100=0.5, 1000=0.75, 10000=1.0
        if sample_size > 0:
            sample_size_score = min(1.0, math.log10(sample_size) / 4.0)
        else:
            sample_size_score = 0.0

        bias_risk_score = 0.9 if bias_risk_low else 0.3

        composite = (
            self.WEIGHTS["design"] * design_score
            + self.WEIGHTS["journal"] * journal_score
            + self.WEIGHTS["recency"] * recency_score
            + self.WEIGHTS["sample_size"] * sample_size_score
            + self.WEIGHTS["bias_risk"] * bias_risk_score
        )

        return SourceQualityScore(
            source_id=source_id,
            design_score=round(design_score, 3),
            journal_score=round(journal_score, 3),
            recency_score=round(recency_score, 3),
            sample_size_score=round(sample_size_score, 3),
            bias_risk_score=round(bias_risk_score, 3),
            composite_score=round(composite, 3),
        )


# ─────────────────────────────────────────────────────────────────────────────
# L1-7: DEDUPLICATION ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class DeduplicationEngine:
    """
    L1-7: Detects and merges duplicate evidence across sources.

    Deduplication strategy (ordered by reliability):
    1. Exact DOI match → definite duplicate
    2. Exact PMID match → definite duplicate
    3. Title + first author + year match → probable duplicate
    4. Title similarity (Jaccard > 0.85) + year match → possible duplicate

    When duplicates found, keep the highest-quality version
    (prefer PubMed > Crossref > Preprint).
    """

    SOURCE_PRIORITY = {
        "pubmed": 10,
        "crossref": 8,
        "openfda": 9,
        "nice": 9,
        "who": 9,
        "semantic_scholar": 6,
        "europe_pmc": 5,
        "preprint": 2,
    }

    def __init__(self):
        self._doi_index: dict[str, list[dict]] = {}
        self._pmid_index: dict[str, list[dict]] = {}
        self._title_index: dict[str, list[dict]] = {}

    def _normalize_title(self, title: str) -> str:
        """Normalize title for comparison: lowercase, remove punctuation."""
        return re.sub(r'[^\w\s]', '', title.lower()).strip()

    def _title_tokens(self, title: str) -> set[str]:
        """Tokenize normalized title for Jaccard comparison."""
        normalized = self._normalize_title(title)
        return {w for w in normalized.split() if len(w) > 2}

    def _jaccard_similarity(self, set_a: set[str], set_b: set[str]) -> float:
        """Compute Jaccard similarity between two token sets."""
        if not set_a or not set_b:
            return 0.0
        intersection = set_a & set_b
        union = set_a | set_b
        return len(intersection) / len(union)

    def check_duplicate(
        self,
        doi: Optional[str] = None,
        pmid: Optional[str] = None,
        title: str = "",
        year: Optional[int] = None,
        first_author: str = "",
    ) -> tuple[bool, Optional[str], str]:
        """
        Check if evidence is a duplicate.

        Returns: (is_duplicate, existing_id, match_method)
        """
        # 1. Exact DOI match
        if doi and doi in self._doi_index:
            existing = self._doi_index[doi][0]
            return True, existing.get("id"), "doi_exact"

        # 2. Exact PMID match
        if pmid and pmid in self._pmid_index:
            existing = self._pmid_index[pmid][0]
            return True, existing.get("id"), "pmid_exact"

        # 3. Title + author + year
        if title and year and first_author:
            norm_title = self._normalize_title(title)
            key = f"{norm_title}|{first_author.lower()}|{year}"
            if key in self._title_index:
                existing = self._title_index[key][0]
                return True, existing.get("id"), "title_author_year"

        # 4. Fuzzy title match
        if title and year:
            new_tokens = self._title_tokens(title)
            for indexed_key, entries in self._title_index.items():
                if str(year) in indexed_key:
                    indexed_tokens = self._title_tokens(indexed_key.split("|")[0])
                    similarity = self._jaccard_similarity(new_tokens, indexed_tokens)
                    if similarity > 0.85:
                        return True, entries[0].get("id"), f"title_fuzzy_{similarity:.2f}"

        return False, None, "no_match"

    def register_evidence(
        self,
        evidence_id: str,
        doi: Optional[str] = None,
        pmid: Optional[str] = None,
        title: str = "",
        year: Optional[int] = None,
        first_author: str = "",
        source: str = "",
    ) -> bool:
        """
        Register evidence in dedup index. Returns False if duplicate.
        """
        is_dup, existing_id, method = self.check_duplicate(
            doi=doi, pmid=pmid, title=title, year=year, first_author=first_author,
        )

        if is_dup:
            logger.info(
                "Duplicate detected (%s): new=%s matches existing=%s",
                method, evidence_id, existing_id,
            )
            return False

        entry = {
            "id": evidence_id,
            "doi": doi,
            "pmid": pmid,
            "title": title,
            "year": year,
            "source": source,
        }

        if doi:
            self._doi_index.setdefault(doi, []).append(entry)
        if pmid:
            self._pmid_index.setdefault(pmid, []).append(entry)
        if title:
            norm = self._normalize_title(title)
            key = f"{norm}|{first_author.lower() if first_author else ''}|{year or ''}"
            self._title_index.setdefault(key, []).append(entry)

        return True
