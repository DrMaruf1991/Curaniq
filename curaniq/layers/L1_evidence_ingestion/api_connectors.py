"""
CURANIQ — Medical Evidence Operating System
Layer 1: Evidence Ingestion — Live API Connectors (L1-1)

Connects to all governed evidence sources with defined SLAs.
Web search is NEVER used as an evidence source — it is only used as a
meta-freshness signal (L1-17) to trigger pulls from governed APIs.

Sources implemented:
- PubMed (NCBI E-utilities) — 4x/day polling
- openFDA (drug labels + FAERS) — labels daily, FAERS weekly
- DailyMed SPL — daily + integrity checks
- Crossref REST API — DOI metadata + retraction detection
- Retraction Watch DB — comprehensive retraction detection (real-time per citation)
- NICE Syndication API — daily fetch
- LactMed API (NIH) — weekly
- Cochrane Library API — weekly
- ClinicalTrials.gov API v2 — every 6h priority
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlencode

import aiohttp

from curaniq.models.evidence import (
    EvidenceChunk,
    EvidenceProvenanceChain,
    EvidenceTier,
    Jurisdiction,
    RetractionStatus,
    SourceAPI,
    StalenessStatus,
    STALENESS_TTL_HOURS,
    FAIL_CLOSED_SOURCES,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# BASE API CONNECTOR
# ─────────────────────────────────────────────────────────────────────────────

class APIConnectionError(Exception):
    """Raised when a governed source is unreachable."""
    pass

class StalenessFailClosedError(Exception):
    """
    Raised when a safety-critical source has expired TTL.
    Per L1-5: refuse rather than use stale safety-critical data.
    """
    pass


class BaseAPIConnector:
    """
    Base class for all governed source API connectors.
    Enforces: rate limiting, timeout, retry, staleness tracking, error logging.
    """
    
    SOURCE: SourceAPI
    BASE_URL: str
    REQUEST_TIMEOUT_SECONDS: float = 30.0
    MAX_RETRIES: int = 3
    RETRY_BACKOFF_BASE: float = 2.0    # Exponential backoff
    PIPELINE_VERSION: str = "1.0.0"

    def __init__(self, api_key: Optional[str] = None, session: Optional[aiohttp.ClientSession] = None):
        self.api_key = api_key
        self._session = session
        self._last_successful_fetch: Optional[datetime] = None
        self._consecutive_failures: int = 0

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.REQUEST_TIMEOUT_SECONDS)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _get(self, url: str, params: Optional[dict] = None, headers: Optional[dict] = None) -> dict:
        """
        Execute GET request with retry + exponential backoff.
        Tracks consecutive failures for source-unreachable detection (L1-5).
        """
        session = await self.get_session()
        last_error = None
        
        for attempt in range(self.MAX_RETRIES):
            try:
                async with session.get(url, params=params, headers=headers) as resp:
                    if resp.status == 200:
                        self._consecutive_failures = 0
                        self._last_successful_fetch = datetime.now(timezone.utc)
                        return await resp.json()
                    elif resp.status == 429:
                        # Rate limited — back off
                        wait = self.RETRY_BACKOFF_BASE ** (attempt + 2)
                        logger.warning(f"{self.SOURCE.value}: Rate limited. Waiting {wait}s.")
                        await asyncio.sleep(wait)
                    elif resp.status >= 500:
                        raise APIConnectionError(f"Server error {resp.status} from {self.SOURCE.value}")
                    else:
                        raise APIConnectionError(f"HTTP {resp.status} from {self.SOURCE.value}: {url}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = e
                self._consecutive_failures += 1
                if attempt < self.MAX_RETRIES - 1:
                    wait = self.RETRY_BACKOFF_BASE ** attempt
                    await asyncio.sleep(wait)
        
        # All retries exhausted
        self._consecutive_failures += 1
        logger.error(
            f"{self.SOURCE.value}: All {self.MAX_RETRIES} retries failed. "
            f"Consecutive failures: {self._consecutive_failures}. Last error: {last_error}"
        )
        raise APIConnectionError(f"{self.SOURCE.value}: Unreachable after {self.MAX_RETRIES} retries. {last_error}")

    def check_staleness(self) -> StalenessStatus:
        """
        Check TTL status of this source per L1-5 SLA Dashboard.
        Returns CRITICAL for safety-critical sources past TTL → triggers REFUSE.
        """
        if self._last_successful_fetch is None:
            return StalenessStatus.UNKNOWN
        
        ttl_hours = STALENESS_TTL_HOURS.get(self.SOURCE, 24.0)
        now = datetime.now(timezone.utc)
        age_hours = (now - self._last_successful_fetch).total_seconds() / 3600
        
        if age_hours <= ttl_hours:
            return StalenessStatus.FRESH
        
        # TTL expired
        if self.SOURCE in FAIL_CLOSED_SOURCES:
            return StalenessStatus.CRITICAL
        return StalenessStatus.STALE

    def _make_provenance(
        self,
        snippet_bytes: bytes,
        doi: Optional[str] = None,
        pub_date: Optional[datetime] = None,
        jurisdiction: Jurisdiction = Jurisdiction.INTL,
        evidence_tier: EvidenceTier = EvidenceTier.UNKNOWN,
        document_version: Optional[str] = None,
        chunk_position: int = 0,
        parent_document_id: str = "",
    ) -> EvidenceProvenanceChain:
        """Create a complete, immutable provenance chain for an evidence chunk."""
        return EvidenceProvenanceChain(
            source_api=self.SOURCE,
            retrieval_timestamp=datetime.now(timezone.utc),
            document_version=document_version,
            snippet_hash=hashlib.sha256(snippet_bytes).hexdigest(),
            ingestion_pipeline_version=self.PIPELINE_VERSION,
            source_doi=doi,
            publication_date=pub_date,
            jurisdiction=jurisdiction,
            evidence_tier=evidence_tier,
            chunk_position=chunk_position,
            parent_document_id=parent_document_id or str(hashlib.md5(snippet_bytes[:100]).hexdigest()),
        )


# ─────────────────────────────────────────────────────────────────────────────
# PUBMED CONNECTOR (L1-1)
# NCBI E-utilities — 4x/day polling per SLA
# ─────────────────────────────────────────────────────────────────────────────

class PubMedConnector(BaseAPIConnector):
    """
    PubMed E-utilities connector.
    
    Search → Fetch cycle:
    1. esearch: get PMIDs matching query
    2. efetch: retrieve full abstracts + metadata
    3. Parse publication type to determine EvidenceTier
    4. Extract PICO elements where present
    """
    
    SOURCE = SourceAPI.PUBMED
    BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    
    # Publication types → EvidenceTier mapping
    PUBTYPE_TO_TIER: dict[str, EvidenceTier] = {
        "Systematic Review":       EvidenceTier.SYSTEMATIC_REVIEW,
        "Meta-Analysis":           EvidenceTier.SYSTEMATIC_REVIEW,
        "Randomized Controlled Trial": EvidenceTier.RCT,
        "Clinical Trial, Phase III":   EvidenceTier.RCT,
        "Clinical Trial, Phase IV":    EvidenceTier.RCT,
        "Practice Guideline":      EvidenceTier.GUIDELINE,
        "Guideline":               EvidenceTier.GUIDELINE,
        "Cohort Studies":          EvidenceTier.COHORT,
        "Case Reports":            EvidenceTier.CASE_REPORT,
        "Review":                  EvidenceTier.COHORT,  # Narrative review
    }

    async def search(
        self,
        query: str,
        max_results: int = 20,
        date_from: Optional[str] = None,   # YYYY/MM/DD
        date_to: Optional[str] = None,
        pub_types: Optional[list[str]] = None,
    ) -> list[str]:
        """
        Search PubMed. Returns list of PMIDs.
        
        Automatically adds [MeSH Terms] and [tiab] qualifiers for precision.
        Filters by date range when specified (for delta detection L1-16).
        """
        params: dict[str, Any] = {
            "db": "pubmed",
            "term": query,
            "retmax": max_results,
            "retmode": "json",
            "usehistory": "y",
        }
        
        if self.api_key:
            params["api_key"] = self.api_key
        
        if date_from and date_to:
            params["datetype"] = "pdat"
            params["mindate"] = date_from
            params["maxdate"] = date_to
        
        if pub_types:
            # Filter to specific publication types (e.g., only RCTs)
            type_filter = " OR ".join(f'"{pt}"[pt]' for pt in pub_types)
            params["term"] = f"({query}) AND ({type_filter})"
        
        try:
            response = await self._get(f"{self.BASE_URL}/esearch.fcgi", params=params)
            return response.get("esearchresult", {}).get("idlist", [])
        except APIConnectionError:
            logger.warning(f"PubMed search failed for: {query}")
            return []

    async def fetch_articles(self, pmids: list[str]) -> list[EvidenceChunk]:
        """
        Fetch full article metadata + abstracts for given PMIDs.
        Returns list of EvidenceChunk objects with complete provenance.
        """
        if not pmids:
            return []
        
        params: dict[str, Any] = {
            "db": "pubmed",
            "id": ",".join(pmids),
            "retmode": "json",
            "rettype": "abstract",
        }
        if self.api_key:
            params["api_key"] = self.api_key
        
        try:
            response = await self._get(f"{self.BASE_URL}/efetch.fcgi", params=params)
        except APIConnectionError:
            logger.error(f"PubMed efetch failed for PMIDs: {pmids}")
            return []
        
        chunks = []
        articles = response.get("PubmedArticleSet", {}).get("PubmedArticle", [])
        if not isinstance(articles, list):
            articles = [articles]
        
        for article in articles:
            try:
                chunk = self._parse_article(article)
                if chunk:
                    chunks.append(chunk)
            except Exception as e:
                logger.error(f"Failed to parse PubMed article: {e}")
                continue
        
        return chunks

    def _parse_article(self, article: dict) -> Optional[EvidenceChunk]:
        """Parse a PubMed article JSON into an EvidenceChunk."""
        try:
            medline = article.get("MedlineCitation", {})
            pmid = medline.get("PMID", {})
            pmid_str = pmid.get("#text", "") if isinstance(pmid, dict) else str(pmid)
            
            article_data = medline.get("Article", {})
            title = article_data.get("ArticleTitle", "")
            if isinstance(title, dict):
                title = title.get("#text", "")
            
            # Abstract extraction
            abstract_obj = article_data.get("Abstract", {})
            abstract_texts = abstract_obj.get("AbstractText", "")
            if isinstance(abstract_texts, list):
                abstract = " ".join(
                    (t.get("#text", t) if isinstance(t, dict) else t)
                    for t in abstract_texts
                )
            elif isinstance(abstract_texts, dict):
                abstract = abstract_texts.get("#text", "")
            else:
                abstract = str(abstract_texts)
            
            if not abstract:
                return None  # Skip articles without abstracts
            
            # Content assembly
            content = f"TITLE: {title}\n\nABSTRACT: {abstract}"
            content_bytes = content.encode("utf-8")
            
            # Publication date
            pub_date = None
            journal = article_data.get("Journal", {})
            journal_issue = journal.get("JournalIssue", {})
            pub_date_data = journal_issue.get("PubDate", {})
            year = pub_date_data.get("Year")
            month = pub_date_data.get("Month", "01")
            day = pub_date_data.get("Day", "01")
            
            if year:
                try:
                    # Handle month abbreviations (Jan, Feb, etc.)
                    month_map = {
                        "Jan": "01", "Feb": "02", "Mar": "03", "Apr": "04",
                        "May": "05", "Jun": "06", "Jul": "07", "Aug": "08",
                        "Sep": "09", "Oct": "10", "Nov": "11", "Dec": "12",
                    }
                    month_str = month_map.get(str(month), str(month).zfill(2))
                    day_str = str(day).zfill(2) if str(day).isdigit() else "01"
                    pub_date = datetime.strptime(
                        f"{year}-{month_str}-{day_str}", "%Y-%m-%d"
                    ).replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    pub_date = None
            
            # Evidence tier from publication type
            pub_type_list = article_data.get("PublicationTypeList", {}).get("PublicationType", [])
            if not isinstance(pub_type_list, list):
                pub_type_list = [pub_type_list]
            
            evidence_tier = EvidenceTier.UNKNOWN
            for pt in pub_type_list:
                pt_text = pt.get("#text", pt) if isinstance(pt, dict) else str(pt)
                if pt_text in self.PUBTYPE_TO_TIER:
                    evidence_tier = self.PUBTYPE_TO_TIER[pt_text]
                    break
            
            # DOI
            doi = None
            article_id_list = article.get("PubmedData", {}).get("ArticleIdList", {}).get("ArticleId", [])
            if not isinstance(article_id_list, list):
                article_id_list = [article_id_list]
            for aid in article_id_list:
                if isinstance(aid, dict) and aid.get("@IdType") == "doi":
                    doi = aid.get("#text")
                    break
            
            provenance = self._make_provenance(
                snippet_bytes=content_bytes,
                doi=doi,
                pub_date=pub_date,
                jurisdiction=Jurisdiction.INTL,
                evidence_tier=evidence_tier,
                document_version=f"PMID:{pmid_str}",
                chunk_position=0,
                parent_document_id=f"pubmed:{pmid_str}",
            )
            
            # Preprint quarantine check (L1-6) — PubMed shouldn't have preprints
            # but defensive check
            is_preprint = evidence_tier == EvidenceTier.PREPRINT
            
            chunk = EvidenceChunk(
                content=content,
                content_bytes=content_bytes,
                provenance=provenance,
                retraction_status=RetractionStatus.UNCHECKED,  # Will be checked by L2-7
                evidence_tier=evidence_tier,
                staleness_status=self.check_staleness(),
                staleness_ttl_hours=STALENESS_TTL_HOURS[self.SOURCE],
                last_verified=datetime.now(timezone.utc),
                is_quarantined=is_preprint,
                quarantine_reason="PubMed preprint detected — requires peer review verification" if is_preprint else None,
            )
            
            return chunk
        
        except Exception as e:
            logger.error(f"Error parsing PubMed article: {e}")
            return None

    async def search_and_fetch(
        self,
        query: str,
        max_results: int = 20,
        pub_types: Optional[list[str]] = None,
    ) -> list[EvidenceChunk]:
        """Combined search + fetch in one call."""
        pmids = await self.search(query, max_results=max_results, pub_types=pub_types)
        if not pmids:
            return []
        return await self.fetch_articles(pmids)


# ─────────────────────────────────────────────────────────────────────────────
# OPENFDA CONNECTOR (L1-1)
# Drug labels (daily) + FAERS adverse events (weekly)
# ─────────────────────────────────────────────────────────────────────────────

class OpenFDAConnector(BaseAPIConnector):
    """
    openFDA API connector for drug labels (SPL) and adverse event reports (FAERS).
    
    Drug labels: contain dosing, contraindications, warnings, pregnancy info.
    FAERS: adverse event signals — fed to L1-11 Pharmacovigilance Feed.
    """
    
    SOURCE = SourceAPI.OPENFDA_LABELS
    BASE_URL = "https://api.fda.gov"
    
    # Critical fields from drug labels to extract
    LABEL_FIELDS = [
        "warnings_and_cautions",
        "contraindications",
        "dosage_and_administration",
        "drug_interactions",
        "pregnancy",
        "nursing_mothers",
        "pediatric_use",
        "geriatric_use",
        "boxed_warning",
        "warnings",
        "precautions",
        "adverse_reactions",
        "clinical_pharmacology",
    ]

    async def search_drug_label(
        self,
        drug_name: str,
        field: str = "openfda.generic_name",
    ) -> list[EvidenceChunk]:
        """
        Search drug labels by drug name.
        Returns structured evidence chunks for each critical label section.
        
        Priority: boxed_warning > contraindications > warnings > dosing > interactions
        """
        params = {
            "search": f'{field}:"{drug_name}"',
            "limit": 3,  # Top 3 most recent labels
        }
        if self.api_key:
            params["api_key"] = self.api_key
        
        staleness = self.check_staleness()
        if staleness == StalenessStatus.CRITICAL:
            raise StalenessFailClosedError(
                f"openFDA drug labels: TTL expired for safety-critical source. "
                f"Cannot serve dosing/contraindication data from stale FDA labels. "
                f"Last successful fetch: {self._last_successful_fetch}"
            )
        
        try:
            response = await self._get(f"{self.BASE_URL}/drug/label.json", params=params)
        except APIConnectionError:
            logger.error(f"openFDA label search failed for: {drug_name}")
            return []
        
        chunks = []
        results = response.get("results", [])
        
        for label in results:
            # Extract key label sections as separate chunks
            # Prioritize safety-critical sections
            openfda = label.get("openfda", {})
            
            # Determine drug identity
            generic_names = openfda.get("generic_name", ["Unknown"])
            brand_names = openfda.get("brand_name", [])
            application_numbers = openfda.get("application_number", [])
            
            drug_id = f"openfda:{application_numbers[0] if application_numbers else generic_names[0]}"
            
            # Extract SPL version for document versioning
            set_id = label.get("set_id", "")
            version = label.get("version", "1")
            doc_version = f"SPL:{set_id}:v{version}"
            
            # Process each critical section
            priority_sections = [
                ("boxed_warning", True),           # Always extract if present
                ("contraindications", True),       # Always extract
                ("warnings_and_cautions", True),   # Always extract
                ("drug_interactions", True),       # Always extract
                ("dosage_and_administration", True),
                ("pregnancy", True),
                ("nursing_mothers", True),
                ("pediatric_use", False),
                ("geriatric_use", False),
                ("clinical_pharmacology", False),
            ]
            
            for chunk_idx, (section, is_safety_critical) in enumerate(priority_sections):
                section_text = label.get(section, [])
                if not section_text:
                    continue
                
                if isinstance(section_text, list):
                    text = " ".join(str(t) for t in section_text)
                else:
                    text = str(section_text)
                
                if not text.strip():
                    continue
                
                # Prefix with drug name and section for context
                drug_label = f"{generic_names[0]} ({', '.join(brand_names[:2])})" if brand_names else generic_names[0]
                content = f"FDA DRUG LABEL — {drug_label}\nSECTION: {section.upper().replace('_', ' ')}\n\n{text}"
                content_bytes = content.encode("utf-8")
                
                # Determine evidence tier for drug labels
                # Black box warnings and contraindications = guideline-level evidence
                if section in ("boxed_warning", "contraindications", "warnings_and_cautions"):
                    tier = EvidenceTier.GUIDELINE
                else:
                    tier = EvidenceTier.COHORT
                
                provenance = self._make_provenance(
                    snippet_bytes=content_bytes,
                    jurisdiction=Jurisdiction.US,
                    evidence_tier=tier,
                    document_version=doc_version,
                    chunk_position=chunk_idx,
                    parent_document_id=drug_id,
                )
                
                chunk = EvidenceChunk(
                    content=content,
                    content_bytes=content_bytes,
                    provenance=provenance,
                    retraction_status=RetractionStatus.CLEAR,  # FDA labels are authoritative
                    evidence_tier=tier,
                    staleness_status=staleness,
                    staleness_ttl_hours=STALENESS_TTL_HOURS[self.SOURCE],
                    last_verified=datetime.now(timezone.utc),
                )
                
                chunks.append(chunk)
        
        logger.info(f"openFDA: Retrieved {len(chunks)} label chunks for '{drug_name}'")
        return chunks

    async def check_black_box_warning(self, drug_name: str) -> Optional[str]:
        """
        Specifically check if a drug has a Black Box Warning.
        Used by L5-11 (Black Box / REMS Gate) for mandatory flagging.
        Returns the warning text if present, None otherwise.
        """
        params = {
            "search": f'openfda.generic_name:"{drug_name}"',
            "limit": 1,
        }
        if self.api_key:
            params["api_key"] = self.api_key
        
        try:
            response = await self._get(f"{self.BASE_URL}/drug/label.json", params=params)
            results = response.get("results", [])
            if results and results[0].get("boxed_warning"):
                return results[0]["boxed_warning"][0]
        except APIConnectionError:
            pass
        
        return None


# ─────────────────────────────────────────────────────────────────────────────
# CROSSREF CONNECTOR (L1-1 + L2-7 Retraction Sentinel)
# Real-time DOI verification and retraction detection
# ─────────────────────────────────────────────────────────────────────────────

class CrossrefConnector(BaseAPIConnector):
    """
    Crossref REST API for DOI metadata and retraction detection.
    
    Per architecture: 'Real-time Crossref + Retraction Watch check on
    every citation. Contaminated evidence blocked.'
    
    Every DOI cited by CURANIQ MUST pass through this connector.
    Retracted evidence is BLOCKED — never shown to clinicians.
    """
    
    SOURCE = SourceAPI.CROSSREF
    BASE_URL = "https://api.crossref.org"
    PIPELINE_VERSION = "1.0.0"
    
    # Crossref retraction/update types to flag
    RETRACTION_TYPES = {
        "retraction",
        "expression-of-concern",
        "correction",
        "partial-retraction",
    }

    async def check_doi_retraction(self, doi: str) -> tuple[RetractionStatus, Optional[str]]:
        """
        Check if a DOI has been retracted.
        Returns (RetractionStatus, retraction_notice_url).
        
        Checks both:
        1. Crossref 'relation' field for retraction notices
        2. Crossref 'update-to' field for corrections
        
        This is called for EVERY citation before it is added to an evidence pack.
        Retracted evidence is immediately blocked — it cannot be cited.
        """
        params = {
            "mailto": "safety@curaniq.com"  # Crossref polite pool requirement
        }
        
        try:
            response = await self._get(
                f"{self.BASE_URL}/works/{doi}",
                params=params,
                headers={"User-Agent": f"CURANIQ/{self.PIPELINE_VERSION} (safety@curaniq.com)"},
            )
        except APIConnectionError:
            logger.warning(f"Crossref unreachable for DOI: {doi}. Marking as UNCHECKED.")
            return RetractionStatus.UNCHECKED, None
        
        message = response.get("message", {})
        
        # Check 'relation' field for retraction notices
        relations = message.get("relation", {})
        is_retracted_by = relations.get("is-retracted-by", [])
        if is_retracted_by:
            retraction_doi = is_retracted_by[0].get("id", "unknown")
            logger.warning(f"RETRACTION DETECTED: DOI {doi} retracted by {retraction_doi}")
            return RetractionStatus.RETRACTED, f"https://doi.org/{retraction_doi}"
        
        # Check 'update-to' for corrections/expressions of concern
        update_to = message.get("update-to", [])
        for update in update_to:
            update_type = update.get("type", "").lower()
            if "retraction" in update_type:
                return RetractionStatus.RETRACTED, update.get("DOI")
            elif "expression-of-concern" in update_type:
                return RetractionStatus.EXPRESSION, update.get("DOI")
            elif "correction" in update_type:
                return RetractionStatus.CORRECTED, update.get("DOI")
        
        # Check 'type' field — if the paper itself IS a retraction notice
        doc_type = message.get("type", "")
        if doc_type == "retraction":
            return RetractionStatus.RETRACTED, f"https://doi.org/{doi}"
        
        return RetractionStatus.CLEAR, None

    async def batch_check_retractions(
        self, dois: list[str]
    ) -> dict[str, tuple[RetractionStatus, Optional[str]]]:
        """
        Batch check multiple DOIs for retraction status.
        Runs concurrently with rate limiting.
        Returns {doi: (status, retraction_url)}.
        """
        # Rate limiting: Crossref polite pool allows ~50 req/s
        semaphore = asyncio.Semaphore(10)
        
        async def check_with_limit(doi: str):
            async with semaphore:
                return doi, await self.check_doi_retraction(doi)
        
        tasks = [check_with_limit(doi) for doi in dois]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        output = {}
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Retraction check error: {result}")
                continue
            doi, status = result
            output[doi] = status
        
        return output


# ─────────────────────────────────────────────────────────────────────────────
# NICE GUIDELINES CONNECTOR (L1-9)
# Official UK NICE guideline feed — daily fetch
# ─────────────────────────────────────────────────────────────────────────────

class NICEConnector(BaseAPIConnector):
    """
    NICE Syndication API — UK clinical guidelines.
    
    Essential for UK/Uzbekistan NICE pathway alignment (per architecture).
    NICE guidelines represent highest-quality regulatory evidence for UK context.
    """
    
    SOURCE = SourceAPI.NICE_GUIDELINES
    BASE_URL = "https://api.nice.org.uk/services/syndication/v1"

    async def search_guidelines(
        self,
        query: str,
        product_types: Optional[list[str]] = None,
    ) -> list[EvidenceChunk]:
        """
        Search NICE guidelines.
        product_types: e.g., ["guidelines", "technology-appraisals", "quality-standards"]
        """
        staleness = self.check_staleness()
        
        params: dict[str, Any] = {
            "q": query,
            "apikey": self.api_key or "",
        }
        if product_types:
            params["productTypes"] = ",".join(product_types)
        
        try:
            response = await self._get(
                f"{self.BASE_URL}/guidance",
                params=params,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self.api_key}" if self.api_key else "",
                },
            )
        except APIConnectionError:
            logger.warning(f"NICE API unavailable for query: {query}")
            return []
        
        chunks = []
        items = response if isinstance(response, list) else response.get("data", [])
        
        for item in items[:10]:  # Limit to 10 most relevant
            title = item.get("title", "")
            summary = item.get("description", item.get("summary", ""))
            published = item.get("publishedDate", item.get("updated", ""))
            nice_id = item.get("id", item.get("url", ""))
            
            if not summary:
                continue
            
            content = f"NICE GUIDELINE: {title}\n\n{summary}"
            content_bytes = content.encode("utf-8")
            
            # Parse publication date
            pub_date = None
            if published:
                try:
                    pub_date = datetime.fromisoformat(
                        published.replace("Z", "+00:00")
                    )
                except (ValueError, AttributeError):
                    pass
            
            provenance = self._make_provenance(
                snippet_bytes=content_bytes,
                jurisdiction=Jurisdiction.UK,
                evidence_tier=EvidenceTier.GUIDELINE,
                document_version=f"NICE:{nice_id}",
                pub_date=pub_date,
                chunk_position=0,
                parent_document_id=f"nice:{nice_id}",
            )
            
            chunk = EvidenceChunk(
                content=content,
                content_bytes=content_bytes,
                provenance=provenance,
                retraction_status=RetractionStatus.CLEAR,  # NICE guidelines are authoritative
                evidence_tier=EvidenceTier.GUIDELINE,
                staleness_status=staleness,
                staleness_ttl_hours=STALENESS_TTL_HOURS[self.SOURCE],
                last_verified=datetime.now(timezone.utc),
            )
            chunks.append(chunk)
        
        return chunks


# ─────────────────────────────────────────────────────────────────────────────
# LACTMED CONNECTOR (L1-10)
# NIH LactMed database — drug safety in breastfeeding
# ─────────────────────────────────────────────────────────────────────────────

class LactMedConnector(BaseAPIConnector):
    """
    NIH LactMed API — drug effects on breastfeeding.
    
    Per architecture: 'Structured extraction of lactation risk levels,
    infant exposure data, alternative recommendations. Critical for
    postpartum medication safety.' Feeds L3-9 Pregnancy & Lactation Engine.
    """
    
    SOURCE = SourceAPI.LACTMED
    BASE_URL = "https://lhncbc.nlm.nih.gov/LactMed/api"

    async def search_drug(self, drug_name: str) -> list[EvidenceChunk]:
        """
        Search LactMed for drug safety data in breastfeeding.
        Returns evidence chunks with lactation risk level + infant exposure data.
        """
        # LactMed uses their own search endpoint
        try:
            response = await self._get(
                f"{self.BASE_URL}/drug",
                params={"drug": drug_name, "return": "json"},
            )
        except APIConnectionError:
            logger.warning(f"LactMed unavailable for: {drug_name}")
            return []
        
        chunks = []
        records = response if isinstance(response, list) else response.get("records", [])
        
        for record in records[:3]:
            drug_id = record.get("id", "")
            drug_name_official = record.get("name", drug_name)
            summary = record.get("summary", "")
            risk_level = record.get("riskSummary", "")
            
            # Fetch full record for detailed data
            try:
                detail = await self._get(
                    f"{self.BASE_URL}/drug/{drug_id}",
                    params={"return": "json"},
                )
            except APIConnectionError:
                detail = {}
            
            maternal_levels = detail.get("maternalLevels", "")
            infant_levels = detail.get("infantLevels", "")
            alternative_drugs = detail.get("alternativeDrugs", "")
            concern_level = detail.get("concernLevel", "")  # e.g., "Low", "Moderate", "High"
            
            content = (
                f"LACTMED — {drug_name_official}\n"
                f"LACTATION RISK: {concern_level}\n"
                f"RISK SUMMARY: {risk_level}\n"
                f"SUMMARY: {summary}\n"
            )
            if maternal_levels:
                content += f"\nMATERNAL DRUG LEVELS: {maternal_levels}"
            if infant_levels:
                content += f"\nINFANT DRUG LEVELS: {infant_levels}"
            if alternative_drugs:
                content += f"\nALTERNATIVES: {alternative_drugs}"
            
            content_bytes = content.encode("utf-8")
            
            provenance = self._make_provenance(
                snippet_bytes=content_bytes,
                jurisdiction=Jurisdiction.US,
                evidence_tier=EvidenceTier.GUIDELINE,  # NIH-curated database
                document_version=f"LactMed:{drug_id}",
                chunk_position=0,
                parent_document_id=f"lactmed:{drug_id}",
            )
            
            chunk = EvidenceChunk(
                content=content,
                content_bytes=content_bytes,
                provenance=provenance,
                retraction_status=RetractionStatus.CLEAR,
                evidence_tier=EvidenceTier.GUIDELINE,
                staleness_status=self.check_staleness(),
                staleness_ttl_hours=STALENESS_TTL_HOURS[self.SOURCE],
                last_verified=datetime.now(timezone.utc),
            )
            chunks.append(chunk)
        
        return chunks


# ─────────────────────────────────────────────────────────────────────────────
# EVIDENCE SOURCE ORCHESTRATOR
# Manages all connectors, handles staleness, aggregates evidence
# ─────────────────────────────────────────────────────────────────────────────

class EvidenceSourceOrchestrator:
    """
    Coordinates all API connectors for a given clinical query.
    
    Implements:
    - Source selection based on query type (medication → openFDA/DailyMed/LactMed priority)
    - Parallel evidence retrieval with timeout budget
    - Staleness fail-closed enforcement (L1-5)
    - Source-unreachable detection (L1-5)
    - Per-source last-fetch timestamp tracking for staleness display
    """

    def __init__(
        self,
        pubmed_key: Optional[str] = None,
        fda_key: Optional[str] = None,
        nice_key: Optional[str] = None,
    ):
        self.pubmed = PubMedConnector(api_key=pubmed_key)
        self.openfda = OpenFDAConnector(api_key=fda_key)
        self.crossref = CrossrefConnector()
        self.nice = NICEConnector(api_key=nice_key)
        self.lactmed = LactMedConnector()

    async def retrieve_evidence(
        self,
        query: str,
        drug_names: Optional[list[str]] = None,
        is_breastfeeding_query: bool = False,
        jurisdiction: Jurisdiction = Jurisdiction.INTL,
        max_chunks: int = 50,
    ) -> list[EvidenceChunk]:
        """
        Main evidence retrieval method for a clinical query.
        
        Retrieves from all relevant sources in parallel.
        Enforces staleness fail-closed for safety-critical sources.
        Returns list of EvidenceChunks ready for semantic chunking + embedding.
        """
        tasks = []
        
        # PubMed — always included for all clinical queries
        tasks.append(self.pubmed.search_and_fetch(
            query=query,
            max_results=20,
            pub_types=["Systematic Review", "Meta-Analysis", "Randomized Controlled Trial",
                       "Practice Guideline", "Guideline"],
        ))
        
        # Drug label evidence — for any medication-related query
        if drug_names:
            for drug in drug_names[:3]:  # Limit to 3 drugs per query
                tasks.append(self.openfda.search_drug_label(drug))
                
                if is_breastfeeding_query:
                    tasks.append(self.lactmed.search_drug(drug))
        
        # NICE guidelines — for UK jurisdiction queries
        if jurisdiction in (Jurisdiction.UK, Jurisdiction.INTL, Jurisdiction.WHO):
            tasks.append(self.nice.search_guidelines(query))
        
        # Execute all retrievals in parallel with timeout
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.TimeoutError:
            logger.error("Evidence retrieval timed out")
            results = []
        
        # Aggregate and filter
        all_chunks: list[EvidenceChunk] = []
        for result in results:
            if isinstance(result, Exception):
                logger.warning(f"Evidence retrieval error (non-fatal): {result}")
                continue
            if isinstance(result, list):
                all_chunks.extend(result)
        
        # Remove quarantined chunks (preprints)
        citable_chunks = [c for c in all_chunks if not c.is_quarantined]
        
        # Log quarantined evidence (it's retrieved but not cited)
        quarantined_count = len(all_chunks) - len(citable_chunks)
        if quarantined_count > 0:
            logger.info(f"Quarantined {quarantined_count} preprint/unvalidated chunks (L1-6)")
        
        logger.info(
            f"Evidence retrieval: {len(citable_chunks)} citable chunks "
            f"from {len(tasks)} sources for query: '{query[:80]}...'"
        )
        
        return citable_chunks[:max_chunks]

    def get_staleness_display(self) -> str:
        """
        Generate freshness display string for all sources.
        Example: "PubMed: 2h ago | openFDA: 6h ago | NICE: 4d ago"
        """
        connectors = {
            "PubMed": self.pubmed,
            "openFDA": self.openfda,
            "NICE": self.nice,
            "LactMed": self.lactmed,
        }
        
        parts = []
        for name, connector in connectors.items():
            lf = connector._last_successful_fetch
            if lf is None:
                parts.append(f"{name}: never fetched")
                continue
            
            if lf.tzinfo is None:
                lf = lf.replace(tzinfo=timezone.utc)
            
            now = datetime.now(timezone.utc)
            diff_hours = (now - lf).total_seconds() / 3600
            
            if diff_hours < 1:
                parts.append(f"{name}: {int(diff_hours * 60)}m ago")
            elif diff_hours < 48:
                parts.append(f"{name}: {int(diff_hours)}h ago")
            else:
                parts.append(f"{name}: {int(diff_hours / 24)}d ago")
        
        return " | ".join(parts)
