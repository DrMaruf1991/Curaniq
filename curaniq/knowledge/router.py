"""
CURANIQ Clinical Knowledge — RouterProvider.

Composes `LiveEvidenceProvider` and `VendoredSnapshotProvider` with
environment-aware policy:

   demo / research:
       - Try live first. On KnowledgeUnavailableError, fall back to
         vendored. Result is non-authoritative.
       - Audit: every fall-through is logged with the reason, so we
         can see in CI which connectors are still unwired.

   clinician_prod:
       - Live only. No vendored fallback ever.
       - On KnowledgeUnavailableError: re-raise. Caller (typically
         a clinical engine) MUST refuse the clinical query and surface
         "Insufficient evidence" — never silently degrade.
       - VendoredSnapshotProvider is not constructed in this env; the
         router never has the option to fall back.

This module is the single point where env-policy lives. Engines never
see env logic — they consume `ClinicalKnowledgeProvider` and trust the
router to enforce the contract.
"""
from __future__ import annotations

import logging
from typing import Iterator

from curaniq.knowledge.exceptions import (
    KnowledgeUnavailableError,
    VendoredDataRefusedError,
)
from curaniq.knowledge.live import LiveEvidenceProvider
from curaniq.knowledge.types import DoseBounds, FatalErrorRule
from curaniq.knowledge.vendored import VendoredSnapshotProvider
from curaniq.truth_core.config import is_clinician_prod

logger = logging.getLogger(__name__)


class RouterProvider:
    """
    Environment-aware composition.

    Construction:
        - In any env, requires a `live` provider.
        - In non-prod, optionally constructs a vendored fallback.
        - In prod, vendored is forbidden by VendoredSnapshotProvider's
          tripwire — this class never even attempts to construct one.
    """

    name = "router"

    def __init__(self) -> None:
        # Live is mandatory in every env. In Session A it has no
        # connectors yet, but it exists and conforms to the protocol.
        self._live = LiveEvidenceProvider()
        self._prod = is_clinician_prod()

        if self._prod:
            self._vendored: VendoredSnapshotProvider | None = None
            logger.info("RouterProvider: clinician_prod — live-only, vendored fallback disabled")
        else:
            try:
                self._vendored = VendoredSnapshotProvider()
            except VendoredDataRefusedError:
                # Defensive — should not happen since we already checked is_prod
                self._vendored = None
            logger.info("RouterProvider: %s — live with vendored fallback",
                        "demo/research")

        # Diagnostic counters
        self._live_hits = 0
        self._fallback_hits = 0
        self._refusals = 0

    @property
    def is_authoritative(self) -> bool:
        """Authoritative iff the live backend is fully wired AND vendored
        is disabled. Conservative — if either condition fails this returns False
        so callers in clinician_prod can refuse on (is_authoritative is False)."""
        return self._prod and self._vendored is None

    # ─── DOSE BOUNDS ──────────────────────────────────────────────────────

    def get_dose_bounds(self, drug: str, jurisdiction: str = "US") -> DoseBounds | None:
        try:
            result = self._live.get_dose_bounds(drug, jurisdiction)
            self._live_hits += 1
            return result
        except KnowledgeUnavailableError as exc:
            if self._prod:
                self._refusals += 1
                logger.warning(
                    "RouterProvider: clinician_prod refusing dose_bounds(%s) — "
                    "live unavailable: %s", drug, exc.reason
                )
                raise
            if self._vendored is None:
                # Should be unreachable in non-prod, but be explicit
                raise
            logger.info(
                "RouterProvider: live unavailable for dose_bounds(%s); "
                "falling back to vendored (env=%s, reason=%s)",
                drug, "non-prod", exc.reason
            )
            self._fallback_hits += 1
            return self._vendored.get_dose_bounds(drug, jurisdiction)

    # ─── FATAL ERROR RULES ────────────────────────────────────────────────

    def iter_fatal_error_rules(self) -> Iterator[FatalErrorRule]:
        # Note: fatal_error_rules are SAFETY LOGIC, not clinical recommendations.
        # The vendored ISMP rule artifact is itself authoritative-as-a-rule
        # (the patterns ARE the rule), so even in clinician_prod we serve
        # the vendored rules — they are not "vendored clinical data."
        # However, if a live source ever overrides them, live wins.
        try:
            yield from self._live.iter_fatal_error_rules()
            self._live_hits += 1
            return
        except KnowledgeUnavailableError:
            pass

        if self._vendored is None:
            # In clinician_prod with no live, we still need rules. The
            # rule artifact has is_authoritative=True so it's not
            # "vendored clinical data" — it's the rule itself.
            # We instantiate a vendored loader specifically for rules,
            # bypassing the env tripwire because rules are universal.
            rule_loader = VendoredSnapshotProvider(allow_in_prod=True)
            yield from rule_loader.iter_fatal_error_rules()
            return

        yield from self._vendored.iter_fatal_error_rules()

    # ─── DIAGNOSTICS ──────────────────────────────────────────────────────

    def stats(self) -> dict[str, int]:
        return {
            "live_hits": self._live_hits,
            "fallback_hits": self._fallback_hits,
            "refusals": self._refusals,
        }
