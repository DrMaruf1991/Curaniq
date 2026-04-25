"""
RxNorm REST API connector.

RxNorm (https://rxnav.nlm.nih.gov) is the NLM's controlled drug terminology.
It is free, public-domain, and has no API key requirement. Rate limit is
20 requests per second per IP per the published terms of service.

This connector resolves free-text drug names to canonical RxNorm identities
(RxCUI + preferred term + term-type + synonym set), feeding the
ClinicalKnowledgeProvider's `normalize_drug()` and `get_drug_synonyms()`.

Endpoints used (all documented at https://lhncbc.nlm.nih.gov/RxNav/APIs/RxNormAPIs.html):

    GET /REST/rxcui.json?name={drug}&search={searchType}
        → resolve name → RxCUI(s)

    GET /REST/rxcui/{rxcui}/properties.json
        → canonical name, TTY, suppress flag

    GET /REST/rxcui/{rxcui}/related.json?tty=IN+BN+SY+SCD
        → related concepts (synonyms, brand names, ingredients)

    GET /REST/version.json
        → RxNorm release version (used as evidence_version in Provenance)

Live verification (run this on a machine with internet access):

    curl -s 'https://rxnav.nlm.nih.gov/REST/rxcui.json?name=metformin' | python -m json.tool
    curl -s 'https://rxnav.nlm.nih.gov/REST/rxcui/6809/related.json?tty=IN+BN+SY' | python -m json.tool
    curl -s 'https://rxnav.nlm.nih.gov/REST/version.json' | python -m json.tool

Offline test fixtures in `tests/fixtures/rxnorm/` are hand-constructed
to match the documented response shape exactly. The live integration
test (`tests/test_rxnorm_live.py`) validates the connector against
the real API; it is `pytest.skipif(not CURANIQ_RUN_LIVE)` so the
default test run does not require network.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from threading import Lock
from typing import Any

import httpx

from curaniq.knowledge.exceptions import KnowledgeUnavailableError
from curaniq.knowledge.types import AtcClassification, DrugNormalization, Provenance

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://rxnav.nlm.nih.gov"
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_RATE_LIMIT_PER_SEC = 18  # below NLM's 20/s ceiling

# RxNorm Term-Type priority for "best match" selection.
# Lower index = preferred. IN (ingredient) is the canonical form for
# CURANIQ's purposes; we want one normalized identity per drug substance,
# not per dosage-form variant.
_TTY_PREFERENCE = ("IN", "PIN", "MIN", "BN", "SBD", "SCD", "SY")


class RxNormConnector:
    """
    Synchronous RxNorm REST client.

    Construction is cheap; all I/O happens on demand. Single connector
    instance is safe to share across threads (httpx.Client is thread-safe;
    rate-limiter uses a lock).

    Args:
        base_url: Override for testing. Default = production rxnav.nlm.nih.gov.
        timeout_s: HTTP timeout per request.
        max_retries: Retries on 5xx and connection errors with exponential
                     backoff (0.5s, 1s, 2s).
        rate_limit_per_sec: Cap on outbound requests; set < 20 per NLM TOS.
        client: Inject a pre-configured httpx.Client for tests/mocking.
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_retries: int = DEFAULT_MAX_RETRIES,
        rate_limit_per_sec: int = DEFAULT_RATE_LIMIT_PER_SEC,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_s = timeout_s
        self._max_retries = max_retries
        self._min_interval_s = 1.0 / rate_limit_per_sec if rate_limit_per_sec > 0 else 0
        self._last_request_at = 0.0
        self._rate_lock = Lock()
        self._client = client or httpx.Client(
            timeout=timeout_s,
            headers={"User-Agent": "CURANIQ/3.6 (clinical-evidence-os)"},
        )
        # Cache: simple in-process dict; keyed by (path, params-tuple).
        # Negative results cached too — RxNorm names that don't resolve
        # are stable. Cache evicted only on connector restart.
        self._cache: dict[tuple, Any] = {}
        self._cached_version: str | None = None

    # ─── HTTP CORE ────────────────────────────────────────────────────────

    def _rate_limit(self) -> None:
        """Block until the rate-limit interval has elapsed since last request."""
        if self._min_interval_s == 0:
            return
        with self._rate_lock:
            elapsed = time.monotonic() - self._last_request_at
            wait = self._min_interval_s - elapsed
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict:
        """
        GET against RxNorm with rate-limiting and exponential-backoff retry.

        Raises:
            KnowledgeUnavailableError: network failure, 5xx after retries.
            httpx.HTTPStatusError propagated only for 4xx (let caller decide).
        """
        cache_key = (path, tuple(sorted((params or {}).items())))
        if cache_key in self._cache:
            return self._cache[cache_key]

        url = f"{self._base_url}{path}"
        last_exc: Exception | None = None
        backoff = 0.5
        for attempt in range(self._max_retries + 1):
            self._rate_limit()
            try:
                response = self._client.get(url, params=params)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.ReadError) as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    logger.warning("RxNorm %s attempt %d failed (%s); retrying in %.1fs",
                                   path, attempt + 1, type(exc).__name__, backoff)
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                raise KnowledgeUnavailableError(
                    fact=f"RxNorm {path}",
                    reason=f"network failure after {attempt + 1} attempts: {exc}",
                ) from exc

            if response.status_code == 200:
                try:
                    body = response.json()
                except ValueError as exc:
                    raise KnowledgeUnavailableError(
                        fact=f"RxNorm {path}",
                        reason=f"non-JSON response: {exc}",
                    ) from exc
                self._cache[cache_key] = body
                return body

            if response.status_code in (429, 500, 502, 503, 504):
                if attempt < self._max_retries:
                    logger.warning("RxNorm %s returned %d; retrying in %.1fs",
                                   path, response.status_code, backoff)
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                raise KnowledgeUnavailableError(
                    fact=f"RxNorm {path}",
                    reason=f"HTTP {response.status_code} after {attempt + 1} attempts",
                )

            # 4xx — let caller handle (e.g., 404 for unknown drug → return None)
            response.raise_for_status()

        # Unreachable, but mypy
        raise KnowledgeUnavailableError(
            fact=f"RxNorm {path}",
            reason=f"exhausted retries: {last_exc}",
        )

    # ─── PROVENANCE ───────────────────────────────────────────────────────

    def _get_version(self) -> str:
        """RxNorm release version (e.g., 'RxNorm_full_03032025'). Cached."""
        if self._cached_version is not None:
            return self._cached_version
        try:
            doc = self._get("/REST/version.json")
            self._cached_version = doc.get("version", "unknown")
        except KnowledgeUnavailableError:
            self._cached_version = "unknown"
        return self._cached_version

    def _provenance(self, source_url: str) -> Provenance:
        return Provenance(
            source="RXNORM",
            source_url=source_url,
            snapshot_date_iso=datetime.now(timezone.utc).isoformat(),
            evidence_version=self._get_version(),
            license_status="public_domain",
            extraction_method="live_api",
            is_authoritative=True,
        )

    # ─── PUBLIC API — DRUG NORMALIZATION ──────────────────────────────────

    def normalize(self, name: str) -> DrugNormalization | None:
        """
        Resolve free-text drug name to canonical RxNorm identity.

        Returns None iff RxNorm has no match.
        Raises KnowledgeUnavailableError on connector failure.

        Algorithm:
          1) GET /REST/rxcui.json?name={name}&search=2 (broader matching,
             allows close-but-not-exact matches like spelling variants).
          2) Pick best RxCUI (first one, matching RxNorm's own ranking).
          3) GET /REST/rxcui/{rxcui}/properties.json for canonical name + TTY.
          4) GET /REST/rxcui/{rxcui}/related.json?tty=IN+BN+SY for synonyms.
        """
        params = {"name": name.strip(), "search": "2"}
        try:
            doc = self._get("/REST/rxcui.json", params=params)
        except KnowledgeUnavailableError:
            raise

        id_group = doc.get("idGroup") or {}
        rxcui_list = id_group.get("rxnormId") or []
        if not rxcui_list:
            return None

        rxcui = str(rxcui_list[0])

        try:
            props_doc = self._get(f"/REST/rxcui/{rxcui}/properties.json")
        except KnowledgeUnavailableError:
            raise

        props = props_doc.get("properties") or {}
        canonical = props.get("name") or name.strip()
        tty = props.get("tty") or "IN"
        if tty not in {"IN", "BN", "SCD", "SBD", "MIN", "PIN", "SY", "PSN", "SCDC", "SCDF", "SCDG"}:
            tty = "IN"

        # Synonyms: include IN, BN, SY (synonym), and the synonym group from
        # rxcui itself. We want all human-readable name variants.
        synonyms_set: set[str] = {canonical}
        try:
            related_doc = self._get(
                f"/REST/rxcui/{rxcui}/related.json",
                params={"tty": "IN+BN+SY"},
            )
            related = (related_doc.get("relatedGroup") or {}).get("conceptGroup") or []
            for group in related:
                for concept in group.get("conceptProperties") or []:
                    syn = concept.get("name")
                    if syn:
                        synonyms_set.add(syn)
        except KnowledgeUnavailableError:
            # Synonyms are best-effort — if related lookup fails, return
            # the canonical name only.
            logger.warning("RxNorm related.json failed for rxcui=%s; returning canonical only", rxcui)

        synonyms = tuple(sorted(synonyms_set))
        prov = self._provenance(
            source_url=f"{self._base_url}/REST/rxcui/{rxcui}/properties.json"
        )

        return DrugNormalization(
            input_name=name,
            rxcui=rxcui,
            canonical_name=canonical,
            tty=tty,
            synonyms=synonyms,
            provenance=prov,
        )

    def get_synonyms(self, name: str) -> list[str]:
        """
        Return all RxNorm-known synonyms for `name`.

        Convenience wrapper around `normalize()`. Returns empty list iff
        RxNorm has no match.
        """
        norm = self.normalize(name)
        if norm is None:
            return []
        return list(norm.synonyms)

    # ─── PUBLIC API — ATC CLASSIFICATION ──────────────────────────────────

    def get_atc(self, name_or_rxcui: str) -> AtcClassification | None:
        """
        Return ATC classification(s) for a drug.

        Accepts either a drug name (auto-resolves to RxCUI first) or a
        bare RxCUI. The ATC class graph in RxNav is queried via the
        rxclass API.

        Endpoint: GET /REST/rxclass/class/byRxcui.json?rxcui={rxcui}&relaSource=ATC
        """
        # Resolve to RxCUI if input is a name
        if name_or_rxcui.isdigit():
            rxcui = name_or_rxcui
        else:
            norm = self.normalize(name_or_rxcui)
            if norm is None:
                return None
            rxcui = norm.rxcui

        try:
            doc = self._get(
                "/REST/rxclass/class/byRxcui.json",
                params={"rxcui": rxcui, "relaSource": "ATC"},
            )
        except KnowledgeUnavailableError:
            raise

        groups = (doc.get("rxclassDrugInfoList") or {}).get("rxclassDrugInfo") or []
        if not groups:
            return None

        codes_list: list[str] = []
        levels_list: list[int] = []
        for entry in groups:
            mc = entry.get("rxclassMinConceptItem") or {}
            class_id = mc.get("classId")
            if not class_id:
                continue
            # ATC code length determines level:
            #   1 char  = level 1 (Anatomical)
            #   3 chars = level 2 (Therapeutic)
            #   4 chars = level 3 (Pharmacological)
            #   5 chars = level 4 (Chemical)
            #   7 chars = level 5 (Substance)
            length = len(class_id)
            if length == 1:
                level = 1
            elif length == 3:
                level = 2
            elif length == 4:
                level = 3
            elif length == 5:
                level = 4
            elif length == 7:
                level = 5
            else:
                continue
            codes_list.append(class_id)
            levels_list.append(level)

        if not codes_list:
            return None

        # primary_atc = the longest (most specific) code in the set
        primary = max(codes_list, key=len) if any(len(c) == 7 for c in codes_list) else None

        return AtcClassification(
            rxcui=rxcui,
            atc_codes=tuple(codes_list),
            atc_levels=tuple(levels_list),
            primary_atc=primary,
            provenance=self._provenance(
                source_url=f"{self._base_url}/REST/rxclass/class/byRxcui.json?rxcui={rxcui}",
            ),
        )

    # ─── LIFECYCLE ────────────────────────────────────────────────────────

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "RxNormConnector":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
