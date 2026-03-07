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

class FileRiskLevel(str, Enum):
    SAFE     = "safe"
    CAUTION  = "caution"
    BLOCKED  = "blocked"


# File type whitelist with size limits
ALLOWED_TYPES: dict[str, dict] = {
    ".pdf":  {"mime": "application/pdf",       "max_mb": 50, "risk": FileRiskLevel.CAUTION},
    ".docx": {"mime": "application/vnd.openxmlformats", "max_mb": 25, "risk": FileRiskLevel.CAUTION},
    ".txt":  {"mime": "text/plain",            "max_mb": 10, "risk": FileRiskLevel.SAFE},
    ".csv":  {"mime": "text/csv",              "max_mb": 50, "risk": FileRiskLevel.SAFE},
    ".json": {"mime": "application/json",      "max_mb": 10, "risk": FileRiskLevel.SAFE},
    ".png":  {"mime": "image/png",             "max_mb": 20, "risk": FileRiskLevel.SAFE},
    ".jpg":  {"mime": "image/jpeg",            "max_mb": 20, "risk": FileRiskLevel.SAFE},
    ".jpeg": {"mime": "image/jpeg",            "max_mb": 20, "risk": FileRiskLevel.SAFE},
    ".xml":  {"mime": "application/xml",       "max_mb": 10, "risk": FileRiskLevel.CAUTION},
    ".fhir": {"mime": "application/fhir+json", "max_mb": 10, "risk": FileRiskLevel.CAUTION},
}

# Dangerous patterns to detect in file content
DANGEROUS_PATTERNS: list[re.Pattern] = [
    re.compile(r'<script[\s>]', re.I),
    re.compile(r'javascript:', re.I),
    re.compile(r'data:text/html', re.I),
    re.compile(r'eval\s*\(', re.I),
    re.compile(r'__import__\s*\(', re.I),
    re.compile(r'subprocess\.(call|Popen|run)', re.I),
    re.compile(r'os\.(system|exec|popen)', re.I),
]

PROMPT_INJECTION_IN_DOC: list[re.Pattern] = [
    re.compile(r'ignore\s+(all\s+)?(previous|prior)\s+(instructions?|prompts?)', re.I),
    re.compile(r'you\s+are\s+now\s+(a\s+)?(different|new|unrestricted)', re.I),
    re.compile(r'system\s*prompt', re.I),
    re.compile(r'<\s*/?\s*(?:system|user|assistant)\s*>', re.I),
]


@dataclass
class UploadSanitizationResult:
    safe: bool = True
    risk_level: FileRiskLevel = FileRiskLevel.SAFE
    blocked_reason: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    sanitized_path: Optional[str] = None
    file_hash: str = ""
    file_size_bytes: int = 0


class UploadSanitizer:
    """
    L6-6: Sanitizes uploaded documents before processing.

    Checks:
    1. File extension whitelist (reject .exe, .js, .py, etc.)
    2. File size limits per type
    3. Magic bytes verification (extension matches actual content)
    4. Content scanning for injection patterns
    5. Prompt injection detection in document text
    6. SHA-256 hash for audit trail
    """

    # Magic bytes for common file types
    MAGIC_BYTES: dict[str, bytes] = {
        ".pdf":  b"%PDF",
        ".png":  b"\x89PNG",
        ".jpg":  b"\xff\xd8\xff",
        ".jpeg": b"\xff\xd8\xff",
        ".docx": b"PK\x03\x04",
        ".xml":  b"<?xml",
    }

    def sanitize(self, filepath: str) -> UploadSanitizationResult:
        """Full sanitization pipeline for an uploaded file."""
        result = UploadSanitizationResult()

        # 1. Check file exists
        if not os.path.isfile(filepath):
            result.safe = False
            result.risk_level = FileRiskLevel.BLOCKED
            result.blocked_reason = "File not found"
            return result

        # 2. Extension whitelist
        _, ext = os.path.splitext(filepath.lower())
        if ext not in ALLOWED_TYPES:
            result.safe = False
            result.risk_level = FileRiskLevel.BLOCKED
            result.blocked_reason = f"File type '{ext}' not allowed. Permitted: {', '.join(ALLOWED_TYPES.keys())}"
            return result

        type_config = ALLOWED_TYPES[ext]

        # 3. File size check
        file_size = os.path.getsize(filepath)
        result.file_size_bytes = file_size
        max_bytes = type_config["max_mb"] * 1024 * 1024
        if file_size > max_bytes:
            result.safe = False
            result.risk_level = FileRiskLevel.BLOCKED
            result.blocked_reason = f"File too large: {file_size/1024/1024:.1f}MB (max {type_config['max_mb']}MB)"
            return result

        # 4. SHA-256 hash
        sha256 = hashlib.sha256()
        with open(filepath, "rb") as f:
            header = f.read(1024)
            sha256.update(header)
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                sha256.update(chunk)
        result.file_hash = sha256.hexdigest()

        # 5. Magic bytes verification
        if ext in self.MAGIC_BYTES:
            expected = self.MAGIC_BYTES[ext]
            if not header.startswith(expected):
                result.safe = False
                result.risk_level = FileRiskLevel.BLOCKED
                result.blocked_reason = f"File content does not match {ext} format (possible disguised file)"
                return result

        # 6. Content scanning for text-based files
        if ext in (".txt", ".csv", ".json", ".xml", ".fhir"):
            try:
                with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read(500_000)  # Scan first 500KB

                for pattern in DANGEROUS_PATTERNS:
                    if pattern.search(content):
                        result.safe = False
                        result.risk_level = FileRiskLevel.BLOCKED
                        result.blocked_reason = "Dangerous code pattern detected in file content"
                        return result

                for pattern in PROMPT_INJECTION_IN_DOC:
                    if pattern.search(content):
                        result.warnings.append("Prompt injection pattern detected in document — content will be sanitized")
                        result.risk_level = FileRiskLevel.CAUTION
            except UnicodeDecodeError:
                result.warnings.append("Binary content in text file — treating as opaque")

        result.risk_level = type_config["risk"]
        result.sanitized_path = filepath
        return result
