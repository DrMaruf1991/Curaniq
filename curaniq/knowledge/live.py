"""
CURANIQ Clinical Knowledge — LiveEvidenceProvider.

This is the architecture's actual promise: at retrieval time, query the
governed evidence sources (L1 connectors), extract the clinical fact from
the live document, attach hash-bound provenance, and return.

Scope of this Session A implementation
======================================
This file establishes the LIVE PATH SHAPE — the protocol-conforming
provider that, in subsequent sessions, will be backed by real L1
connectors against DailyMed (SPL XML), openFDA, RxNorm, CredibleMeds,
LactMed, ATC/RxClass, and ISMP.

In Session A:
- The provider IS protocol-conforming and import-safe.
- It does NOT silently fall back to vendored data. If the L1 connector
  layer is not yet wired for a given fact, the provider raises
  KnowledgeUnavailableError. Callers (RouterProvider) decide what to do.
- The fail-closed semantics are the contract — never relax this.

Subsequent sessions (B–G in the plan) wire each connector in turn:
    Session B: RxNorm + ATC for synonyms / drug class
    Session C: DailyMed SPL fetch + section parser for dose/renal/
               hepatic/pediatric bounds (this is the hard one)
    Session D: LactMed + CredibleMeds
    Session E: openFDA labels + FAERS for DDI
    Session F: Migrate all remaining engines through this protocol
    Session G: Wire L4-14 hash-lock into the live extraction path

Each session's connector arrives via dependency injection into this
provider. The provider's external contract does not change.
"""
from __future__ import annotations

import logging
from typing import Iterator, Protocol

from curaniq.knowledge.exceptions import KnowledgeUnavailableError
from curaniq.knowledge.types import DoseBounds, FatalErrorRule

logger = logging.getLogger(__name__)


class _DoseBoundsConnector(Protocol):
    """L1 connector that can resolve dose bounds for a single drug."""
    def fetch_bounds(self, drug: str, jurisdiction: str) -> DoseBounds | None: ...


class _FatalRuleSource(Protocol):
    """Source of ISMP-derived fatal-error rules. Refreshable."""
    def fetch_rules(self) -> Iterator[FatalErrorRule]: ...


class LiveEvidenceProvider:
    """
    Live evidence-driven clinical knowledge. Authoritative.

    Sessions B-onward inject real connectors here. In Session A the
    connectors are None — every call raises KnowledgeUnavailableError,
    forcing the RouterProvider's policy (which honors clinician_prod
    fail-closed semantics) to surface as a refusal.

    This is intentional: it makes the unwired state visible rather
    than papered-over.
    """

    name = "live"
    is_authoritative = True

    def __init__(
        self,
        *,
        dose_bounds_connector: _DoseBoundsConnector | None = None,
        fatal_rule_source: _FatalRuleSource | None = None,
    ) -> None:
        self._dose_conn = dose_bounds_connector
        self._fatal_src = fatal_rule_source
        if dose_bounds_connector is None:
            logger.warning(
                "LiveEvidenceProvider: no dose_bounds_connector wired — "
                "get_dose_bounds() will raise KnowledgeUnavailableError. "
                "Wire a DailyMed/openFDA connector in Session C."
            )
        if fatal_rule_source is None:
            logger.info(
                "LiveEvidenceProvider: no fatal_rule_source — "
                "iter_fatal_error_rules() will raise. Wire ISMP source in Session B."
            )

    # ─── PROVIDER PROTOCOL ────────────────────────────────────────────────

    def get_dose_bounds(self, drug: str, jurisdiction: str = "US") -> DoseBounds | None:
        if self._dose_conn is None:
            raise KnowledgeUnavailableError(
                fact="dose_bounds", drug=drug,
                reason="no live connector wired (Session C target: DailyMed SPL)",
            )
        try:
            return self._dose_conn.fetch_bounds(drug.lower().strip(), jurisdiction)
        except Exception as exc:
            raise KnowledgeUnavailableError(
                fact="dose_bounds", drug=drug,
                reason=f"connector fetch failed: {type(exc).__name__}: {exc}",
            ) from exc

    def iter_fatal_error_rules(self) -> Iterator[FatalErrorRule]:
        if self._fatal_src is None:
            raise KnowledgeUnavailableError(
                fact="fatal_error_rules",
                reason="no fatal-rule source wired (Session B target: ISMP)",
            )
        try:
            yield from self._fatal_src.fetch_rules()
        except Exception as exc:
            raise KnowledgeUnavailableError(
                fact="fatal_error_rules",
                reason=f"source fetch failed: {type(exc).__name__}: {exc}",
            ) from exc
