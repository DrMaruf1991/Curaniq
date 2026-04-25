"""
CURANIQ — Medical Evidence Operating System
L7-17: Local Antibiogram & Resistance Pattern Integration

Architecture spec:
  'Imports hospital-specific antibiogram data into antimicrobial recommendation
  engine. Uses THIS hospital's resistance patterns, not national averages.
  
  Example: "At your facility, E. coli urinary isolates show 73% susceptibility
  to ciprofloxacin (below 80% threshold) — consider nitrofurantoin (96%
  susceptible) per your 2025 antibiogram + IDSA guidance."
  
  Data format: CSV/JSON import from microbiology lab, or FHIR DiagnosticReport.
  Unit-level granularity (ICU vs ward vs outpatient).
  Falls back to national/regional data if local unavailable.'

Evidence basis:
  - IDSA/SHEA antibiotic stewardship guidelines (Barlam et al., 2016, CID)
  - WHO GLASS (Global Antimicrobial Resistance Surveillance System)
  - Antibiogram-guided prescribing reduces treatment failure 20-40%
    (Klinker et al., 2015, Open Forum Infect Dis)
  - CLSI M39-A4: Analysis and Presentation of Cumulative Antimicrobial
    Susceptibility Test Data
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# CLSI M39 THRESHOLDS — evidence-based susceptibility cutoffs
# Per IDSA guidelines: empiric therapy should target ≥80% susceptibility
# ─────────────────────────────────────────────────────────────────

EMPIRIC_SUSCEPTIBILITY_THRESHOLD: float = 80.0  # IDSA: ≥80% for empiric use
UTI_SUSCEPTIBILITY_THRESHOLD: float = 80.0       # Some guidelines use 90% for UTI
CLSI_MINIMUM_ISOLATES: int = 30                   # CLSI M39: ≥30 isolates for valid %


class SusceptibilityCategory(str, Enum):
    SUSCEPTIBLE = "S"
    INTERMEDIATE = "I"
    RESISTANT = "R"
    NOT_TESTED = "NT"


class ClinicalUnit(str, Enum):
    """Hospital unit types for granular antibiogram data."""
    HOSPITAL_WIDE = "hospital_wide"
    ICU = "icu"
    MEDICAL_WARD = "medical_ward"
    SURGICAL_WARD = "surgical_ward"
    PEDIATRICS = "pediatrics"
    EMERGENCY = "emergency"
    OUTPATIENT = "outpatient"
    NEONATAL = "neonatal"


# ─────────────────────────────────────────────────────────────────
# ANTIBIOGRAM DATA MODELS
# ─────────────────────────────────────────────────────────────────

@dataclass
class SusceptibilityRecord:
    """
    One cell of an antibiogram: organism × antibiotic × unit.
    Represents the cumulative susceptibility percentage.
    """
    organism: str                    # E.g., "Escherichia coli"
    antibiotic: str                  # E.g., "Ciprofloxacin"
    susceptibility_pct: float        # 0-100 (e.g., 73.0 = 73% susceptible)
    total_isolates: int = 0          # N tested
    susceptible_count: int = 0
    intermediate_count: int = 0
    resistant_count: int = 0
    unit: ClinicalUnit = ClinicalUnit.HOSPITAL_WIDE
    specimen_type: Optional[str] = None   # "urine", "blood", "wound", etc.
    year: int = field(default_factory=lambda: datetime.now().year)
    quarter: Optional[int] = None         # Q1-Q4 for quarterly data
    data_source: str = "local_lab"

    @property
    def is_valid_clsi(self) -> bool:
        """CLSI M39: ≥30 isolates required for valid cumulative percentage."""
        return self.total_isolates >= CLSI_MINIMUM_ISOLATES

    @property
    def above_empiric_threshold(self) -> bool:
        """IDSA: ≥80% susceptibility for empiric therapy."""
        return self.susceptibility_pct >= EMPIRIC_SUSCEPTIBILITY_THRESHOLD

    @property
    def clinical_recommendation(self) -> str:
        """Generate a clinical recommendation based on susceptibility."""
        if not self.is_valid_clsi:
            return f"Insufficient data ({self.total_isolates} isolates, need ≥{CLSI_MINIMUM_ISOLATES})"
        if self.susceptibility_pct >= 90:
            return "RECOMMENDED for empiric therapy"
        elif self.susceptibility_pct >= EMPIRIC_SUSCEPTIBILITY_THRESHOLD:
            return "Acceptable for empiric therapy (≥80%)"
        elif self.susceptibility_pct >= 50:
            return f"AVOID for empiric therapy ({self.susceptibility_pct:.0f}% < 80% threshold)"
        else:
            return f"HIGH RESISTANCE — do not use empirically ({self.susceptibility_pct:.0f}%)"


@dataclass
class AntibiogramReport:
    """A complete antibiogram report for a tenant."""
    report_id: str = field(default_factory=lambda: str(uuid4()))
    tenant_id: str = ""
    facility_name: str = ""
    reporting_period: str = ""       # "2025" or "2025-Q1"
    records: list[SusceptibilityRecord] = field(default_factory=list)
    imported_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    total_organisms: int = 0
    total_antibiotics: int = 0


# ─────────────────────────────────────────────────────────────────
# FALLBACK: National/Regional resistance data (WHO GLASS / EARS-Net)
# Used when local antibiogram is not available
# ─────────────────────────────────────────────────────────────────

_REGIONAL_FALLBACK_DATA: dict[str, dict[str, float]] = {
    # WHO GLASS 2024 Central Asia estimates (approximate)
    # Format: "organism|antibiotic" → susceptibility%
    "Escherichia coli|Ciprofloxacin": 65.0,
    "Escherichia coli|Nitrofurantoin": 95.0,
    "Escherichia coli|Amoxicillin-clavulanate": 72.0,
    "Escherichia coli|Ceftriaxone": 78.0,
    "Escherichia coli|Trimethoprim-sulfamethoxazole": 55.0,
    "Escherichia coli|Fosfomycin": 97.0,
    "Escherichia coli|Meropenem": 99.0,
    "Klebsiella pneumoniae|Ceftriaxone": 60.0,
    "Klebsiella pneumoniae|Ciprofloxacin": 55.0,
    "Klebsiella pneumoniae|Meropenem": 90.0,
    "Klebsiella pneumoniae|Amikacin": 88.0,
    "Staphylococcus aureus|Oxacillin": 70.0,   # MRSA ~30%
    "Staphylococcus aureus|Vancomycin": 99.5,
    "Staphylococcus aureus|Trimethoprim-sulfamethoxazole": 95.0,
    "Staphylococcus aureus|Clindamycin": 75.0,
    "Streptococcus pneumoniae|Penicillin": 85.0,
    "Streptococcus pneumoniae|Ceftriaxone": 95.0,
    "Pseudomonas aeruginosa|Piperacillin-tazobactam": 75.0,
    "Pseudomonas aeruginosa|Ciprofloxacin": 70.0,
    "Pseudomonas aeruginosa|Meropenem": 80.0,
    "Pseudomonas aeruginosa|Ceftazidime": 78.0,
    "Enterococcus faecalis|Ampicillin": 95.0,
    "Enterococcus faecalis|Vancomycin": 98.0,
    "Enterococcus faecium|Vancomycin": 70.0,    # VRE rising
    "Enterococcus faecium|Linezolid": 99.0,
    "Acinetobacter baumannii|Meropenem": 40.0,  # Critical WHO priority
    "Acinetobacter baumannii|Colistin": 90.0,
}


# ─────────────────────────────────────────────────────────────────
# ANTIBIOGRAM ENGINE
# ─────────────────────────────────────────────────────────────────

class LocalAntibiogramEngine:
    """
    L7-17: Local Antibiogram & Resistance Pattern Integration.
    
    Per-tenant hospital antibiogram with unit-level granularity.
    Falls back to regional/WHO GLASS data when local unavailable.
    
    Import: CSV/JSON from microbiology lab.
    Query: organism × antibiotic × unit → susceptibility + recommendation.
    """

    def __init__(self, tenant_id: str = "default") -> None:
        self.tenant_id = tenant_id
        # Indexed: (organism_lower, antibiotic_lower, unit) → SusceptibilityRecord
        self._index: dict[tuple[str, str, str], SusceptibilityRecord] = {}
        self._reports: list[AntibiogramReport] = []
        self._organisms: set[str] = set()
        self._antibiotics: set[str] = set()

    def import_antibiogram(self, data: list[dict], facility_name: str = "") -> AntibiogramReport:
        """
        Import antibiogram data from CSV/JSON.
        
        Expected fields per row:
          organism, antibiotic, susceptibility_pct, total_isolates,
          [unit], [specimen_type], [year], [quarter]
        
        Returns AntibiogramReport with import statistics.
        """
        report = AntibiogramReport(
            tenant_id=self.tenant_id,
            facility_name=facility_name,
        )

        for row in data:
            try:
                record = SusceptibilityRecord(
                    organism=row["organism"].strip(),
                    antibiotic=row["antibiotic"].strip(),
                    susceptibility_pct=float(row["susceptibility_pct"]),
                    total_isolates=int(row.get("total_isolates", 0)),
                    susceptible_count=int(row.get("susceptible_count", 0)),
                    intermediate_count=int(row.get("intermediate_count", 0)),
                    resistant_count=int(row.get("resistant_count", 0)),
                    unit=ClinicalUnit(row.get("unit", "hospital_wide")),
                    specimen_type=row.get("specimen_type"),
                    year=int(row.get("year", datetime.now().year)),
                    quarter=int(row["quarter"]) if row.get("quarter") else None,
                )

                key = (
                    record.organism.lower(),
                    record.antibiotic.lower(),
                    record.unit.value,
                )
                self._index[key] = record
                self._organisms.add(record.organism)
                self._antibiotics.add(record.antibiotic)
                report.records.append(record)

            except (KeyError, ValueError, TypeError) as e:
                logger.warning(f"L7-17: Antibiogram row skipped: {e}")

        report.total_organisms = len(self._organisms)
        report.total_antibiotics = len(self._antibiotics)
        report.reporting_period = str(report.records[0].year) if report.records else "unknown"
        self._reports.append(report)

        logger.info(
            f"L7-17: Imported {len(report.records)} antibiogram records for "
            f"{self.tenant_id}: {report.total_organisms} organisms × "
            f"{report.total_antibiotics} antibiotics"
        )
        return report

    def query(
        self,
        organism: str,
        antibiotic: Optional[str] = None,
        unit: ClinicalUnit = ClinicalUnit.HOSPITAL_WIDE,
        specimen_type: Optional[str] = None,
    ) -> list[SusceptibilityRecord]:
        """
        Query antibiogram for an organism (optionally filtered by antibiotic/unit).
        Falls back to hospital-wide if unit-specific data unavailable.
        Falls back to regional data if local unavailable.
        """
        org_lower = organism.lower()
        results = []

        if antibiotic:
            # Specific organism × antibiotic query
            record = self._lookup(org_lower, antibiotic.lower(), unit)
            if record:
                results.append(record)
        else:
            # All antibiotics for this organism
            for (o, a, u), record in self._index.items():
                if o == org_lower and u == unit.value:
                    results.append(record)

            # Fallback to hospital-wide if unit-specific empty
            if not results and unit != ClinicalUnit.HOSPITAL_WIDE:
                for (o, a, u), record in self._index.items():
                    if o == org_lower and u == ClinicalUnit.HOSPITAL_WIDE.value:
                        results.append(record)

        # Sort by susceptibility (highest first — best empiric options first)
        results.sort(key=lambda r: -r.susceptibility_pct)
        return results

    def recommend_empiric(
        self,
        organism: str,
        unit: ClinicalUnit = ClinicalUnit.HOSPITAL_WIDE,
        infection_site: Optional[str] = None,
    ) -> dict[str, Any]:
        """
        Generate empiric antibiotic recommendation based on local antibiogram.
        
        Architecture example: 'At your facility, E. coli urinary isolates show 73%
        susceptibility to ciprofloxacin (below 80% threshold) — consider
        nitrofurantoin (96% susceptible).'
        
        Returns structured recommendation with evidence trail.
        """
        records = self.query(organism, unit=unit)
        data_source = "local_antibiogram"

        # Fallback to regional if no local data
        if not records:
            records = self._get_regional_fallback(organism)
            data_source = "regional_who_glass"

        if not records:
            return {
                "organism": organism,
                "data_available": False,
                "message": f"No antibiogram data available for {organism}. "
                          "Recommend culture-directed therapy.",
                "data_source": "none",
            }

        recommended = [r for r in records if r.above_empiric_threshold]
        avoid = [r for r in records if r.is_valid_clsi and not r.above_empiric_threshold]

        # Build recommendation text
        rec_text_parts = []
        if recommended:
            top = recommended[0]
            rec_text_parts.append(
                f"RECOMMENDED: {top.antibiotic} "
                f"({top.susceptibility_pct:.0f}% susceptible, "
                f"n={top.total_isolates})"
            )
        if avoid:
            avoid_names = [f"{r.antibiotic} ({r.susceptibility_pct:.0f}%)" for r in avoid[:3]]
            rec_text_parts.append(f"AVOID empirically: {', '.join(avoid_names)}")

        return {
            "organism": organism,
            "unit": unit.value,
            "data_available": True,
            "data_source": data_source,
            "recommended": [
                {
                    "antibiotic": r.antibiotic,
                    "susceptibility_pct": r.susceptibility_pct,
                    "total_isolates": r.total_isolates,
                    "recommendation": r.clinical_recommendation,
                    "valid_clsi": r.is_valid_clsi,
                }
                for r in recommended[:5]
            ],
            "avoid": [
                {
                    "antibiotic": r.antibiotic,
                    "susceptibility_pct": r.susceptibility_pct,
                    "total_isolates": r.total_isolates,
                    "recommendation": r.clinical_recommendation,
                }
                for r in avoid[:5]
            ],
            "message": " | ".join(rec_text_parts),
            "evidence_basis": (
                "IDSA/SHEA Antibiotic Stewardship Guidelines (Barlam 2016 CID): "
                "empiric therapy should target ≥80% local susceptibility. "
                f"Data source: {data_source}. "
                "CLSI M39-A4: ≥30 isolates required for valid cumulative %."
            ),
        }

    def _lookup(
        self, organism: str, antibiotic: str, unit: ClinicalUnit
    ) -> Optional[SusceptibilityRecord]:
        """Look up a specific organism×antibiotic×unit combination with fallback."""
        # Try exact unit
        record = self._index.get((organism, antibiotic, unit.value))
        if record:
            return record
        # Fallback to hospital-wide
        if unit != ClinicalUnit.HOSPITAL_WIDE:
            return self._index.get((organism, antibiotic, ClinicalUnit.HOSPITAL_WIDE.value))
        return None

    def _get_regional_fallback(self, organism: str) -> list[SusceptibilityRecord]:
        """Fall back to regional/WHO GLASS data when local unavailable."""
        org_lower = organism.lower()
        results = []
        for key, pct in _REGIONAL_FALLBACK_DATA.items():
            org_part, abx_part = key.split("|")
            if org_part.lower() == org_lower:
                results.append(SusceptibilityRecord(
                    organism=org_part,
                    antibiotic=abx_part,
                    susceptibility_pct=pct,
                    total_isolates=0,  # Regional aggregate — no isolate count
                    data_source="who_glass_regional",
                ))
        results.sort(key=lambda r: -r.susceptibility_pct)
        return results

    def summary(self) -> dict:
        """Audit summary."""
        return {
            "module": "L7-17",
            "tenant_id": self.tenant_id,
            "organisms": len(self._organisms),
            "antibiotics": len(self._antibiotics),
            "total_records": len(self._index),
            "reports_imported": len(self._reports),
            "regional_fallback_entries": len(_REGIONAL_FALLBACK_DATA),
        }


# Backward-compatible alias expected by c
# Backward-compatible wrapper expected by curaniq.core.pipeline
class InstitutionalAntibiogram(LocalAntibiogramEngine):
    def __init__(self, tenant_id: str = "default"):
        super().__init__(tenant_id=tenant_id)
