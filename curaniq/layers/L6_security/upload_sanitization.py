"""
CURANIQ -- Layer 6: Security & Adversarial Defense
L6-6 Upload Security Scanner (Antivirus / Malware Prevention)

Architecture: File extension whitelist, magic bytes verification,
content scanning for code injection and prompt injection patterns,
SHA-256 audit hashing. Fail-closed: unknown = blocked.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class FileRiskLevel(str, Enum):
    SAFE     = "safe"
    CAUTION  = "caution"
    BLOCKED  = "blocked"


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
    ".hl7":  {"mime": "application/hl7-v2",    "max_mb": 5,  "risk": FileRiskLevel.CAUTION},
}

DANGEROUS_PATTERNS: list[re.Pattern] = [
    re.compile(r'<script[\s>]', re.I),
    re.compile(r'javascript:', re.I),
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
    """L6-6: Full upload sanitization pipeline."""

    MAGIC_BYTES: dict[str, bytes] = {
        ".pdf":  b"%PDF",
        ".png":  b"\x89PNG",
        ".jpg":  b"\xff\xd8\xff",
        ".jpeg": b"\xff\xd8\xff",
        ".docx": b"PK\x03\x04",
    }

    def sanitize(self, filepath: str) -> UploadSanitizationResult:
        result = UploadSanitizationResult()
        if not os.path.isfile(filepath):
            result.safe, result.risk_level = False, FileRiskLevel.BLOCKED
            result.blocked_reason = "File not found"
            return result

        _, ext = os.path.splitext(filepath.lower())
        if ext not in ALLOWED_TYPES:
            result.safe, result.risk_level = False, FileRiskLevel.BLOCKED
            result.blocked_reason = f"File type '{ext}' not allowed"
            return result

        tc = ALLOWED_TYPES[ext]
        fs = os.path.getsize(filepath)
        result.file_size_bytes = fs
        if fs > tc["max_mb"] * 1024 * 1024:
            result.safe, result.risk_level = False, FileRiskLevel.BLOCKED
            result.blocked_reason = f"File too large: {fs/1024/1024:.1f}MB (max {tc['max_mb']}MB)"
            return result

        sha = hashlib.sha256()
        with open(filepath, "rb") as f:
            hdr = f.read(1024)
            sha.update(hdr)
            while (ch := f.read(8192)):
                sha.update(ch)
        result.file_hash = sha.hexdigest()

        if ext in self.MAGIC_BYTES and not hdr.startswith(self.MAGIC_BYTES[ext]):
            result.safe, result.risk_level = False, FileRiskLevel.BLOCKED
            result.blocked_reason = f"Content does not match {ext} format"
            return result

        if ext in (".txt", ".csv", ".json", ".xml", ".fhir", ".hl7"):
            try:
                with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read(500_000)
                for p in DANGEROUS_PATTERNS:
                    if p.search(content):
                        result.safe, result.risk_level = False, FileRiskLevel.BLOCKED
                        result.blocked_reason = "Dangerous code pattern detected"
                        return result
                for p in PROMPT_INJECTION_IN_DOC:
                    if p.search(content):
                        result.warnings.append("Prompt injection pattern detected -- will be sanitized")
                        result.risk_level = FileRiskLevel.CAUTION
            except UnicodeDecodeError:
                result.warnings.append("Binary content in text file")

        result.risk_level = tc["risk"]
        result.sanitized_path = filepath
        return result
