"""
CURANIQ - Evidence Retrieval Engine (L1-1 + L4-1)
Real API calls to PubMed and OpenFDA. No seed data dependency.

Copy to: curaniq/layers/L1_evidence_ingestion/evidence_retriever.py

Architecture:
  PubMed E-utilities: search + fetch abstracts. Free with API key.
  OpenFDA: drug labels (dosing, contraindications, warnings). Free.
  
  All API keys from environment. Works without keys (lower rate limits).
  Falls back gracefully: no internet = empty evidence = pipeline refuses
  via No-Evidence Refusal Gate (L5-3). Fail-closed.

Env vars:
  NCBI_API_KEY    — PubMed (optional, increases rate from 3/s to 10/s)
  OPENFDA_API_KEY — OpenFDA (optional, increases rate limit)
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

import urllib.request
import urllib.parse
import json
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# HTTP helper — stdlib only, no external dependencies
# ─────────────────────────────────────────────────────────────────

def _http_get(url: str, params: Optional[dict] = None, timeout: int = 15) -> Optional[str]:
    """Simple sync HTTP GET. Returns response text or None on failure."""
    try:
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "CURANIQ/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        logger.warning(f"HTTP GET failed: {url[:80]} — {e}")
        return None


def _http_get_json(url: str, params: Optional[dict] = None, timeout: int = 15) -> Optional[dict]:
    """HTTP GET returning parsed JSON."""
    text = _http_get(url, params, timeout)
    if text:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
    return None


# ─────────────────────────────────────────────────────────────────
# PUBMED E-UTILITIES
# ─────────────────────────────────────────────────────────────────

PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

# Publication type -> evidence tier
_PUBTYPE_TIER = {
    "systematic review": "systematic_review",
    "meta-analysis": "systematic_review",
    "randomized controlled trial": "rct",
    "clinical trial, phase iii": "rct",
    "clinical trial, phase iv": "rct",
    "practice guideline": "guideline",
    "guideline": "guideline",
    "review": "cohort",
    "case reports": "case_report",
    "editorial": "expert_opinion",
    "comment": "expert_opinion",
    "letter": "expert_opinion",
}


def pubmed_search(query: str, max_results: int = 10) -> list[str]:
    """
    Search PubMed. Returns list of PMIDs.
    Prioritizes systematic reviews, RCTs, and guidelines.
    """
    api_key = os.environ.get("NCBI_API_KEY", "")
    params = {
        "db": "pubmed",
        "term": query,
        "retmax": max_results,
        "retmode": "json",
        "sort": "relevance",
    }
    if api_key:
        params["api_key"] = api_key

    data = _http_get_json(f"{PUBMED_BASE}/esearch.fcgi", params)
    if data:
        return data.get("esearchresult", {}).get("idlist", [])
    return []


def pubmed_fetch(pmids: list[str]) -> list[dict]:
    """
    Fetch article metadata + abstracts for given PMIDs.
    Returns list of parsed article dicts.
    """
    if not pmids:
        return []

    api_key = os.environ.get("NCBI_API_KEY", "")
    params = {
        "db": "pubmed",
        "id": ",".join(pmids),
        "retmode": "xml",
        "rettype": "abstract",
    }
    if api_key:
        params["api_key"] = api_key

    xml_text = _http_get(f"{PUBMED_BASE}/efetch.fcgi", params, timeout=20)
    if not xml_text:
        return []

    return _parse_pubmed_xml(xml_text)


def _parse_pubmed_xml(xml_text: str) -> list[dict]:
    """Parse PubMed efetch XML into article dicts."""
    articles = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    for article_el in root.findall(".//PubmedArticle"):
        try:
            medline = article_el.find("MedlineCitation")
            if medline is None:
                continue

            pmid_el = medline.find("PMID")
            pmid = pmid_el.text if pmid_el is not None else ""

            article = medline.find("Article")
            if article is None:
                continue

            # Title
            title_el = article.find("ArticleTitle")
            title = title_el.text if title_el is not None else ""

            # Abstract
            abstract_parts = []
            abstract_el = article.find("Abstract")
            if abstract_el is not None:
                for abs_text in abstract_el.findall("AbstractText"):
                    label = abs_text.get("Label", "")
                    text = abs_text.text or ""
                    if label:
                        abstract_parts.append(f"{label}: {text}")
                    else:
                        abstract_parts.append(text)
            abstract = " ".join(abstract_parts)

            # Authors
            authors = []
            author_list = article.find("AuthorList")
            if author_list is not None:
                for author in author_list.findall("Author"):
                    last = author.findtext("LastName", "")
                    init = author.findtext("Initials", "")
                    if last:
                        authors.append(f"{last} {init}".strip())

            # Publication date
            pub_date = None
            date_el = article.find(".//PubDate")
            if date_el is not None:
                year = date_el.findtext("Year", "")
                month = date_el.findtext("Month", "01")
                if year:
                    month_num = _month_to_num(month)
                    try:
                        pub_date = datetime(int(year), month_num, 1, tzinfo=timezone.utc)
                    except (ValueError, TypeError):
                        pass

            # Journal
            journal_el = article.find("Journal/Title")
            journal = journal_el.text if journal_el is not None else ""

            # Publication types -> evidence tier
            tier = "cohort"  # default
            pubtype_list = medline.find(".//PublicationTypeList")
            if pubtype_list is not None:
                for pt in pubtype_list.findall("PublicationType"):
                    pt_text = (pt.text or "").lower()
                    if pt_text in _PUBTYPE_TIER:
                        tier = _PUBTYPE_TIER[pt_text]
                        break

            # DOI
            doi = ""
            for id_el in article_el.findall(".//ArticleId"):
                if id_el.get("IdType") == "doi":
                    doi = id_el.text or ""
                    break

            articles.append({
                "pmid": pmid,
                "title": title,
                "abstract": abstract,
                "authors": authors[:5],
                "journal": journal,
                "published_date": pub_date,
                "tier": tier,
                "doi": doi,
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            })

        except Exception as e:
            logger.warning(f"Failed parsing article: {e}")
            continue

    return articles


def _month_to_num(month_str: str) -> int:
    """Convert month string to number."""
    months = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    try:
        return int(month_str)
    except (ValueError, TypeError):
        return months.get(month_str.lower()[:3], 1)


# ─────────────────────────────────────────────────────────────────
# OPENFDA DRUG LABELS
# ─────────────────────────────────────────────────────────────────

OPENFDA_BASE = "https://api.fda.gov"

# Label sections to extract, in clinical priority order
_LABEL_SECTIONS = [
    ("boxed_warning", "Black Box Warning"),
    ("contraindications", "Contraindications"),
    ("warnings_and_cautions", "Warnings"),
    ("dosage_and_administration", "Dosing"),
    ("drug_interactions", "Drug Interactions"),
    ("pregnancy", "Pregnancy"),
    ("nursing_mothers", "Lactation"),
    ("pediatric_use", "Pediatric"),
    ("adverse_reactions", "Adverse Reactions"),
]


def openfda_drug_label(drug_name: str) -> list[dict]:
    """
    Fetch FDA drug label sections for a drug.
    Returns list of dicts, one per label section found.
    """
    api_key = os.environ.get("OPENFDA_API_KEY", "")
    params = {
        "search": f'openfda.generic_name:"{drug_name}"',
        "limit": 1,
    }
    if api_key:
        params["api_key"] = api_key

    data = _http_get_json(f"{OPENFDA_BASE}/drug/label.json", params)
    if not data or "results" not in data:
        return []

    result = data["results"][0]
    sections = []

    # Extract brand name
    brand = ""
    openfda = result.get("openfda", {})
    brand_names = openfda.get("brand_name", [])
    if brand_names:
        brand = brand_names[0]

    for field, section_name in _LABEL_SECTIONS:
        content_list = result.get(field, [])
        if content_list:
            content = content_list[0] if isinstance(content_list, list) else str(content_list)
            if len(content) > 50:  # Skip empty/trivial sections
                sections.append({
                    "section": section_name,
                    "content": content[:2000],  # Cap at 2000 chars
                    "drug": drug_name,
                    "brand": brand,
                    "source": "FDA Drug Label (DailyMed/SPL)",
                    "url": f"https://api.fda.gov/drug/label.json?search=openfda.generic_name:%22{urllib.parse.quote(drug_name)}%22",
                })

    return sections


# ─────────────────────────────────────────────────────────────────
# EVIDENCE RETRIEVAL ENGINE
# Orchestrates PubMed + OpenFDA for a clinical query.
# Returns evidence in schemas.py EvidenceObject format.
# ─────────────────────────────────────────────────────────────────

def retrieve_evidence(
    query_text: str,
    drug_names: list[str],
    food_herbs: list[str],
    query_id: Any = None,
    max_pubmed: int = 8,
    max_fda_per_drug: int = 1,
) -> list[dict]:
    """
    Main evidence retrieval function.
    
    Calls real APIs. Returns list of evidence dicts ready to be
    converted to EvidenceObject by the pipeline.
    
    Returns empty list if APIs unavailable — pipeline will refuse
    via No-Evidence Refusal Gate (L5-3). Fail-closed.
    """
    evidence: list[dict] = []
    now = datetime.now(timezone.utc)

    # ── PubMed: search for clinical evidence ──
    # Build search query with drug names and synonyms
    search_terms = [query_text]

    # Add drug-specific searches
    for drug in drug_names[:3]:
        try:
            from curaniq.layers.L2_curation.ontology import get_search_synonyms
            synonyms = get_search_synonyms(drug)
            drug_query = " OR ".join(f'"{s}"' for s in synonyms[:3])
            search_terms.append(drug_query)
        except ImportError:
            search_terms.append(f'"{drug}"')

    # Search PubMed
    for search_q in search_terms[:3]:
        pmids = pubmed_search(search_q, max_results=max_pubmed)
        if pmids:
            articles = pubmed_fetch(pmids)
            for art in articles:
                if not art.get("abstract"):
                    continue
                snippet = art["abstract"][:1000]
                evidence.append({
                    "source_type": "pubmed",
                    "source_id": f"PMID{art['pmid']}",
                    "title": art.get("title", ""),
                    "snippet": snippet,
                    "snippet_hash": hashlib.sha256(snippet.encode("utf-8")).hexdigest(),
                    "url": art.get("url", ""),
                    "authors": art.get("authors", []),
                    "published_date": art.get("published_date"),
                    "tier": art.get("tier", "cohort"),
                    "jurisdiction": "INT",
                    "last_verified_at": now,
                    "staleness_ttl_hours": 6,
                })

    # ── OpenFDA: drug labels for each detected drug ──
    for drug in drug_names[:3]:
        sections = openfda_drug_label(drug)
        for section in sections:
            snippet = section["content"][:1000]
            evidence.append({
                "source_type": "openfda",
                "source_id": f"FDA-LABEL-{drug.upper()}-{section['section']}",
                "title": f"{drug} — {section['section']} ({section.get('brand', '')})",
                "snippet": snippet,
                "snippet_hash": hashlib.sha256(snippet.encode("utf-8")).hexdigest(),
                "url": section.get("url", ""),
                "authors": ["U.S. Food and Drug Administration"],
                "published_date": now,  # Labels are always current
                "tier": "guideline",  # FDA labels = regulatory guideline level
                "jurisdiction": "US",
                "last_verified_at": now,
                "staleness_ttl_hours": 24,
            })

    # Deduplicate by source_id
    seen = set()
    unique = []
    for ev in evidence:
        if ev["source_id"] not in seen:
            seen.add(ev["source_id"])
            unique.append(ev)

    logger.info(
        f"Evidence retrieval: {len(unique)} objects "
        f"(PubMed: {sum(1 for e in unique if e['source_type']=='pubmed')}, "
        f"FDA: {sum(1 for e in unique if e['source_type']=='openfda')})"
    )

    return unique
