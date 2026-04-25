"""
CURANIQ Clinical Knowledge — exceptions.

The knowledge layer is fail-closed by contract. When clinical data is
unavailable from a governed source, the provider raises rather than
returning a fabricated default. Callers decide whether to refuse the
clinical query or warn and degrade.

This is a deliberate inversion of the historical pattern where
hardcoded constants silently filled in when retrieval failed —
that pattern is the root cause of the "evidence-free clinical answer"
failure mode the architecture exists to prevent.
"""
from __future__ import annotations


class KnowledgeError(Exception):
    """Base for all clinical knowledge layer errors."""


class KnowledgeUnavailableError(KnowledgeError):
    """
    Raised when a clinical fact cannot be retrieved from any authorized
    provider. In `clinician_prod` this MUST surface as a refusal to the
    clinician — never as a silent fallback to vendored or hardcoded data.
    """

    def __init__(self, fact: str, drug: str | None = None, reason: str = "") -> None:
        self.fact = fact
        self.drug = drug
        self.reason = reason
        msg = f"clinical knowledge '{fact}' unavailable"
        if drug:
            msg += f" for drug '{drug}'"
        if reason:
            msg += f" — {reason}"
        super().__init__(msg)


class VendoredDataRefusedError(KnowledgeError):
    """
    Raised when a `VendoredSnapshotProvider` is asked to serve in an
    environment where vendored snapshots are forbidden (`clinician_prod`).
    Always indicates a configuration / boot-time tripwire issue —
    never silently catch.
    """


class ProvenanceMissingError(KnowledgeError):
    """
    Raised when a vendored data file is loaded without complete provenance
    metadata (source, snapshot date, license, evidence_version). A vendored
    file with no provenance is indistinguishable from anonymous hardcoded
    data and is therefore treated as invalid.
    """
