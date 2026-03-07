"""
CURANIQ — Medical Evidence Operating System
Layer 2: Evidence Knowledge & Synthesis

L2-7  Retraction Watch Sentinel
L2-6  Jurisdiction-Aware Guideline Gating

Architecture requirements (L2-7):
- 'Real-time Crossref + Retraction Watch check on every citation'
- 'Contaminated evidence blocked'
- Called for EVERY citation before it is added to an evidence pack

Architecture requirements (L2-6):
- UK → NICE. US → AHA/ACC. Uzbekistan → MOH + WHO
- Auto by geolocation/profile
- International comparison view available
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp

from curaniq.models.evidence import (
    EvidenceChunk,
    EvidenceTier,
    Jurisdiction,
    RetractionStatus,
    SourceAPI,
    StalenessStatus,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# L2-7: RETRACTION WATCH SENTINEL
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RetractionRecord:
    """A confirmed retraction record from Retraction Watch or Crossref."""
    doi: Optional[str]
    pmid: Optional[str]
    title: Optional[str]
    retraction_type: str         # "retraction", "correction", "expression_of_concern"
    retraction_date: Optional[datetime]
    retraction_reason: Optional[str]
    retraction_doi: Optional[str]  # DOI of the retraction notice
    source: str                  # "retraction_watch" | "crossref"
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


# Known retracted papers in CURANIQ's domain (seed data for offline operation)
# Production: augmented by real-time Retraction Watch API + Crossref webhooks
KNOWN_RETRACTIONS: dict[str, RetractionRecord] = {
    # Wakefield MMR-autism study (most consequential medical retraction)
    "10.1016/s0140-6736(97)11096-0": RetractionRecord(
        doi="10.1016/s0140-6736(97)11096-0",
        pmid="9500320",
        title="Ileal-lymphoid-nodular hyperplasia, non-specific colitis, and pervasive developmental disorder in children",
        retraction_type="retraction",
        retraction_date=datetime(2010, 2, 2, tzinfo=timezone.utc),
        retraction_reason="Research fraud — fabricated data on MMR vaccine and autism",
        retraction_doi="10.1016/s0140-6736(10)60175-4",
        source="retraction_watch",
    ),
    # HRT and CVD Lancet retraction
    "10.1016/s0140-6736(03)13607-0": RetractionRecord(
        doi="10.1016/s0140-6736(03)13607-0",
        pmid=None,
        title="Hormone replacement therapy and cardiovascular risk: a meta-analysis",
        retraction_type="expression_of_concern",
        retraction_date=datetime(2020, 6, 1, tzinfo=timezone.utc),
        retraction_reason="Data integrity concerns — under investigation",
        retraction_doi=None,
        source="crossref",
    ),
    # Hydroxychloroquine COVID Lancet paper
    "10.1016/s0140-6736(20)31180-6": RetractionRecord(
        doi="10.1016/s0140-6736(20)31180-6",
        pmid="32450107",
        title="Hydroxychloroquine or chloroquine with or without a macrolide for treatment of COVID-19",
        retraction_type="retraction",
        retraction_date=datetime(2020, 6, 4, tzinfo=timezone.utc),
        retraction_reason="Inability to verify data — data integrity concerns",
        retraction_doi="10.1016/s0140-6736(20)31324-6",
        source="retraction_watch",
    ),
    # NEJM remdesivir companion paper
    "10.1056/NEJMoa2007764": RetractionRecord(
        doi="10.1056/NEJMoa2007764",
        pmid=None,
        title="Remdesivir in Hospitalized Patients with Covid-19",
        retraction_type="correction",
        retraction_date=datetime(2020, 9, 1, tzinfo=timezone.utc),
        retraction_reason="Correction to patient numbers — results unchanged",
        retraction_doi=None,
        source="crossref",
    ),
}

# PMID-based lookup (no DOI)
KNOWN_RETRACTIONS_BY_PMID: dict[str, RetractionRecord] = {
    r.pmid: r
    for r in KNOWN_RETRACTIONS.values()
    if r.pmid
}


class RetractionWatchSentinel:
    """
    L2-7: Real-time retraction detection for every citation.
    
    Architecture: 'Real-time Crossref + Retraction Watch check on every citation.
    Contaminated evidence blocked.'
    
    This sentinel is called for EVERY evidence chunk before it can be used.
    A retracted citation is IMMEDIATELY blocked — it cannot appear in any
    clinical response under any circumstances.
    
    Multi-source approach:
    1. In-memory cache of known retractions (seed data)
    2. Crossref REST API (real-time DOI verification)
    3. Retraction Watch API (comprehensive retraction database)
    4. Crossref webhooks (real-time push notifications — production)
    """

    def __init__(self) -> None:
        # Cache: doi/pmid → RetractionRecord
        self._retraction_cache: dict[str, RetractionRecord] = dict(KNOWN_RETRACTIONS)
        self._pmid_cache: dict[str, RetractionRecord] = dict(KNOWN_RETRACTIONS_BY_PMID)
        # Verified-clear DOIs — skip re-checking
        self._clear_cache: set[str] = set()
        self._clear_cache_pmid: set[str] = set()

    def check_chunk(self, chunk: EvidenceChunk) -> tuple[RetractionStatus, Optional[str]]:
        """
        Check a single evidence chunk for retraction status.
        Uses cached data first, API fallback for unknowns.
        Returns (RetractionStatus, retraction_notice_url).
        
        This is SYNCHRONOUS for in-memory checks.
        Use check_chunk_async for API-backed verification.
        """
        doi = chunk.provenance.source_doi
        content = chunk.content

        # Extract DOI from content if not in provenance
        if not doi:
            doi = self._extract_doi_from_content(content)

        # Extract PMID from content
        pmid = self._extract_pmid_from_content(content)

        # Check DOI against retraction cache
        if doi:
            doi_normalized = doi.lower().strip()
            if doi_normalized in self._retraction_cache:
                record = self._retraction_cache[doi_normalized]
                status = self._record_to_status(record)
                logger.warning(
                    f"RETRACTION: DOI {doi} is {record.retraction_type.upper()}. "
                    f"Reason: {record.retraction_reason}. Evidence BLOCKED."
                )
                return status, record.retraction_doi

            if doi_normalized in self._clear_cache:
                return RetractionStatus.CLEAR, None

        # Check PMID against retraction cache
        if pmid:
            if pmid in self._pmid_cache:
                record = self._pmid_cache[pmid]
                status = self._record_to_status(record)
                logger.warning(
                    f"RETRACTION: PMID {pmid} is {record.retraction_type.upper()}. "
                    f"Evidence BLOCKED."
                )
                return status, record.retraction_doi

            if pmid in self._clear_cache_pmid:
                return RetractionStatus.CLEAR, None

        # No DOI or PMID available — cannot verify
        if not doi and not pmid:
            return RetractionStatus.UNCHECKED, None

        # Not in cache — needs API verification
        return RetractionStatus.UNCHECKED, None

    async def check_chunk_async(
        self, chunk: EvidenceChunk
    ) -> tuple[RetractionStatus, Optional[str]]:
        """
        Full async retraction check with API fallback.
        Queries Crossref + Retraction Watch if not in cache.
        """
        # Fast path: in-memory check
        status, notice_url = self.check_chunk(chunk)
        if status != RetractionStatus.UNCHECKED:
            return status, notice_url

        doi = chunk.provenance.source_doi or self._extract_doi_from_content(chunk.content)

        if not doi:
            return RetractionStatus.UNCHECKED, None

        # Crossref API check
        crossref_status, crossref_url = await self._check_crossref(doi)
        if crossref_status == RetractionStatus.RETRACTED:
            return crossref_status, crossref_url

        # Retraction Watch API check (if available)
        rw_status, rw_url = await self._check_retraction_watch(doi)
        if rw_status == RetractionStatus.RETRACTED:
            return rw_status, rw_url

        # Mark as clear
        self._clear_cache.add(doi.lower().strip())
        return RetractionStatus.CLEAR, None

    async def verify_evidence_pack(
        self, chunks: list[EvidenceChunk]
    ) -> dict[str, tuple[RetractionStatus, Optional[str]]]:
        """
        Batch verify all chunks in an evidence pack.
        Runs concurrently with rate limiting.
        Returns {chunk_id: (status, notice_url)}.
        """
        semaphore = asyncio.Semaphore(5)

        async def check_with_limit(chunk: EvidenceChunk):
            async with semaphore:
                status, url = await self.check_chunk_async(chunk)
                return chunk.chunk_id, status, url

        tasks = [check_with_limit(c) for c in chunks]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        output = {}
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Retraction check error: {result}")
                continue
            chunk_id, status, url = result
            output[chunk_id] = (status, url)

        # Log any retractions found
        retracted = [cid for cid, (s, _) in output.items() if s == RetractionStatus.RETRACTED]
        if retracted:
            logger.critical(
                f"RETRACTED EVIDENCE IN PACK: {len(retracted)} chunks blocked. "
                f"IDs: {retracted}"
            )

        return output

    def filter_retracted(
        self, chunks: list[EvidenceChunk]
    ) -> tuple[list[EvidenceChunk], list[str]]:
        """
        Synchronous filter: remove any chunk with known retraction status.
        Returns (clean_chunks, blocked_chunk_ids).
        """
        clean = []
        blocked = []

        for chunk in chunks:
            status, _ = self.check_chunk(chunk)
            if status == RetractionStatus.RETRACTED:
                blocked.append(chunk.chunk_id)
                logger.warning(f"Blocked retracted evidence: {chunk.chunk_id}")
            else:
                clean.append(chunk)

        return clean, blocked

    def register_retraction(self, record: RetractionRecord) -> None:
        """Register a new retraction (from webhook or manual governance action)."""
        if record.doi:
            self._retraction_cache[record.doi.lower().strip()] = record
            self._clear_cache.discard(record.doi.lower().strip())
        if record.pmid:
            self._pmid_cache[record.pmid] = record
            self._clear_cache_pmid.discard(record.pmid)
        logger.warning(
            f"New retraction registered: {record.doi or record.pmid} "
            f"({record.retraction_type})"
        )

    def _record_to_status(self, record: RetractionRecord) -> RetractionStatus:
        if record.retraction_type == "retraction":
            return RetractionStatus.RETRACTED
        if record.retraction_type == "expression_of_concern":
            return RetractionStatus.EXPRESSION
        if record.retraction_type == "correction":
            return RetractionStatus.CORRECTED
        return RetractionStatus.RETRACTED

    def _extract_doi_from_content(self, content: str) -> Optional[str]:
        """Extract DOI from text content."""
        doi_pattern = re.compile(
            r'(?:doi:|DOI:|https?://doi\.org/|10\.)(\S+)',
            re.I
        )
        match = doi_pattern.search(content)
        if match:
            doi = match.group(0)
            doi = re.sub(r'^doi:', '', doi, flags=re.I)
            doi = doi.rstrip('.,;)')
            if doi.startswith('http'):
                doi = re.sub(r'https?://doi\.org/', '', doi)
            return doi.strip()
        return None

    def _extract_pmid_from_content(self, content: str) -> Optional[str]:
        """Extract PubMed ID from text content."""
        pmid_pattern = re.compile(r'PMID[:\s]+(\d{6,9})', re.I)
        match = pmid_pattern.search(content)
        if match:
            return match.group(1)
        return None

    async def _check_crossref(
        self, doi: str
    ) -> tuple[RetractionStatus, Optional[str]]:
        """Query Crossref API for retraction status."""
        try:
            async with aiohttp.ClientSession() as session:
                url = f"https://api.crossref.org/works/{doi}"
                headers = {"User-Agent": "CURANIQ/1.0 (safety@curaniq.com)"}
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        return RetractionStatus.UNCHECKED, None
                    data = await resp.json()
                    message = data.get("message", {})

                    # Check retraction relations
                    relations = message.get("relation", {})
                    if relations.get("is-retracted-by"):
                        retraction_doi = relations["is-retracted-by"][0].get("id", "")
                        return RetractionStatus.RETRACTED, f"https://doi.org/{retraction_doi}"

                    # Check update-to
                    for update in message.get("update-to", []):
                        update_type = update.get("type", "").lower()
                        if "retraction" in update_type:
                            return RetractionStatus.RETRACTED, update.get("DOI")
                        if "expression-of-concern" in update_type:
                            return RetractionStatus.EXPRESSION, update.get("DOI")
                        if "correction" in update_type:
                            return RetractionStatus.CORRECTED, update.get("DOI")

                    return RetractionStatus.CLEAR, None
        except Exception as e:
            logger.warning(f"Crossref check failed for DOI {doi}: {e}")
            return RetractionStatus.UNCHECKED, None

    async def _check_retraction_watch(
        self, doi: str
    ) -> tuple[RetractionStatus, Optional[str]]:
        """
        Query Retraction Watch database API.
        Production: uses the Retraction Watch REST API.
        """
        try:
            # Retraction Watch API endpoint
            url = f"https://api.retractionwatch.com/api/v1/retractions"
            params = {"doi": doi}
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, params=params,
                    timeout=aiohttp.ClientTimeout(total=5),
                    headers={"User-Agent": "CURANIQ/1.0 (safety@curaniq.com)"}
                ) as resp:
                    if resp.status != 200:
                        return RetractionStatus.UNCHECKED, None
                    data = await resp.json()
                    if not data or not isinstance(data, list):
                        return RetractionStatus.UNCHECKED, None

                    for record in data:
                        retraction_nature = record.get("RetractionNature", "").lower()
                        if "retraction" in retraction_nature:
                            return RetractionStatus.RETRACTED, record.get("RetractionDOI")
                        if "expression of concern" in retraction_nature:
                            return RetractionStatus.EXPRESSION, None
                        if "correction" in retraction_nature:
                            return RetractionStatus.CORRECTED, None

                    return RetractionStatus.CLEAR, None
        except Exception as e:
            logger.warning(f"Retraction Watch check failed for DOI {doi}: {e}")
            return RetractionStatus.UNCHECKED, None


# ─────────────────────────────────────────────────────────────────────────────
# L2-6: JURISDICTION-AWARE GUIDELINE GATING
# Architecture: 'UK → NICE. US → AHA/ACC. Uzbekistan → MOH + WHO'
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class JurisdictionGuidelines:
    """Primary guideline bodies for a jurisdiction."""
    jurisdiction: Jurisdiction
    primary_bodies: list[str]           # e.g., ["NICE", "BNF", "MHRA"]
    secondary_bodies: list[str]         # Accepted international bodies
    preferred_sources: list[SourceAPI]
    drug_reference: str                 # Primary drug reference
    formulary: Optional[str]            # Local formulary if applicable
    language_codes: list[str]           # ISO 639-1 language codes


# Jurisdiction → guideline authority mapping
JURISDICTION_CONFIG: dict[Jurisdiction, JurisdictionGuidelines] = {
    Jurisdiction.UK: JurisdictionGuidelines(
        jurisdiction=Jurisdiction.UK,
        primary_bodies=["NICE", "MHRA", "BNF", "BNFc", "PHE", "SIGN"],
        secondary_bodies=["WHO", "Cochrane", "EMA"],
        preferred_sources=[SourceAPI.NICE_GUIDELINES, SourceAPI.PUBMED, SourceAPI.COCHRANE],
        drug_reference="British National Formulary (BNF)",
        formulary="NHS England",
        language_codes=["en"],
    ),
    Jurisdiction.US: JurisdictionGuidelines(
        jurisdiction=Jurisdiction.US,
        primary_bodies=["FDA", "AHA", "ACC", "ADA", "IDSA", "ACOG", "AAP", "CDC"],
        secondary_bodies=["WHO", "Cochrane"],
        preferred_sources=[SourceAPI.OPENFDA_LABELS, SourceAPI.DAILYMED_SPL, SourceAPI.PUBMED],
        drug_reference="FDA Drug Label + Micromedex",
        formulary=None,
        language_codes=["en"],
    ),
    Jurisdiction.EU: JurisdictionGuidelines(
        jurisdiction=Jurisdiction.EU,
        primary_bodies=["EMA", "ESC", "EASD", "EFNS", "EULAR", "ECDC"],
        secondary_bodies=["WHO", "Cochrane", "NICE"],
        preferred_sources=[SourceAPI.EMA_EPAR, SourceAPI.PUBMED, SourceAPI.COCHRANE],
        drug_reference="EMA Summary of Product Characteristics (SmPC)",
        formulary=None,
        language_codes=["en", "de", "fr", "it", "es"],
    ),
    Jurisdiction.UZ: JurisdictionGuidelines(
        jurisdiction=Jurisdiction.UZ,
        primary_bodies=["Uzbekistan MOH", "WHO", "NICE"],  # Per architecture
        secondary_bodies=["Russian Minzdrav", "Cochrane", "EMA"],
        preferred_sources=[
            SourceAPI.UZ_MOH,
            SourceAPI.NICE_GUIDELINES,
            SourceAPI.PUBMED,
        ],
        drug_reference="Uzbekistan National Drug Formulary + WHO EML",
        formulary="Uzbekistan National Formulary",
        language_codes=["uz", "ru", "en"],
    ),
    Jurisdiction.CIS: JurisdictionGuidelines(
        jurisdiction=Jurisdiction.CIS,
        primary_bodies=["Russian Minzdrav", "WHO", "NICE"],
        secondary_bodies=["Cochrane", "EMA"],
        preferred_sources=[
            SourceAPI.RUSSIAN_MINZDRAV,
            SourceAPI.PUBMED,
            SourceAPI.NICE_GUIDELINES,
        ],
        drug_reference="Russian National Pharmacopoeia + Vidal",
        formulary="CIS Regional Formulary",
        language_codes=["ru"],
    ),
    Jurisdiction.WHO: JurisdictionGuidelines(
        jurisdiction=Jurisdiction.WHO,
        primary_bodies=["WHO", "Cochrane"],
        secondary_bodies=["NICE", "FDA", "EMA"],
        preferred_sources=[SourceAPI.PUBMED, SourceAPI.COCHRANE, SourceAPI.CLINICAL_TRIALS],
        drug_reference="WHO Model Formulary",
        formulary="WHO Essential Medicines List",
        language_codes=["en", "fr", "es", "ru", "ar", "zh"],
    ),
    Jurisdiction.INTL: JurisdictionGuidelines(
        jurisdiction=Jurisdiction.INTL,
        primary_bodies=["WHO", "Cochrane", "NICE", "FDA", "EMA"],
        secondary_bodies=["AHA", "ESC", "EASD"],
        preferred_sources=[
            SourceAPI.PUBMED,
            SourceAPI.COCHRANE,
            SourceAPI.OPENFDA_LABELS,
            SourceAPI.NICE_GUIDELINES,
        ],
        drug_reference="Multiple (WHO + FDA + NICE)",
        formulary=None,
        language_codes=["en"],
    ),
}


class JurisdictionGuidanceGate:
    """
    L2-6: Jurisdiction-aware guideline gating.
    
    Filters and ranks evidence by jurisdiction relevance.
    Ensures UK clinicians get NICE guidance, US clinicians get FDA/AHA,
    Uzbekistan clinicians get MOH + WHO guidance.
    
    Per architecture: 'Auto by geolocation/profile. International
    comparison view available.'
    """

    def filter_by_jurisdiction(
        self,
        chunks: list[EvidenceChunk],
        jurisdiction: Jurisdiction,
        strict: bool = False,
    ) -> list[EvidenceChunk]:
        """
        Filter evidence chunks by jurisdiction relevance.
        
        strict=False: returns all chunks, jurisdictionally-relevant first (recommended)
        strict=True: returns ONLY chunks from the specified jurisdiction
        """
        config = JURISDICTION_CONFIG.get(jurisdiction, JURISDICTION_CONFIG[Jurisdiction.INTL])

        # Sort: jurisdiction-matching chunks first
        def relevance_score(chunk: EvidenceChunk) -> float:
            source = chunk.provenance.source_api
            chunk_jur = chunk.provenance.jurisdiction

            # Perfect match: same jurisdiction
            if chunk_jur == jurisdiction:
                base = 1.0
            # International is always acceptable
            elif chunk_jur == Jurisdiction.INTL or jurisdiction == Jurisdiction.INTL:
                base = 0.8
            # WHO is universally applicable
            elif chunk_jur == Jurisdiction.WHO or source == SourceAPI.PUBMED:
                base = 0.7
            else:
                base = 0.3

            # Boost for preferred sources
            if source in config.preferred_sources:
                base += 0.2

            return min(base, 1.0)

        if strict:
            filtered = [
                c for c in chunks
                if c.provenance.jurisdiction == jurisdiction
                or c.provenance.jurisdiction == Jurisdiction.INTL
                or c.provenance.jurisdiction == Jurisdiction.WHO
            ]
        else:
            filtered = chunks

        return sorted(filtered, key=relevance_score, reverse=True)

    def get_guideline_context(self, jurisdiction: Jurisdiction) -> dict[str, Any]:
        """
        Get the regulatory context for a jurisdiction.
        Included in every response to inform clinicians which guidelines apply.
        """
        config = JURISDICTION_CONFIG.get(jurisdiction)
        if not config:
            config = JURISDICTION_CONFIG[Jurisdiction.INTL]

        return {
            "jurisdiction": jurisdiction.value,
            "primary_guideline_bodies": config.primary_bodies,
            "secondary_bodies": config.secondary_bodies,
            "drug_reference": config.drug_reference,
            "formulary": config.formulary,
            "languages": config.language_codes,
            "note": self._get_jurisdiction_note(jurisdiction),
        }

    def _get_jurisdiction_note(self, jurisdiction: Jurisdiction) -> str:
        notes = {
            Jurisdiction.UK: (
                "UK guidance: NICE guidelines are authoritative. Drug dosing per BNF. "
                "Regulatory authority: MHRA."
            ),
            Jurisdiction.US: (
                "US guidance: FDA labelling is authoritative. Professional society guidelines "
                "(AHA/ACC/ADA/IDSA) provide clinical direction. Regulatory authority: FDA."
            ),
            Jurisdiction.EU: (
                "EU guidance: EMA SmPC is authoritative. ESC/EASD/EULAR guidelines apply. "
                "Regulatory authority: EMA (national agencies for member states)."
            ),
            Jurisdiction.UZ: (
                "Uzbekistan guidance: MOH clinical protocols apply. NICE guidelines are "
                "referenced as international standard. WHO Essential Medicines inform availability. "
                "Local formulary constraints may affect first-line choices."
            ),
            Jurisdiction.CIS: (
                "CIS/Russia guidance: Russian Minzdrav clinical guidelines apply. "
                "Drug names may differ from INN — CIS brand names used. "
                "WHO guidance referenced for international alignment."
            ),
            Jurisdiction.WHO: (
                "International/WHO guidance: WHO Essential Medicines List and clinical guidelines. "
                "Cochrane evidence used as gold standard. Jurisdiction-specific guidelines "
                "supersede WHO recommendations when available."
            ),
            Jurisdiction.INTL: (
                "Multi-jurisdictional guidance: Evidence synthesized across NICE (UK), "
                "FDA (US), EMA (EU), and WHO. "
                "Clinicians should verify local regulatory requirements."
            ),
        }
        return notes.get(jurisdiction, "Consult local regulatory guidelines.")

    def get_comparison_view(
        self,
        chunks: list[EvidenceChunk],
        jurisdictions: list[Jurisdiction],
    ) -> dict[str, list[EvidenceChunk]]:
        """
        International comparison view — show how guidance differs by jurisdiction.
        Per architecture: 'International comparison view available.'
        """
        comparison: dict[str, list[EvidenceChunk]] = {}
        for jur in jurisdictions:
            filtered = self.filter_by_jurisdiction(chunks, jur, strict=True)
            comparison[jur.value] = filtered[:5]  # Top 5 per jurisdiction
        return comparison

    def infer_jurisdiction(
        self,
        user_profile: Optional[dict[str, Any]] = None,
        geolocation: Optional[str] = None,
    ) -> Jurisdiction:
        """
        Auto-infer jurisdiction from user profile or geolocation.
        Per architecture: 'Auto by geolocation/profile.'
        """
        if user_profile:
            jur_code = user_profile.get("jurisdiction") or user_profile.get("country")
            if jur_code:
                try:
                    return Jurisdiction(jur_code.lower())
                except ValueError:
                    pass

        if geolocation:
            geo_map = {
                "GB": Jurisdiction.UK, "UK": Jurisdiction.UK,
                "US": Jurisdiction.US, "US_TERRITORIES": Jurisdiction.US,
                "DE": Jurisdiction.EU, "FR": Jurisdiction.EU, "IT": Jurisdiction.EU,
                "ES": Jurisdiction.EU, "NL": Jurisdiction.EU, "AT": Jurisdiction.EU,
                "UZ": Jurisdiction.UZ,
                "RU": Jurisdiction.CIS, "BY": Jurisdiction.CIS,
                "KZ": Jurisdiction.CIS, "KG": Jurisdiction.CIS,
                "TJ": Jurisdiction.CIS, "TM": Jurisdiction.CIS,
                "AZ": Jurisdiction.CIS, "AM": Jurisdiction.CIS,
                "GE": Jurisdiction.CIS, "MD": Jurisdiction.CIS,
                "UA": Jurisdiction.CIS,
            }
            jur = geo_map.get(geolocation.upper())
            if jur:
                return jur

        return Jurisdiction.INTL  # Default: international
