"""
CURANIQ — Medical Evidence Operating System
Layer 2: Evidence Knowledge & Synthesis

L2-6: Jurisdiction-Aware Guideline Gating

This module re-exports the JurisdictionGuidanceGate and related types
from retraction_jurisdiction.py for clean imports.

Usage:
    from curaniq.layers.L2_curation.jurisdiction_gate import JurisdictionGuidanceGate
"""

from curaniq.layers.L2_curation.retraction_jurisdiction import (
    JurisdictionGuidanceGate,
    JurisdictionGuidelines,
    JURISDICTION_CONFIG,
)

__all__ = [
    "JurisdictionGuidanceGate",
    "JurisdictionGuidelines", 
    "JURISDICTION_CONFIG",
]
