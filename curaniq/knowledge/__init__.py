"""
CURANIQ Clinical Knowledge layer.

The single abstraction barrier between clinical engines and the source
of clinical knowledge. See curaniq/knowledge/provider.py for the contract.

Quick reference:
    >>> from curaniq.knowledge import RouterProvider
    >>> kp = RouterProvider()
    >>> bounds = kp.get_dose_bounds("metformin", jurisdiction="US")
    >>> for rule in kp.iter_fatal_error_rules():
    ...     violated, msg = rule.evaluate(output_text)

Boot-time invariants:
    - In `clinician_prod`, instantiating VendoredSnapshotProvider raises.
    - In `clinician_prod`, RouterProvider does not construct a vendored
      fallback — KnowledgeUnavailableError propagates to the engine.

See docs/MIGRATION_PLAYBOOK.md for the procedure to migrate the next
clinical engine off hardcoded constants onto this provider.
"""
from curaniq.knowledge.connectors import RxNormConnector
from curaniq.knowledge.exceptions import (
    KnowledgeError,
    KnowledgeUnavailableError,
    ProvenanceMissingError,
    VendoredDataRefusedError,
)
from curaniq.knowledge.live import LiveEvidenceProvider
from curaniq.knowledge.provider import ClinicalKnowledgeProvider
from curaniq.knowledge.router import RouterProvider
from curaniq.knowledge.types import (
    AtcClassification,
    DoseBounds,
    DrugNormalization,
    FatalErrorRule,
    Provenance,
    compile_pattern,
)
from curaniq.knowledge.vendored import VendoredSnapshotProvider

__all__ = [
    "AtcClassification",
    "ClinicalKnowledgeProvider",
    "DoseBounds",
    "DrugNormalization",
    "FatalErrorRule",
    "KnowledgeError",
    "KnowledgeUnavailableError",
    "LiveEvidenceProvider",
    "Provenance",
    "ProvenanceMissingError",
    "RouterProvider",
    "RxNormConnector",
    "VendoredDataRefusedError",
    "VendoredSnapshotProvider",
    "compile_pattern",
]
