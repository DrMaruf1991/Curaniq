"""
CURANIQ — Medical Evidence Operating System

L2-15 Terminology Version Control (track RxNorm/SNOMED/ICD versions)
L6-6  Upload Sanitization (document security for untrusted inputs)
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# L2-15: TERMINOLOGY VERSION CONTROL
# ─────────────────────────────────────────────────────────────────────────────

class TerminologySystem(str, Enum):
    RXNORM      = "rxnorm"
    SNOMED_CT   = "snomed_ct"
    ICD_10      = "icd_10"
    ICD_11      = "icd_11"
    LOINC       = "loinc"
    ATC         = "atc"
    UMLS        = "umls"


@dataclass
class TerminologyVersion:
    system: TerminologySystem
    version: str
    release_date: Optional[datetime] = None
    loaded_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    concept_count: int = 0
    checksum: str = ""


class TerminologyVersionControl:
    """
    L2-15: Tracks which version of each terminology system is active.

    Critical for reproducibility: a drug query today must produce the
    same RxNorm mapping as the same query 6 months from now IF the
    same terminology version is pinned.

    Every evidence pack records which terminology versions were used.
    """

    def __init__(self):
        self._versions: dict[TerminologySystem, TerminologyVersion] = {}

    def register_version(
        self,
        system: TerminologySystem,
        version: str,
        concept_count: int = 0,
        checksum: str = "",
    ) -> TerminologyVersion:
        """Register the active version of a terminology system."""
        tv = TerminologyVersion(
            system=system,
            version=version,
            concept_count=concept_count,
            checksum=checksum,
        )
        self._versions[system] = tv
        logger.info("Terminology %s pinned to version %s (%d concepts)",
                     system.value, version, concept_count)
        return tv

    def get_active_version(self, system: TerminologySystem) -> Optional[TerminologyVersion]:
        return self._versions.get(system)

    def get_version_manifest(self) -> dict[str, str]:
        """Get all active versions for evidence pack pinning."""
        return {
            sys.value: ver.version
            for sys, ver in self._versions.items()
        }

    def detect_version_drift(self, system: TerminologySystem, new_version: str) -> bool:
        """Check if a terminology update would cause version drift."""
        current = self._versions.get(system)
        if not current:
            return False
        return current.version != new_version


# ─────────────────────────────────────────────────────────────────────────────
# L6-6: UPLOAD SANITIZATION
# ─────────────────────────────────────────────────────────────────────────────
