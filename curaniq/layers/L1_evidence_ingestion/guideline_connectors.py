"""
CURANIQ — Medical Evidence Operating System
Layer 1: Evidence Data Ingestion — Guideline Connectors

L1-9  NICE Guidelines API (UK National Institute for Health and Care Excellence)
L1-10 WHO Guidelines (World Health Organization)

Architecture: Real HTTP calls to guideline APIs. Env-driven config.
Falls back gracefully: no connectivity = empty results = pipeline
handles via No-Evidence Refusal Gate (L5-3).
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


def _http_get(url: str, params: Optional[dict] = None, timeout: int = 15) -> Optional[str]:
    """Stdlib HTTP GET — no external dependencies."""
    try:
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "User-Agent": "CURANIQ/1.0 (Medical Evidence OS)",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except Exception as e:
        logger.warning("HTTP GET failed for %s: %s", url, e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# L1-9: NICE GUIDELINES API
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class NICEGuideline:
    guideline_id: str = ""
    title: str = ""
    url: str = ""
    last_updated: Optional[datetime] = None
    summary: str = ""
    recommendation_count: int = 0
    jurisdiction: str = "UK"


class NICEGuidelineConnector:
    """
    L1-9: NICE Guidelines API connector.

    Uses NICE Content API (https://api.nice.org.uk/) to retrieve
    clinical guidelines. Free with certificate registration.

    Env: NICE_API_KEY (optional — increases rate limit)
    """

    BASE_URL = "https://www.nice.org.uk/syndication"

    def __init__(self):
        self._api_key = os.environ.get("NICE_API_KEY", "")

    def search_guidelines(self, query: str, max_results: int = 5) -> list[NICEGuideline]:
        """Search NICE for guidelines matching a clinical query."""
        url = f"{self.BASE_URL}/search"
        params = {
            "q": query,
            "ps": str(max_results),
            "om": "gid",  # Order by guideline ID
        }
        if self._api_key:
            params["apikey"] = self._api_key

        raw = _http_get(url, params)
        if not raw:
            return []

        try:
            data = json.loads(raw)
            results = data.get("documents", [])
            guidelines = []
            for doc in results[:max_results]:
                gl = NICEGuideline(
                    guideline_id=doc.get("id", ""),
                    title=doc.get("title", ""),
                    url=doc.get("url", ""),
                    summary=doc.get("teaser", "")[:500],
                )
                guidelines.append(gl)
            return guidelines
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("NICE API parse error: %s", e)
            return []

    def get_guideline_recommendations(self, guideline_id: str) -> list[dict]:
        """Retrieve specific recommendations from a NICE guideline."""
        url = f"{self.BASE_URL}/guidance/{guideline_id}/chapter/recommendations"
        raw = _http_get(url)
        if not raw:
            return []

        try:
            data = json.loads(raw)
            return data.get("recommendations", [])
        except (json.JSONDecodeError, KeyError):
            return []


# ─────────────────────────────────────────────────────────────────────────────
# L1-10: WHO GUIDELINES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class WHOGuideline:
    guideline_id: str = ""
    title: str = ""
    url: str = ""
    publication_year: Optional[int] = None
    who_region: str = "global"
    summary: str = ""


class WHOGuidelineConnector:
    """
    L1-10: WHO Guidelines connector.

    Retrieves WHO clinical guidelines via WHO GHL (Global Health Library).
    Critical for Uzbekistan/CIS markets where WHO guidelines are
    the primary reference over NICE/AHA.
    """

    BASE_URL = "https://apps.who.int/iris/rest"

    def search_guidelines(self, query: str, max_results: int = 5) -> list[WHOGuideline]:
        """Search WHO Institutional Repository for Open Access (IRIS)."""
        url = f"{self.BASE_URL}/discover"
        params = {
            "query": query,
            "scope": "10665",  # WHO publications scope
            "limit": str(max_results),
        }

        raw = _http_get(url, params)
        if not raw:
            return []

        try:
            data = json.loads(raw)
            results = data.get("response", {}).get("docs", [])
            guidelines = []
            for doc in results[:max_results]:
                gl = WHOGuideline(
                    guideline_id=doc.get("id", str(uuid4())),
                    title=doc.get("dc.title", ""),
                    url=doc.get("url", ""),
                    publication_year=doc.get("year"),
                    summary=doc.get("dc.description", "")[:500],
                )
                guidelines.append(gl)
            return guidelines
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("WHO API parse error: %s", e)
            return []
