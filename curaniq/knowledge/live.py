"""
CURANIQ Clinical Knowledge — LiveEvidenceProvider.

This is the architecture's actual promise: at retrieval time, query the
governed evidence sources (L1 connectors), extract the clinical fact from
the live document, attach hash-bound provenance, and return.

Wired connectors:
    Session B: RxNorm + RxClass (drug normalization, synonyms, ATC class)

Unwired sources (Sessions C-G):
    DailyMed SPL parser (renal/hepatic/pediatric/dose bounds)
    LactMed (pregnancy/lactation)
    CredibleMeds (QT risk)
    openFDA labels + Natural Medicines (DDI / food / herb)

Unwired sources raise KnowledgeUnavailableError. The router enforces
fail-closed in clinician_prod and falls back to vendored in demo.
"""
from __future__ import annotations

import logging
from typing import Iterator, Protocol

from curaniq.knowledge.exceptions import KnowledgeUnavailableError
from curaniq.knowledge.types import (
    AtcClassification,
    DoseBounds,
    DrugNormalization,
    FatalErrorRule,
)

logger = logging.getLogger(__name__)


class _DoseBoundsConnector(Protocol):
    def fetch_bounds(self, drug: str, jurisdiction: str) -> DoseBounds | None: ...


class _FatalRuleSource(Protocol):
    def fetch_rules(self) -> Iterator[FatalErrorRule]: ...


class _DrugNormalizationConnector(Protocol):
    """Implemented by RxNormConnector."""
    def normalize(self, name: str) -> DrugNormalization | None: ...
    def get_synonyms(self, name: str) -> list[str]: ...
    def get_atc(self, name_or_rxcui: str) -> AtcClassification | None: ...


class LiveEvidenceProvider:
    """
    Live evidence-driven clinical knowledge. Authoritative.

    Connectors are injected at construction. Missing connectors surface
    as KnowledgeUnavailableError — never silently degrade.
    """

    name = "live"
    is_authoritative = True

    def __init__(
        self,
        *,
        dose_bounds_connector: _DoseBoundsConnector | None = None,
        fatal_rule_source: _FatalRuleSource | None = None,
        drug_normalization_connector: _DrugNormalizationConnector | None = None,
    ) -> None:
        self._dose_conn = dose_bounds_connector
        self._fatal_src = fatal_rule_source
        self._norm_conn = drug_normalization_connector

        if dose_bounds_connector is None:
            logger.info(
                "LiveEvidenceProvider: dose_bounds connector unwired (Session C target: DailyMed)"
            )
        if drug_normalization_connector is None:
            logger.info(
                "LiveEvidenceProvider: drug-normalization connector unwired "
                "(inject RxNormConnector for live)"
            )

    # ─── L5-12 DOSE PLAUSIBILITY ──────────────────────────────────────────

    def get_dose_bounds(self, drug: str, jurisdiction: str = "US") -> DoseBounds | None:
        if self._dose_conn is None:
            raise KnowledgeUnavailableError(
                fact="dose_bounds", drug=drug,
                reason="no live connector wired (Session C target: DailyMed SPL)",
            )
        try:
            return self._dose_conn.fetch_bounds(drug.lower().strip(), jurisdiction)
        except KnowledgeUnavailableError:
            raise
        except Exception as exc:
            raise KnowledgeUnavailableError(
                fact="dose_bounds", drug=drug,
                reason=f"connector fetch failed: {type(exc).__name__}: {exc}",
            ) from exc

    def iter_fatal_error_rules(self) -> Iterator[FatalErrorRule]:
        if self._fatal_src is None:
            raise KnowledgeUnavailableError(
                fact="fatal_error_rules",
                reason="no fatal-rule source wired (rules served from vendored artifact)",
            )
        try:
            yield from self._fatal_src.fetch_rules()
        except Exception as exc:
            raise KnowledgeUnavailableError(
                fact="fatal_error_rules",
                reason=f"source fetch failed: {type(exc).__name__}: {exc}",
            ) from exc

    # ─── L2-1 ONTOLOGY NORMALIZATION (Session B) ──────────────────────────

    def normalize_drug(self, name: str) -> DrugNormalization | None:
        if self._norm_conn is None:
            raise KnowledgeUnavailableError(
                fact="drug_normalization", drug=name,
                reason="no drug-normalization connector wired (inject RxNormConnector)",
            )
        try:
            return self._norm_conn.normalize(name)
        except KnowledgeUnavailableError:
            raise
        except Exception as exc:
            raise KnowledgeUnavailableError(
                fact="drug_normalization", drug=name,
                reason=f"connector fetch failed: {type(exc).__name__}: {exc}",
            ) from exc

    def get_drug_synonyms(self, name: str) -> list[str]:
        if self._norm_conn is None:
            raise KnowledgeUnavailableError(
                fact="drug_synonyms", drug=name,
                reason="no drug-normalization connector wired",
            )
        try:
            return self._norm_conn.get_synonyms(name)
        except KnowledgeUnavailableError:
            raise
        except Exception as exc:
            raise KnowledgeUnavailableError(
                fact="drug_synonyms", drug=name,
                reason=f"connector fetch failed: {type(exc).__name__}: {exc}",
            ) from exc

    def get_atc_classification(self, name_or_rxcui: str) -> AtcClassification | None:
        if self._norm_conn is None:
            raise KnowledgeUnavailableError(
                fact="atc_classification", drug=name_or_rxcui,
                reason="no drug-normalization connector wired",
            )
        try:
            return self._norm_conn.get_atc(name_or_rxcui)
        except KnowledgeUnavailableError:
            raise
        except Exception as exc:
            raise KnowledgeUnavailableError(
                fact="atc_classification", drug=name_or_rxcui,
                reason=f"connector fetch failed: {type(exc).__name__}: {exc}",
            ) from exc
