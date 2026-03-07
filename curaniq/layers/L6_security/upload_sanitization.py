"""
CURANIQ — L6-6: Upload Sanitization
Re-exports from L2 terminology_version module where implementation lives.
"""
from curaniq.layers.L2_curation.terminology_version import (
    UploadSanitizer,
    UploadSanitizationResult,
    FileRiskLevel,
    ALLOWED_TYPES,
)

__all__ = ["UploadSanitizer", "UploadSanitizationResult", "FileRiskLevel", "ALLOWED_TYPES"]
