"""
CURANIQ -- Layer 8: Clinician Experience & Interface
L8-8 Medication Coverage Scope Fence

Architecture: Declares which drugs and therapeutic areas CURANIQ has
validated coverage for vs "outside my verified scope." Prevents the
system from generating confident-sounding output for drugs/conditions
where it has NOT been validated.

This is a SAFETY mechanism: a system that admits its boundaries is
safer than one that confidently guesses. GPT/Gemini never refuse
based on scope -- they always attempt an answer regardless of whether
their training data covers the drug adequately.

Scope is defined by:
1. Drug formulary coverage (ATC codes with validation status)
2. Therapeutic area coverage (ICD-10 chapter-level)
3. Evidence source coverage (which APIs are active + fresh)
4. Jurisdictional coverage (regulatory frameworks per country)
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class CoverageStatus(str, Enum):
    VALIDATED    = "validated"       # Full validation suite passed
    PARTIAL      = "partial"         # Some evidence, not fully validated
    UNVALIDATED  = "unvalidated"     # No validation data
    OUT_OF_SCOPE = "out_of_scope"    # Explicitly excluded


@dataclass
class ScopeCheckResult:
    in_scope: bool = True
    coverage_status: CoverageStatus = CoverageStatus.VALIDATED
    drugs_in_scope: list[str] = field(default_factory=list)
    drugs_out_of_scope: list[str] = field(default_factory=list)
    conditions_in_scope: list[str] = field(default_factory=list)
    conditions_out_of_scope: list[str] = field(default_factory=list)
    scope_message: str = ""
    confidence_modifier: float = 1.0  # 1.0=full, 0.5=partial, 0.0=out_of_scope


class MedicationCoverageScopeFence:
    """
    L8-8: Declares verified medication coverage boundaries.

    Phase 1 (MVP) validated coverage:
    - ATC A10 (Antidiabetics): metformin, empagliflozin, semaglutide, insulin
    - ATC B01 (Antithrombotics): warfarin, heparin, enoxaparin, rivaroxaban, apixaban
    - ATC C (Cardiovascular): full chapter coverage
    - ATC J01 (Antibacterials): amoxicillin, azithromycin, ciprofloxacin, vancomycin, gentamicin
    - ATC N02 (Analgesics): acetaminophen, ibuprofen, naproxen
    - ATC N05/N06 (Psychotropics): fluoxetine, sertraline, lithium, valproic acid
    - ATC L01 (Antineoplastics): basic safety only, not dosing
    - Common DDI pairs from gold-standard databases

    Explicitly OUT OF SCOPE (Phase 1):
    - Orphan drugs / ultra-rare diseases
    - Compounding pharmacy formulations
    - Veterinary medications
    - Experimental/investigational drugs (pre-approval)
    - Herbal/supplement dosing (safety interactions only)
    """

    # ATC codes with Phase 1 validation status
    # Format: ATC prefix -> (status, description)
    VALIDATED_ATC_PREFIXES: dict[str, tuple[CoverageStatus, str]] = {
        "A02": (CoverageStatus.VALIDATED, "Antacids, anti-ulcerants"),
        "A10": (CoverageStatus.VALIDATED, "Antidiabetics"),
        "B01": (CoverageStatus.VALIDATED, "Antithrombotics"),
        "C01": (CoverageStatus.VALIDATED, "Cardiac therapy"),
        "C02": (CoverageStatus.VALIDATED, "Antihypertensives"),
        "C03": (CoverageStatus.VALIDATED, "Diuretics"),
        "C07": (CoverageStatus.VALIDATED, "Beta-blockers"),
        "C08": (CoverageStatus.VALIDATED, "Calcium channel blockers"),
        "C09": (CoverageStatus.VALIDATED, "ACE inhibitors / ARBs"),
        "C10": (CoverageStatus.VALIDATED, "Lipid modifying agents"),
        "G04": (CoverageStatus.PARTIAL, "Urologicals"),
        "H02": (CoverageStatus.VALIDATED, "Corticosteroids"),
        "J01": (CoverageStatus.VALIDATED, "Antibacterials"),
        "J05": (CoverageStatus.PARTIAL, "Antivirals"),
        "L01": (CoverageStatus.PARTIAL, "Antineoplastics -- safety only, not dosing"),
        "M01": (CoverageStatus.VALIDATED, "Anti-inflammatory / antirheumatic"),
        "N02": (CoverageStatus.VALIDATED, "Analgesics"),
        "N03": (CoverageStatus.VALIDATED, "Antiepileptics"),
        "N05": (CoverageStatus.VALIDATED, "Psycholeptics"),
        "N06": (CoverageStatus.VALIDATED, "Psychoanaleptics"),
        "R03": (CoverageStatus.VALIDATED, "Obstructive airway drugs"),
        "R06": (CoverageStatus.VALIDATED, "Antihistamines"),
    }

    # Explicitly out of scope
    OUT_OF_SCOPE_PATTERNS: list[re.Pattern] = [
        re.compile(r'\b(veterinary|animal|canine|feline|equine)\b', re.I),
        re.compile(r'\b(compounding|compounded|magistral)\b', re.I),
        re.compile(r'\b(investigational|experimental|phase\s*[0-3]\s*trial|pre-?approval)\b', re.I),
        re.compile(r'\b(homeopath|naturopath|ayurved)\b', re.I),
    ]

    # Well-known drug name -> ATC prefix mapping (INN names)
    DRUG_ATC_MAP: dict[str, str] = {
        "metformin": "A10", "empagliflozin": "A10", "semaglutide": "A10",
        "insulin": "A10", "gliclazide": "A10", "sitagliptin": "A10",
        "warfarin": "B01", "heparin": "B01", "enoxaparin": "B01",
        "rivaroxaban": "B01", "apixaban": "B01", "dabigatran": "B01",
        "aspirin": "B01", "clopidogrel": "B01",
        "amlodipine": "C08", "lisinopril": "C09", "enalapril": "C09",
        "valsartan": "C09", "losartan": "C09", "atorvastatin": "C10",
        "rosuvastatin": "C10", "bisoprolol": "C07", "metoprolol": "C07",
        "furosemide": "C03", "hydrochlorothiazide": "C03", "spironolactone": "C03",
        "digoxin": "C01", "amiodarone": "C01",
        "amoxicillin": "J01", "azithromycin": "J01", "ciprofloxacin": "J01",
        "vancomycin": "J01", "gentamicin": "J01", "meropenem": "J01",
        "fluconazole": "J02", "ibuprofen": "M01", "naproxen": "M01",
        "acetaminophen": "N02", "paracetamol": "N02",
        "fluoxetine": "N06", "sertraline": "N06", "escitalopram": "N06",
        "lithium": "N05", "valproic acid": "N03", "carbamazepine": "N03",
        "phenytoin": "N03", "pregabalin": "N03",
        "prednisolone": "H02", "dexamethasone": "H02",
        "salbutamol": "R03", "budesonide": "R03",
        "omeprazole": "A02", "pantoprazole": "A02",
    }

    def check_scope(self, drugs: list[str], query_text: str = "") -> ScopeCheckResult:
        """Check if all drugs and the query are within validated scope."""
        result = ScopeCheckResult()

        # Check for explicit out-of-scope patterns
        for pattern in self.OUT_OF_SCOPE_PATTERNS:
            if pattern.search(query_text):
                result.in_scope = False
                result.coverage_status = CoverageStatus.OUT_OF_SCOPE
                result.scope_message = (
                    "This query involves content outside CURANIQ's validated scope "
                    "(veterinary, compounding, investigational, or alternative medicine). "
                    "Please consult a specialist directly."
                )
                result.confidence_modifier = 0.0
                return result

        # Check each drug
        for drug in drugs:
            drug_lower = drug.lower().strip()
            atc = self.DRUG_ATC_MAP.get(drug_lower, "")

            if atc:
                prefix_status = self.VALIDATED_ATC_PREFIXES.get(atc[:3])
                if prefix_status:
                    status, desc = prefix_status
                    if status == CoverageStatus.VALIDATED:
                        result.drugs_in_scope.append(drug)
                    elif status == CoverageStatus.PARTIAL:
                        result.drugs_in_scope.append(drug)
                        result.confidence_modifier = min(result.confidence_modifier, 0.7)
                    else:
                        result.drugs_out_of_scope.append(drug)
                else:
                    result.drugs_out_of_scope.append(drug)
            else:
                # Unknown drug -- not in our map
                result.drugs_out_of_scope.append(drug)
                result.confidence_modifier = min(result.confidence_modifier, 0.5)

        if result.drugs_out_of_scope:
            out_str = ", ".join(result.drugs_out_of_scope)
            if result.drugs_in_scope:
                result.coverage_status = CoverageStatus.PARTIAL
                result.scope_message = (
                    f"Partial scope: {out_str} not in CURANIQ's Phase 1 validated "
                    "formulary. Information provided for these drugs may have lower "
                    "confidence. Recommend independent verification."
                )
            else:
                result.in_scope = False
                result.coverage_status = CoverageStatus.UNVALIDATED
                result.scope_message = (
                    f"Outside validated scope: {out_str}. CURANIQ has not validated "
                    "coverage for these medications. Output confidence is reduced. "
                    "Recommend consulting primary drug references directly."
                )
                result.confidence_modifier = 0.3

        return result
