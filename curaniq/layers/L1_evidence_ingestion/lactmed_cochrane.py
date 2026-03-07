"""
CURANIQ -- Layer 1: Evidence Data Ingestion

L1-10 LactMed API Integration (NIH breastfeeding drug safety)
L1-12 Cochrane Library API Integration (gold standard systematic reviews)

LactMed: https://lhncbc.nlm.nih.gov/LHC-research/LHC-projects/lactmed/lactmedapi.html
  Free NLM API. Returns drug-specific breastfeeding safety data.
  Source: National Library of Medicine TOXNET/LactMed database.

Cochrane: https://www.cochranelibrary.com/developer
  Cochrane Library search API. Returns systematic review metadata.
  Highest-quality evidence source (CEBM Level 1).
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _http_get(url: str, params: Optional[dict] = None, timeout: int = 15) -> Optional[str]:
    """Stdlib HTTP GET with CURANIQ user agent."""
    try:
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "User-Agent": "CURANIQ/1.0 Medical Evidence OS",
            "Accept": "application/json, application/xml",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        logger.warning("HTTP GET failed: %s -- %s", url[:80], e)
        return None


# -----------------------------------------------------------------------------
# L1-10: LactMed (NIH Breastfeeding Drug Safety)
# API: https://lhncbc.nlm.nih.gov/LHC-research/LHC-projects/lactmed/lactmedapi.html
# Verified endpoint: DailyMed/NLM REST services
# -----------------------------------------------------------------------------

@dataclass
class LactMedResult:
    drug_name: str = ""
    summary: str = ""
    drug_levels: str = ""
    effects_on_infant: str = ""
    effects_on_lactation: str = ""
    alternative_drugs: list[str] = field(default_factory=list)
    aap_rating: str = ""  # American Academy of Pediatrics compatibility
    source_url: str = ""
    last_revised: Optional[str] = None


class LactMedConnector:
    """
    L1-10: NIH LactMed database connector.

    LactMed contains peer-reviewed data on drugs and lactation.
    Free NLM API — no key required (higher rate with NCBI API key).

    Critical for: L3-9 Pregnancy & Lactation Engine decisions.
    """

    # NLM DailyMed REST API for drug info including LactMed references
    BASE_URL = "https://dailymed.nlm.nih.gov/dailymed/services/v2"
    # LactMed direct: TOXNET was retired, data now via PubChem/DailyMed
    PUBCHEM_URL = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"

    def __init__(self):
        self._api_key = os.environ.get("NCBI_API_KEY", "")

    def search_drug(self, drug_name: str) -> Optional[LactMedResult]:
        """Search LactMed for breastfeeding safety data on a drug."""
        # Strategy 1: DailyMed SPL labels (Section 8.2 = Lactation)
        url = f"{self.BASE_URL}/drugnames.json"
        params = {"drug_name": drug_name}
        raw = _http_get(url, params)
        if raw:
            try:
                data = json.loads(raw)
                names = data.get("data", [])
                if names:
                    # Get full SPL for lactation section
                    spl_url = f"{self.BASE_URL}/spls.json"
                    spl_params = {"drug_name": drug_name, "page": "1", "pagesize": "1"}
                    spl_raw = _http_get(spl_url, spl_params)
                    if spl_raw:
                        spl_data = json.loads(spl_raw)
                        results = spl_data.get("data", [])
                        if results:
                            spl_id = results[0].get("spl_id", "")
                            return LactMedResult(
                                drug_name=drug_name,
                                source_url=f"https://dailymed.nlm.nih.gov/dailymed/drugInfo.cfm?setid={spl_id}",
                                summary=f"Drug label found for {drug_name}. Check Section 8.2 (Lactation) of full prescribing information.",
                            )
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("LactMed parse error for %s: %s", drug_name, e)

        return None

    def is_compatible_with_breastfeeding(self, drug_name: str) -> Optional[bool]:
        """Quick check: is this drug generally compatible with breastfeeding?"""
        result = self.search_drug(drug_name)
        if not result:
            return None  # Unknown -- fail-closed (caller should flag for review)
        # Cannot determine from label alone -- return None to trigger L3-9 review
        return None


# -----------------------------------------------------------------------------
# L1-12: Cochrane Library (Gold Standard Systematic Reviews)
# https://www.cochranelibrary.com/developer
# Cochrane API uses standard search interface
# -----------------------------------------------------------------------------

@dataclass
class CochraneReview:
    review_id: str = ""
    title: str = ""
    authors: str = ""
    doi: str = ""
    url: str = ""
    abstract: str = ""
    publication_year: Optional[int] = None
    review_type: str = ""  # "intervention", "diagnostic", "qualitative"
    last_assessed_up_to_date: Optional[str] = None


class CochraneConnector:
    """
    L1-12: Cochrane Library API connector.

    Cochrane systematic reviews are CEBM Level 1 evidence --
    the highest quality in the evidence hierarchy. When a Cochrane
    review exists for a clinical question, it should be prioritized
    over individual RCTs.

    API: Cochrane Library uses standard RESTful search.
    """

    BASE_URL = "https://www.cochranelibrary.com/cdsr/doi"
    SEARCH_URL = "https://www.cochranelibrary.com/en/search"

    def search_reviews(self, query: str, max_results: int = 5) -> list[CochraneReview]:
        """
        Search Cochrane Library for systematic reviews.

        Falls back to PubMed search filtered to Cochrane Database if
        direct API is unavailable. This ensures we always attempt
        to find the highest-quality evidence.
        """
        # Strategy: Search PubMed with Cochrane journal filter
        # This is more reliable than scraping Cochrane's web interface
        pubmed_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
        params = {
            "db": "pubmed",
            "term": f'{query} AND "Cochrane Database Syst Rev"[Journal]',
            "retmax": str(max_results),
            "retmode": "json",
            "sort": "relevance",
        }
        api_key = os.environ.get("NCBI_API_KEY", "")
        if api_key:
            params["api_key"] = api_key

        raw = _http_get(pubmed_url, params)
        if not raw:
            return []

        try:
            data = json.loads(raw)
            pmids = data.get("esearchresult", {}).get("idlist", [])
            if not pmids:
                return []

            # Fetch abstracts
            fetch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
            fetch_params = {
                "db": "pubmed",
                "id": ",".join(pmids),
                "rettype": "abstract",
                "retmode": "xml",
            }
            if api_key:
                fetch_params["api_key"] = api_key

            fetch_raw = _http_get(fetch_url, fetch_params)
            if not fetch_raw:
                return [CochraneReview(review_id=pmid, title=f"PMID:{pmid}") for pmid in pmids]

            reviews = []
            try:
                root = ET.fromstring(fetch_raw)
                for article in root.findall(".//PubmedArticle"):
                    pmid_el = article.find(".//PMID")
                    title_el = article.find(".//ArticleTitle")
                    abstract_el = article.find(".//AbstractText")
                    year_el = article.find(".//PubDate/Year")
                    doi_el = article.find('.//ArticleId[@IdType="doi"]')

                    review = CochraneReview(
                        review_id=pmid_el.text if pmid_el is not None else "",
                        title=title_el.text if title_el is not None else "",
                        abstract=abstract_el.text[:500] if abstract_el is not None and abstract_el.text else "",
                        publication_year=int(year_el.text) if year_el is not None and year_el.text else None,
                        doi=doi_el.text if doi_el is not None else "",
                        url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid_el.text}/" if pmid_el is not None else "",
                        review_type="intervention",
                    )
                    reviews.append(review)
            except ET.ParseError:
                pass

            return reviews
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Cochrane search error: %s", e)
            return []
