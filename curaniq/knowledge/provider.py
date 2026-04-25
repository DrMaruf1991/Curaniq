"""
CURANIQ Clinical Knowledge — provider protocol.

The `ClinicalKnowledgeProvider` is the single abstraction barrier between
clinical engines (L3-2 Medication Intelligence, L5-12 Dose Plausibility,
L3-12 QT Risk, etc.) and the source of clinical knowledge.

Any consumer of clinical knowledge MUST go through a provider.
Hardcoding clinical knowledge inside an engine is statically forbidden
(see tests/test_no_hardcoded_clinical_knowledge.py).

Provider implementations:
- `LiveEvidenceProvider` (curaniq.knowledge.live)
    Calls L1 connectors at retrieval time. Authoritative. Required in
    `clinician_prod`. Fails closed.
- `VendoredSnapshotProvider` (curaniq.knowledge.vendored)
    Loads versioned snapshots from `curaniq/data/clinical/*.json` with
    full provenance metadata. Non-authoritative. Refused in `clinician_prod`.
    Used for `demo` and `research` envs and for unit tests.
- `RouterProvider` (curaniq.knowledge.router)
    Composes Live + Vendored with environment-aware policy.
"""
from __future__ import annotations

from typing import Iterator, Protocol, runtime_checkable

from curaniq.knowledge.types import DoseBounds, FatalErrorRule


@runtime_checkable
class ClinicalKnowledgeProvider(Protocol):
    """
    Abstract source of clinical knowledge. All clinical engines consume
    knowledge through this protocol — never via module-level constants.

    Methods MAY raise `KnowledgeUnavailableError`. Callers MUST handle
    by either refusing the clinical query or warning + degrading.
    Silent fallback to defaults is forbidden.

    The Session-A scope of this protocol covers L5-12 only:
        - get_dose_bounds
        - get_fatal_error_rules
    Future migrations extend this protocol as more engines move off
    hardcoded constants. See docs/MIGRATION_PLAYBOOK.md.
    """

    @property
    def name(self) -> str:
        """Stable identifier for logs and audit (e.g., 'vendored', 'live', 'router')."""
        ...

    @property
    def is_authoritative(self) -> bool:
        """True iff facts from this provider are live or recently-cached
        from governed sources. False for vendored snapshots.
        `clinician_prod` callers refuse non-authoritative providers."""
        ...

    # ─── L5-12 DOSE PLAUSIBILITY ───────────────────────────────────────────

    def get_dose_bounds(self, drug: str, jurisdiction: str = "US") -> DoseBounds | None:
        """
        Return single-dose plausibility bounds for `drug`.

        Returns None iff the drug is not covered by this provider
        (caller decides whether absence is an error). Raises
        `KnowledgeUnavailableError` iff the provider failed to reach
        its source (network outage, source down, license expired).
        """
        ...

    def iter_fatal_error_rules(self) -> Iterator[FatalErrorRule]:
        """
        Yield ISMP-derived sentinel rules for known-fatal medication errors.

        These are SAFETY LOGIC, not clinical recommendations. They are
        loaded from a versioned config artifact. The provider attaches
        the artifact's provenance to each rule.
        """
        ...
