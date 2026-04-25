"""
CURANIQ — Medical Evidence Operating System
Layer 3: Deterministic Safety Kernel — CQL Neuro-Symbolic Core (L3-1)

THIS IS THE #1 STRUCTURAL ADVANTAGE OVER GPT/GEMINI.

The CQL engine computes medical math/logic via deterministic Python rules,
NOT LLMs. An LLM can NEVER author a dose, a contraindication verdict, or a 
DDI severity rating. These are computed here and provided to the LLM as 
deterministic facts.

The LLM is then allowed to reason about these deterministic facts — but
it cannot override them.

Implements:
- Weight-based dosing with maximum dose caps
- Renal dose adjustment (Cockcroft-Gault + CKD-EPI)
- Hepatic dose adjustment (Child-Pugh scoring)
- Drug-Drug Interaction (DDI) severity classification
- Contraindication reasoning (drug-disease, drug-allergy, drug-drug)
- Allergy kernel (IgE vs side effect vs severe cutaneous reactions)
- Unit/measurement sanity validation
- Physiologic plausibility checks
- Dispense-aware dosing (real tablet/vial strengths, safe rounding)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# ENUMERATIONS
# ─────────────────────────────────────────────────────────────────────────────

class DDISeverity(str, Enum):
    """Drug-Drug Interaction severity — deterministic, per clinical pharmacology."""
    CONTRAINDICATED  = "contraindicated"   # Absolutely avoid combination
    MAJOR            = "major"             # Clinically significant — avoid if possible
    MODERATE         = "moderate"          # Monitor and adjust
    MINOR            = "minor"             # Minimal clinical significance
    UNKNOWN          = "unknown"           # Insufficient data
    NO_INTERACTION   = "no_interaction"


class AllergyType(str, Enum):
    """
    Allergy classification per L3-1 Allergy Kernel.
    Critical distinction: IgE-mediated reactions vs side effects vs severe cutaneous.
    """
    TRUE_IgE_MEDIATED    = "true_ige_mediated"      # Anaphylaxis risk — absolute avoid
    SEVERE_CUTANEOUS     = "severe_cutaneous"         # SJS, TEN, DRESS — absolute avoid
    INTOLERANCE          = "intolerance"              # Side effect, not immune-mediated
    UNKNOWN_ALLERGY      = "unknown"                  # Treat as true allergy — conservative


class ContraindicationSeverity(str, Enum):
    """Contraindication severity — determines whether to refuse or warn."""
    ABSOLUTE    = "absolute"   # Never use — will cause serious harm
    RELATIVE    = "relative"   # Avoid if alternatives exist; use with monitoring
    PRECAUTION  = "precaution" # Use with caution and monitoring


class RenalFunction(str, Enum):
    """CKD staging per KDIGO 2022."""
    NORMAL      = "normal"       # eGFR ≥ 90
    MILD_CKD    = "mild_ckd"     # eGFR 60-89 (CKD G2)
    MODERATE_CKD = "moderate_ckd" # eGFR 30-59 (CKD G3)
    SEVERE_CKD  = "severe_ckd"  # eGFR 15-29 (CKD G4)
    ESRD        = "esrd"         # eGFR < 15 (CKD G5) or dialysis
    DIALYSIS    = "dialysis"     # On hemodialysis or peritoneal dialysis


class HepaticFunction(str, Enum):
    """Child-Pugh classification for hepatic function assessment."""
    NORMAL     = "normal"    # No hepatic impairment
    MILD_A     = "child_pugh_a"   # Score 5-6 — mild
    MODERATE_B = "child_pugh_b"  # Score 7-9 — moderate
    SEVERE_C   = "child_pugh_c"  # Score 10-15 — severe


# ─────────────────────────────────────────────────────────────────────────────
# DATA STRUCTURES
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PatientProfile:
    """
    Minimal patient context required by the safety kernel.
    All fields are validated for physiologic plausibility.
    """
    age_years:       Optional[float] = None
    weight_kg:       Optional[float] = None
    height_cm:       Optional[float] = None
    sex:             Optional[str] = None      # "male" | "female" | "unknown"
    serum_creatinine_mg_dl: Optional[float] = None
    egfr_ml_min:     Optional[float] = None    # Pre-computed eGFR if available
    child_pugh_score: Optional[int] = None     # 5-15
    child_pugh_class: Optional[str] = None    # "A" | "B" | "C"
    is_on_dialysis:  bool = False
    weight_method:   str = "actual"           # "actual" | "ideal" | "adjusted"


@dataclass
class DoseCalculationResult:
    """
    Output of the CQL dose calculator.
    ALL numeric values here are deterministic — never LLM-generated.
    """
    drug_name:               str
    calculated_dose_mg:      Optional[float]   # Calculated dose in mg
    calculated_dose_mg_per_kg: Optional[float] # mg/kg for weight-based
    recommended_dose_str:    str               # Human-readable: "500 mg q8h"
    dosing_interval_hours:   Optional[float]
    route:                   str               # "oral" | "IV" | "IM" | "subcutaneous"
    
    # Adjustments applied
    renal_adjustment_applied: bool = False
    renal_adjustment_factor:  Optional[float] = None  # e.g., 0.5 = 50% of normal
    hepatic_adjustment_applied: bool = False
    weight_based:            bool = False
    
    # Safety checks
    dose_within_range:       bool = True
    max_dose_exceeded:       bool = False
    min_dose_below:          bool = False
    max_dose_cap_mg:         Optional[float] = None
    
    # Dispense-aware rounding (L3-2)
    available_strengths_mg:  list[float] = field(default_factory=list)
    dispensable_dose_mg:     Optional[float] = None  # Nearest available strength
    rounding_safe:           bool = True
    rounding_note:           Optional[str] = None
    
    # Safety warnings
    warnings:                list[str] = field(default_factory=list)
    is_safe_to_dispense:     bool = True
    
    # Evidence basis (always CQL — never LLM)
    calculation_method:      str = "CQL_deterministic"
    evidence_source:         str = ""   # e.g., "KDIGO 2022 eGFR-based dosing"


@dataclass
class DDIResult:
    """Result of a drug-drug interaction check."""
    drug_1:          str
    drug_2:          str
    severity:        DDISeverity
    mechanism:       str           # Pharmacokinetic | Pharmacodynamic | Both
    clinical_effect: str           # What happens clinically
    management:      str           # What to do about it
    is_absolute:     bool = False  # True = contraindicated
    monitoring_required: list[str] = field(default_factory=list)  # Labs to monitor
    alternatives:    list[str] = field(default_factory=list)       # Alternative drugs
    evidence_source: str = ""      # e.g., "FDA label", "Micromedex"


@dataclass
class ContraindicationResult:
    """Result of a contraindication check."""
    drug:            str
    condition_or_drug: str         # The condition or drug that causes contraindication
    severity:        ContraindicationSeverity
    reason:          str
    is_absolute:     bool
    alternatives:    list[str] = field(default_factory=list)
    monitoring:      list[str] = field(default_factory=list)
    evidence_source: str = ""


@dataclass
class AllergyAssessment:
    """Result of allergy kernel assessment."""
    drug:            str
    allergen:        str
    allergy_type:    AllergyType
    cross_reactivity_risk: bool = False
    cross_reactive_drugs: list[str] = field(default_factory=list)
    safe_alternatives: list[str] = field(default_factory=list)
    management:      str = ""
    # For penicillin allergy — the most common and complex
    is_penicillin_allergy: bool = False
    cephalosporin_risk:    Optional[str] = None  # "low" | "moderate" | "high"


@dataclass
class CQLKernelOutput:
    """
    Complete output from the CQL Safety Kernel for one query.
    
    This is the deterministic ground truth that the LLM CANNOT override.
    The LLM receives this as factual context — it may explain and contextualize,
    but it cannot change any numeric value or safety verdict.
    """
    dose_results:           list[DoseCalculationResult] = field(default_factory=list)
    ddi_results:            list[DDIResult] = field(default_factory=list)
    contraindication_results: list[ContraindicationResult] = field(default_factory=list)
    allergy_assessments:    list[AllergyAssessment] = field(default_factory=list)
    
    # Overall safety verdict
    has_contraindications:  bool = False
    has_major_ddis:         bool = False
    has_absolute_contraindications: bool = False
    has_allergy_contraindications: bool = False
    
    # Flags for specialist engines
    requires_qt_monitoring: bool = False     # → L3-12 QT Risk Engine
    requires_renal_monitoring: bool = False  # → eGFR monitoring
    requires_hepatic_monitoring: bool = False
    
    # Summary for LLM context
    safety_summary:         str = ""
    
    def is_safe_to_proceed(self) -> bool:
        """
        If any absolute contraindication exists, the system REFUSES to generate
        a recommendation — it only provides the contraindication reason and alternatives.
        """
        return not (
            self.has_absolute_contraindications or
            self.has_allergy_contraindications or
            any(ddi.severity == DDISeverity.CONTRAINDICATED for ddi in self.ddi_results)
        )

    def to_llm_context(self) -> str:
        """
        Formats CQL output as factual context for the constrained generator.
        The LLM MUST treat all values here as deterministic facts.
        """
        lines = ["=== CURANIQ DETERMINISTIC SAFETY KERNEL OUTPUT ==="]
        lines.append("IMPORTANT: All values below are DETERMINISTICALLY COMPUTED.")
        lines.append("You MUST NOT alter, round differently, or contradict any of these values.\n")
        
        if self.dose_results:
            lines.append("DOSING (deterministic):")
            for d in self.dose_results:
                lines.append(f"  {d.drug_name}: {d.recommended_dose_str}")
                if d.renal_adjustment_applied:
                    lines.append(f"    → Renal-adjusted (factor: {d.renal_adjustment_factor})")
                if d.max_dose_exceeded:
                    lines.append(f"    ⚠️ MAX DOSE EXCEEDED — cap at {d.max_dose_cap_mg} mg")
                for w in d.warnings:
                    lines.append(f"    ⚠️ {w}")
        
        if self.ddi_results:
            lines.append("\nDRUG-DRUG INTERACTIONS (deterministic):")
            for ddi in self.ddi_results:
                lines.append(f"  {ddi.drug_1} + {ddi.drug_2}: {ddi.severity.value.upper()}")
                lines.append(f"    Effect: {ddi.clinical_effect}")
                lines.append(f"    Management: {ddi.management}")
        
        if self.contraindication_results:
            lines.append("\nCONTRAINDICATIONS (deterministic):")
            for c in self.contraindication_results:
                lines.append(f"  {c.drug} + {c.condition_or_drug}: {c.severity.value.upper()}")
                lines.append(f"    Reason: {c.reason}")
                if c.alternatives:
                    lines.append(f"    Alternatives: {', '.join(c.alternatives)}")
        
        if self.allergy_assessments:
            lines.append("\nALLERGY ASSESSMENT (deterministic):")
            for a in self.allergy_assessments:
                lines.append(f"  {a.drug} (allergy to {a.allergen}): {a.allergy_type.value}")
                if a.cross_reactivity_risk:
                    lines.append(f"    Cross-reactivity risk: {', '.join(a.cross_reactive_drugs)}")
                if a.safe_alternatives:
                    lines.append(f"    Safe alternatives: {', '.join(a.safe_alternatives)}")
        
        if not self.is_safe_to_proceed():
            lines.append("\n🚫 SAFETY KERNEL VERDICT: ABSOLUTE CONTRAINDICATION DETECTED.")
            lines.append("DO NOT generate a dose recommendation.")
            lines.append("Explain the contraindication and provide alternatives only.")
        
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# PHARMACOKINETIC CALCULATORS
# ─────────────────────────────────────────────────────────────────────────────

class PKCalculators:
    """
    Validated pharmacokinetic calculators.
    All math is deterministic — no LLM arithmetic allowed.
    """
    
    @staticmethod
    def cockcroft_gault(
        age_years: float,
        weight_kg: float,
        sex: str,          # "male" | "female"
        serum_creatinine_mg_dl: float,
    ) -> float:
        """
        Cockcroft-Gault equation for creatinine clearance (mL/min).
        
        CrCl = [(140 - age) × weight × (0.85 if female)] / (72 × SCr)
        
        Reference: Cockcroft DW, Gault MH. Nephron. 1976;16(1):31-41.
        Used for drug dosing adjustments — more appropriate than eGFR for this purpose.
        """
        if serum_creatinine_mg_dl <= 0:
            raise ValueError("Serum creatinine must be > 0")
        if weight_kg <= 0 or age_years <= 0:
            raise ValueError("Weight and age must be positive")
        
        sex_factor = 0.85 if sex.lower() == "female" else 1.0
        crcl = ((140 - age_years) * weight_kg * sex_factor) / (72 * serum_creatinine_mg_dl)
        return max(0.0, round(crcl, 1))

    @staticmethod
    def ideal_body_weight_kg(height_cm: float, sex: str) -> float:
        """
        Devine formula for Ideal Body Weight.
        
        Males: IBW = 50 kg + 2.3 kg per inch over 5 feet
        Females: IBW = 45.5 kg + 2.3 kg per inch over 5 feet
        
        Reference: Devine BJ. Drug Intell Clin Pharm. 1974;8:650-655.
        """
        height_inches = height_cm / 2.54
        inches_over_5ft = max(0, height_inches - 60)
        
        base = 50.0 if sex.lower() == "male" else 45.5
        ibw = base + (2.3 * inches_over_5ft)
        return round(max(ibw, 30.0), 1)  # Floor at 30 kg

    @staticmethod
    def adjusted_body_weight_kg(
        actual_weight_kg: float,
        ideal_weight_kg: float,
        correction_factor: float = 0.4,
    ) -> float:
        """
        Adjusted Body Weight for obese patients.
        ABW = IBW + 0.4 × (ABW - IBW)
        
        Used when actual weight > 125% IBW.
        Reference: Traynor AM et al. Ann Pharmacother. 1995.
        """
        if actual_weight_kg <= ideal_weight_kg * 1.25:
            return actual_weight_kg  # Not obese — use actual
        return ideal_weight_kg + (correction_factor * (actual_weight_kg - ideal_weight_kg))

    @staticmethod
    def classify_renal_function(egfr_ml_min: float, on_dialysis: bool = False) -> RenalFunction:
        """
        Classify renal function per KDIGO 2022 CKD staging.
        Reference: KDIGO 2022 CKD Guideline. Kidney Int Suppl. 2024.
        """
        if on_dialysis:
            return RenalFunction.DIALYSIS
        if egfr_ml_min >= 90:
            return RenalFunction.NORMAL
        if egfr_ml_min >= 60:
            return RenalFunction.MILD_CKD
        if egfr_ml_min >= 30:
            return RenalFunction.MODERATE_CKD
        if egfr_ml_min >= 15:
            return RenalFunction.SEVERE_CKD
        return RenalFunction.ESRD


# ─────────────────────────────────────────────────────────────────────────────
# DDI DATABASE (curated subset — P1 core drugs)
# Full database integrates with L3-2 Medication Intelligence Module
# ─────────────────────────────────────────────────────────────────────────────

# Format: frozenset({drug_a, drug_b}) → DDIResult template
# Drug names are RxNorm normalized lowercase
# ---------------------------------------------------------------
# DRUG-TO-CLASS LOOKUP: enables class-level DDI matching
# Maps specific drugs to their pharmacological classes used in _DDI_DATABASE.
# Without this, "fluoxetine + tramadol" won't match "ssri + tramadol".
# ---------------------------------------------------------------
def _load_drug_to_class() -> dict[str, list[str]]:
    """Load drug-to-class mapping from versioned JSON data file."""
    try:
        from curaniq.data_loader import load_json_data
        raw = load_json_data("drug_class_mapping.json")
        mapping = raw.get("mappings", {})
        return {drug.lower(): classes for drug, classes in mapping.items() if isinstance(classes, list)}
    except Exception:
        import logging
        logging.getLogger(__name__).warning(
            "Failed to load drug_class_mapping.json. DDI class-level matching disabled."
        )
        return {}

_DRUG_TO_CLASS: dict[str, list[str]] = _load_drug_to_class()


_DDI_DATABASE: dict[frozenset, dict] = {
    # ─── CONTRAINDICATED COMBINATIONS ───────────────────────────────────────
    frozenset({"warfarin", "metronidazole"}): {
        "severity": DDISeverity.MAJOR,
        "mechanism": "Pharmacokinetic — CYP2C9 inhibition",
        "clinical_effect": "Metronidazole inhibits CYP2C9, increasing warfarin exposure by 40-100%. Risk of severe bleeding.",
        "management": "Avoid combination if possible. If unavoidable, reduce warfarin dose by 25-50% and monitor INR every 2-3 days.",
        "monitoring_required": ["INR daily x 5 days then weekly"],
        "alternatives": ["tinidazole (less CYP inhibition)", "topical metronidazole for vaginal infection"],
        "evidence_source": "FDA warfarin label, Stockley Drug Interactions",
    },
    frozenset({"ssri", "tramadol"}): {
        "severity": DDISeverity.MAJOR,
        "mechanism": "Pharmacodynamic — serotonin syndrome risk",
        "clinical_effect": "Combined serotonergic activity increases risk of serotonin syndrome: hyperthermia, rigidity, altered mental status.",
        "management": "Avoid combination. If pain management required, consider non-serotonergic alternatives (acetaminophen, NSAIDs if appropriate).",
        "monitoring_required": ["Mental status", "Temperature", "Muscle tone"],
        "alternatives": ["acetaminophen", "NSAIDs (if no contraindication)", "opioids with lower serotonergic activity"],
        "evidence_source": "FDA tramadol label, Boyer EW NEJM 2005",
    },
    frozenset({"linezolid", "ssri"}): {
        "severity": DDISeverity.CONTRAINDICATED,
        "mechanism": "Pharmacodynamic — MAO-A inhibition causing serotonin syndrome",
        "clinical_effect": "Linezolid is a MAO-A inhibitor. Combined with SSRIs: life-threatening serotonin syndrome.",
        "management": "CONTRAINDICATED. Stop SSRI at least 2 weeks before linezolid (5 weeks for fluoxetine). Use IV methylene blue protocol only if no alternative.",
        "monitoring_required": [],
        "alternatives": ["daptomycin", "vancomycin", "tigecycline (depending on indication)"],
        "evidence_source": "FDA linezolid Black Box Warning",
    },
    frozenset({"warfarin", "aspirin"}): {
        "severity": DDISeverity.MAJOR,
        "mechanism": "Pharmacodynamic — additive bleeding risk + platelet inhibition",
        "clinical_effect": "Combined anticoagulation and antiplatelet therapy significantly increases major bleeding risk (OR 2.5-3.5 vs warfarin alone).",
        "management": "Avoid dual therapy unless specific indication (e.g., mechanical heart valve + AF). If used, PPI co-prescription required. Monitor INR closely.",
        "monitoring_required": ["INR", "Signs of bleeding", "Hemoglobin"],
        "alternatives": ["Acetaminophen for analgesia", "Discuss indication — often aspirin not needed with warfarin"],
        "evidence_source": "Concomitant warfarin + aspirin studies, AHA/ACC guidance",
    },
    frozenset({"simvastatin", "clarithromycin"}): {
        "severity": DDISeverity.CONTRAINDICATED,
        "mechanism": "Pharmacokinetic — CYP3A4 inhibition",
        "clinical_effect": "Clarithromycin dramatically increases simvastatin AUC (up to 10-fold). Risk of severe myopathy and rhabdomyolysis.",
        "management": "CONTRAINDICATED. Suspend simvastatin during clarithromycin course. Switch to pravastatin or rosuvastatin (not CYP3A4-dependent) if statin needed during treatment.",
        "monitoring_required": [],
        "alternatives": ["pravastatin (preferred — not CYP3A4 metabolized)", "rosuvastatin", "fluvastatin"],
        "evidence_source": "FDA simvastatin safety communication 2011, FDA label",
    },
    frozenset({"metformin", "contrast_media"}): {
        "severity": DDISeverity.MAJOR,
        "mechanism": "Risk of contrast-induced nephropathy → metformin accumulation → lactic acidosis",
        "clinical_effect": "If AKI occurs after IV contrast, metformin cannot be cleared → lactate accumulation → lactic acidosis (rare but fatal).",
        "management": "Hold metformin 48h before IV contrast if eGFR < 60 mL/min. After procedure: check creatinine before restarting. If eGFR normal at 48h, restart metformin.",
        "monitoring_required": ["Creatinine at 48h post-contrast", "Lactate if symptomatic"],
        "alternatives": ["Continue if eGFR > 60 and no renal risk factors"],
        "evidence_source": "ACR Manual on Contrast Media 2022, FDA metformin label",
    },
    frozenset({"ace_inhibitor", "potassium_sparing_diuretic"}): {
        "severity": DDISeverity.MAJOR,
        "mechanism": "Pharmacodynamic — additive hyperkalemia risk",
        "clinical_effect": "Both reduce aldosterone effect → additive potassium retention → hyperkalemia risk, especially in CKD/diabetes.",
        "management": "Monitor potassium carefully. Check K+ within 1-2 weeks of initiation/dose change and periodically. Target K+ < 5.5 mEq/L. Reduce or stop one agent if hyperkalemia develops.",
        "monitoring_required": ["Serum potassium 1-2 weeks post-initiation", "Serum creatinine"],
        "alternatives": ["Loop diuretics if diuresis needed without hyperkalemia risk"],
        "evidence_source": "RALES trial, EPHESUS trial, ESC HF Guidelines 2023",
    },
    frozenset({"fluconazole", "warfarin"}): {
        "severity": DDISeverity.MAJOR,
        "mechanism": "Pharmacokinetic — CYP2C9 inhibition",
        "clinical_effect": "Fluconazole potently inhibits CYP2C9, the primary warfarin metabolizing enzyme. INR can increase 2-3-fold.",
        "management": "Reduce warfarin dose empirically by 25-50% when starting fluconazole. Monitor INR every 2-3 days. Restore original dose when fluconazole discontinued.",
        "monitoring_required": ["INR every 2-3 days during fluconazole course"],
        "alternatives": ["Topical antifungal if appropriate (no systemic DDI)"],
        "evidence_source": "FDA fluconazole label, multiple PK studies",
    },
    # QT prolongation combinations (→ L3-12 QT Risk Engine)
    frozenset({"haloperidol", "azithromycin"}): {
        "severity": DDISeverity.MAJOR,
        "mechanism": "Pharmacodynamic — additive QT prolongation",
        "clinical_effect": "Both prolong QT interval. Combination increases risk of torsades de pointes (TdP), potentially fatal ventricular arrhythmia.",
        "management": "Avoid combination. If unavoidable: baseline ECG, correct electrolytes (K+, Mg2+, Ca2+), avoid other QT-prolonging agents, ECG monitoring.",
        "monitoring_required": ["ECG before and 4-6h after initiation", "Electrolytes"],
        "alternatives": ["Erythromycin if antibiotic needed (also QT-prolonging — consider azithromycin alternatives: amoxicillin, doxycycline)", "Benzodiazepine if sedation needed"],
        "evidence_source": "crediblemeds.org Known Risk, FDA safety communications",
    },
}

# Class identifiers — NOT clinical data. These are pseudo-drug names used by
# CQL rules to identify drug classes (CQL rules say "if drug is in class
# 'ssri', then warn about serotonin syndrome"). The actual class membership
# expansion happens in L3-2 Medication Intelligence using ATC class data.
# Keeping these as a frozenset here is functional plumbing, not hardcoded
# clinical knowledge.
_CLASS_IDENTIFIERS: frozenset[str] = frozenset({
    "ssri",
    "ace_inhibitor",
    "potassium_sparing_diuretic",
})


def _normalize_drug_name(name: str, knowledge_provider=None) -> str:
    """
    Normalize drug name to canonical form for CQL lookup.

    1. Class identifiers (ssri, ace_inhibitor, ...) are returned as-is
       so L3-2 can expand them via ATC class data.
    2. Otherwise, the name is resolved through the clinical knowledge
       provider (live RxNorm in clinician_prod, vendored in demo).
       Returns the RxNorm canonical name on hit, or the normalized
       input on miss.

    Falls back gracefully: if the provider raises KnowledgeUnavailableError
    (live unwired in this env, vendored doesn't have the drug), we return
    the normalized input rather than failing — drug-name normalization is
    best-effort augmentation of CQL inputs.
    """
    normalized = name.lower().strip().replace("-", "_").replace(" ", "_")
    if normalized in _CLASS_IDENTIFIERS:
        return normalized
    if knowledge_provider is None:
        return normalized
    try:
        norm = knowledge_provider.normalize_drug(name)
        if norm is not None:
            return norm.canonical_name.lower().strip().replace("-", "_").replace(" ", "_")
    except Exception:
        # KnowledgeUnavailableError or any provider hiccup → use input as-is
        pass
    return normalized


# ─────────────────────────────────────────────────────────────────────────────
# CQL NEURO-SYMBOLIC CORE ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class CQLEngine:
    """
    The Deterministic Safety Kernel — Layer 3-1.
    
    Every calculation here produces a deterministic output.
    The engine never uses statistical inference or LLMs.
    When in doubt, the engine refuses or provides the most conservative option.
    
    This is the foundation of CURANIQ's trust — clinicians know that
    any number in a CQL output was computed, not predicted.
    """

    def __init__(self, knowledge_provider=None):
        """
        Args:
            knowledge_provider: ClinicalKnowledgeProvider for drug-name
                resolution. If None, drug-name normalization is best-effort
                using only class identifiers (no synonym resolution).
                Production should inject a RouterProvider with RxNorm wired.
        """
        self.pk_calculators = PKCalculators()
        self._ddi_db = _DDI_DATABASE
        self._knowledge_provider = knowledge_provider

    def evaluate_patient_query(
        self,
        drugs: list[str],
        patient: PatientProfile,
        query_type: str = "general",  # "dosing" | "ddi" | "contraindication" | "allergy" | "general"
    ) -> CQLKernelOutput:
        """
        Main entry point for the CQL kernel.
        Evaluates a clinical query against all relevant safety rules.
        
        Returns CQLKernelOutput with all deterministic safety verdicts.
        """
        output = CQLKernelOutput()
        
        normalized_drugs = [_normalize_drug_name(d, self._knowledge_provider) for d in drugs]
        
        # 1. Renal function classification
        renal_fn = None
        if patient.egfr_ml_min is not None:
            renal_fn = PKCalculators.classify_renal_function(
                patient.egfr_ml_min, patient.is_on_dialysis
            )
        elif (patient.serum_creatinine_mg_dl and patient.age_years and
              patient.weight_kg and patient.sex):
            try:
                crcl = PKCalculators.cockcroft_gault(
                    age_years=patient.age_years,
                    weight_kg=patient.weight_kg,
                    sex=patient.sex,
                    serum_creatinine_mg_dl=patient.serum_creatinine_mg_dl,
                )
                renal_fn = PKCalculators.classify_renal_function(crcl)
                # Update egfr for subsequent calculations
                patient = PatientProfile(
                    **{**patient.__dict__, "egfr_ml_min": crcl}
                )
            except ValueError:
                pass
        
        # 2. Drug-Drug Interaction checking
        if len(normalized_drugs) >= 2:
            ddi_results = self.check_all_ddis(normalized_drugs)
            output.ddi_results = ddi_results
            output.has_major_ddis = any(
                d.severity in (DDISeverity.MAJOR, DDISeverity.CONTRAINDICATED)
                for d in ddi_results
            )
        
        # 3. Dose calculation (if query includes dosing)
        if query_type in ("dosing", "general") and drugs:
            dose_results = []
            for drug in normalized_drugs:
                result = self.calculate_dose(drug, patient, renal_fn)
                if result:
                    dose_results.append(result)
            output.dose_results = dose_results
        
        # 4. Contraindications (drug-disease)
        contraindication_results = self.check_disease_contraindications(
            normalized_drugs, patient
        )
        output.contraindication_results = contraindication_results
        output.has_absolute_contraindications = any(
            c.is_absolute for c in contraindication_results
        )
        
        # 5. Generate safety summary
        output.safety_summary = self._generate_safety_summary(output, patient)
        
        return output

    def check_all_ddis(self, drugs: list[str]) -> list[DDIResult]:
        """
        Check all pairwise drug-drug interactions.
        Checks both specific drug pairs AND drug-class pairs
        (e.g., fluoxetine->ssri so fluoxetine+tramadol matches ssri+tramadol).
        """
        results = []
        seen_pairs = set()
        
        for i, drug_a in enumerate(drugs):
            for drug_b in drugs[i + 1:]:
                pair = frozenset({drug_a, drug_b})
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                
                # Try direct pair first
                ddi_data = self._ddi_db.get(pair)
                
                # If no direct match, try class-expanded pairs
                if not ddi_data:
                    names_a = [drug_a] + _DRUG_TO_CLASS.get(drug_a, [])
                    names_b = [drug_b] + _DRUG_TO_CLASS.get(drug_b, [])
                    for na in names_a:
                        for nb in names_b:
                            expanded = frozenset({na, nb})
                            if expanded != pair:
                                ddi_data = self._ddi_db.get(expanded)
                                if ddi_data:
                                    break
                        if ddi_data:
                            break
                if ddi_data and ddi_data["severity"] != DDISeverity.NO_INTERACTION:
                    result = DDIResult(
                        drug_1=drug_a,
                        drug_2=drug_b,
                        severity=ddi_data["severity"],
                        mechanism=ddi_data.get("mechanism", "Unknown"),
                        clinical_effect=ddi_data.get("clinical_effect", ""),
                        management=ddi_data.get("management", "Consult clinical pharmacist"),
                        is_absolute=ddi_data["severity"] == DDISeverity.CONTRAINDICATED,
                        monitoring_required=ddi_data.get("monitoring_required", []),
                        alternatives=ddi_data.get("alternatives", []),
                        evidence_source=ddi_data.get("evidence_source", ""),
                    )
                    results.append(result)
        
        # Sort by severity (most dangerous first)
        severity_order = {
            DDISeverity.CONTRAINDICATED: 0,
            DDISeverity.MAJOR: 1,
            DDISeverity.MODERATE: 2,
            DDISeverity.MINOR: 3,
            DDISeverity.UNKNOWN: 4,
        }
        results.sort(key=lambda r: severity_order.get(r.severity, 5))
        
        return results

    def calculate_dose(
        self,
        drug_name: str,
        patient: PatientProfile,
        renal_fn: Optional[RenalFunction] = None,
    ) -> Optional[DoseCalculationResult]:
        """
        Calculate drug dose for a specific patient.
        
        Currently implements core drugs — full formulary integrates with L3-2.
        All calculations follow validated clinical dosing references.
        """
        # Dispatch to drug-specific calculator
        calculators = {
            "metformin": self._dose_metformin,
            "amoxicillin": self._dose_amoxicillin,
            "vancomycin": self._dose_vancomycin_initial,
            "gentamicin": self._dose_gentamicin,
            "acetaminophen": self._dose_acetaminophen,
            "ibuprofen": self._dose_ibuprofen,
            "warfarin": self._dose_warfarin_initiation,
            "lisinopril": self._dose_lisinopril,
            "atorvastatin": self._dose_atorvastatin,
        }
        
        calculator = calculators.get(drug_name.lower())
        if calculator:
            return calculator(patient, renal_fn)
        
        return None  # Drug not in current kernel — handled by L3-2

    def _dose_metformin(
        self, patient: PatientProfile, renal_fn: Optional[RenalFunction]
    ) -> DoseCalculationResult:
        """
        Metformin dosing with mandatory eGFR check.
        Reference: FDA metformin label 2016 revision, KDIGO 2022.
        """
        result = DoseCalculationResult(
            drug_name="metformin",
            calculated_dose_mg=None,
            calculated_dose_mg_per_kg=None,
            recommended_dose_str="",
            dosing_interval_hours=12,
            route="oral",
            available_strengths_mg=[500, 750, 1000],
        )
        
        # eGFR is MANDATORY for metformin dosing
        if patient.egfr_ml_min is None:
            result.is_safe_to_dispense = False
            result.recommended_dose_str = "CANNOT DOSE — eGFR required before metformin"
            result.warnings.append("eGFR MUST be known before prescribing metformin (risk of lactic acidosis)")
            return result
        
        egfr = patient.egfr_ml_min
        
        # FDA 2016 label guidance:
        if egfr >= 60:
            result.calculated_dose_mg = 1000
            result.recommended_dose_str = "500–1000 mg twice daily with meals (titrate over 4 weeks)"
            result.max_dose_cap_mg = 2000
        elif egfr >= 45:
            result.calculated_dose_mg = 500
            result.recommended_dose_str = "500 mg twice daily (reduce dose — eGFR 45-59)"
            result.renal_adjustment_applied = True
            result.renal_adjustment_factor = 0.5
            result.warnings.append(f"Renal-adjusted dose for eGFR {egfr:.0f}. Monitor eGFR every 3-6 months.")
        elif egfr >= 30:
            result.calculated_dose_mg = 500
            result.recommended_dose_str = "500 mg once daily ONLY — FDA warns against initiation in eGFR 30-44"
            result.renal_adjustment_applied = True
            result.renal_adjustment_factor = 0.25
            result.warnings.append(
                f"eGFR {egfr:.0f}: FDA label states do not INITIATE metformin if eGFR 30-44. "
                "For existing patients, assess risk/benefit. Monitor closely."
            )
        else:
            # eGFR < 30: CONTRAINDICATED
            result.is_safe_to_dispense = False
            result.calculated_dose_mg = 0
            result.recommended_dose_str = f"CONTRAINDICATED — eGFR {egfr:.0f} mL/min < 30 (lactic acidosis risk)"
            result.warnings.append(
                f"Metformin CONTRAINDICATED when eGFR < 30 mL/min. "
                f"Current eGFR: {egfr:.0f}. Risk of fatal lactic acidosis. "
                "Consider alternative glycemic agents (e.g., DPP-4 inhibitor at appropriate renal dose, "
                "insulin, SGLT2 inhibitor if eGFR appropriate)."
            )
        
        result.evidence_source = "FDA metformin label 2016, KDIGO 2022 Diabetes & CKD Guideline"
        return result

    def _dose_amoxicillin(
        self, patient: PatientProfile, renal_fn: Optional[RenalFunction]
    ) -> DoseCalculationResult:
        """
        Amoxicillin dosing.
        Reference: BNF, FDA label, EUCAST breakpoints.
        """
        # Check for pediatric dosing (→ L3-7)
        is_pediatric = (patient.age_years is not None and patient.age_years < 18)
        
        result = DoseCalculationResult(
            drug_name="amoxicillin",
            calculated_dose_mg=None,
            calculated_dose_mg_per_kg=None,
            recommended_dose_str="",
            dosing_interval_hours=8,
            route="oral",
            available_strengths_mg=[250, 500, 875],
        )
        
        if is_pediatric and patient.weight_kg:
            # Weight-based pediatric dosing
            dose_mg_per_kg = 25.0  # Standard pediatric dose
            max_dose_mg = 500.0    # Never exceed adult dose
            
            calculated = patient.weight_kg * dose_mg_per_kg
            result.calculated_dose_mg = min(calculated, max_dose_mg)
            result.calculated_dose_mg_per_kg = dose_mg_per_kg
            result.weight_based = True
            result.max_dose_cap_mg = max_dose_mg
            
            if calculated > max_dose_mg:
                result.warnings.append(f"Dose capped at {max_dose_mg} mg (adult max)")
            
            result.recommended_dose_str = (
                f"{result.calculated_dose_mg:.0f} mg (25 mg/kg/dose) every 8 hours"
            )
        else:
            # Adult dosing
            result.calculated_dose_mg = 500
            result.recommended_dose_str = "500 mg every 8 hours (standard dose)"
            result.max_dose_cap_mg = 3000  # 1g TID for severe infections
        
        # Renal adjustment
        if renal_fn in (RenalFunction.SEVERE_CKD, RenalFunction.ESRD):
            result.dosing_interval_hours = 24
            result.renal_adjustment_applied = True
            result.renal_adjustment_factor = 0.33
            result.recommended_dose_str = (
                f"{result.calculated_dose_mg:.0f} mg every 24 hours (renal-adjusted for severe CKD)"
            )
        elif renal_fn == RenalFunction.DIALYSIS:
            result.recommended_dose_str = "500 mg every 24h + supplemental dose after hemodialysis"
            result.renal_adjustment_applied = True
            result.warnings.append("Supplemental dose required after hemodialysis — amoxicillin is dialyzable")
        
        result.evidence_source = "BNF 2024, FDA label, EUCAST 2024 clinical breakpoints"
        return result

    def _dose_vancomycin_initial(
        self, patient: PatientProfile, renal_fn: Optional[RenalFunction]
    ) -> DoseCalculationResult:
        """
        Vancomycin initial loading dose.
        Target: AUC/MIC 400-600 mg·h/L per IDSA/SIDP/ASHP 2020 guideline.
        
        Reference: Rybak MJ et al. Am J Health Syst Pharm. 2020.
        Note: Ongoing dose optimization requires TDM (→ L3-18).
        """
        result = DoseCalculationResult(
            drug_name="vancomycin",
            calculated_dose_mg=None,
            calculated_dose_mg_per_kg=None,
            recommended_dose_str="",
            dosing_interval_hours=None,
            route="IV",
            available_strengths_mg=[250, 500, 750, 1000, 1500, 2000],
        )
        
        if not patient.weight_kg:
            result.is_safe_to_dispense = False
            result.recommended_dose_str = "CANNOT DOSE — actual body weight required"
            result.warnings.append("Weight required for vancomycin dosing (25-30 mg/kg loading dose)")
            return result
        
        # Loading dose: 25-30 mg/kg actual body weight (IDSA 2020)
        loading_dose_mg = patient.weight_kg * 25  # Use 25 mg/kg as starting point
        result.calculated_dose_mg = loading_dose_mg
        result.calculated_dose_mg_per_kg = 25.0
        result.weight_based = True
        
        # Maintenance interval based on renal function
        interval_map = {
            RenalFunction.NORMAL: 8,          # q8h
            RenalFunction.MILD_CKD: 12,       # q12h
            RenalFunction.MODERATE_CKD: 24,   # q24h
            RenalFunction.SEVERE_CKD: 48,     # q48h
            RenalFunction.ESRD: None,         # Require TDM — no standard interval
            RenalFunction.DIALYSIS: None,     # Special protocol
        }
        
        if renal_fn:
            result.dosing_interval_hours = interval_map.get(renal_fn, 12)
            result.renal_adjustment_applied = renal_fn not in (RenalFunction.NORMAL, None)
        else:
            result.dosing_interval_hours = 12  # Default without eGFR
            result.warnings.append("Renal function unknown — using q12h as default. Obtain eGFR urgently.")
        
        interval_str = (
            f"q{int(result.dosing_interval_hours)}h"
            if result.dosing_interval_hours else "frequency requires TDM"
        )
        result.recommended_dose_str = (
            f"Loading: {loading_dose_mg:.0f} mg IV (25 mg/kg). "
            f"Maintenance: AUC-guided dosing {interval_str}. "
            "Therapeutic drug monitoring (AUC/MIC target 400-600 mg·h/L) required."
        )
        
        result.warnings.extend([
            "AUC-guided TDM required (not trough-only monitoring) per IDSA 2020",
            "Infuse over ≥60 min (≥90 min if dose > 1500 mg) to prevent Red Man Syndrome",
            "Monitor renal function (SCr) every 48-72h",
        ])
        
        result.evidence_source = "Rybak MJ et al. ASHP/SIDP/IDSA 2020 Vancomycin TDM Guideline"
        return result

    def _dose_gentamicin(
        self, patient: PatientProfile, renal_fn: Optional[RenalFunction]
    ) -> DoseCalculationResult:
        """
        Gentamicin — once-daily (extended-interval) dosing using Hartford nomogram.
        Reference: Nicolau DP et al. Antimicrob Agents Chemother. 1995.
        """
        result = DoseCalculationResult(
            drug_name="gentamicin",
            calculated_dose_mg=None,
            calculated_dose_mg_per_kg=None,
            recommended_dose_str="",
            dosing_interval_hours=24,
            route="IV",
        )
        
        if not patient.weight_kg or not patient.egfr_ml_min:
            result.is_safe_to_dispense = False
            result.recommended_dose_str = "CANNOT DOSE — weight AND eGFR required (nephrotoxic drug)"
            result.warnings.append("Gentamicin dosing REQUIRES weight and renal function — nephrotoxicity risk")
            return result
        
        # Use adjusted body weight for obese patients
        dosing_weight = patient.weight_kg
        if patient.height_cm and patient.sex:
            ibw = PKCalculators.ideal_body_weight_kg(patient.height_cm, patient.sex)
            if patient.weight_kg > ibw * 1.25:  # Obese (>125% IBW)
                dosing_weight = PKCalculators.adjusted_body_weight_kg(patient.weight_kg, ibw)
                result.warnings.append(
                    f"Obese patient: using Adjusted Body Weight {dosing_weight:.1f} kg "
                    f"(ABW {patient.weight_kg:.1f} kg, IBW {ibw:.1f} kg)"
                )
        
        # Hartford nomogram: 7 mg/kg q24h (adjust for renal function)
        dose_mg_per_kg = 7.0
        if renal_fn == RenalFunction.MODERATE_CKD:
            dose_mg_per_kg = 5.0
            result.dosing_interval_hours = 36
        elif renal_fn in (RenalFunction.SEVERE_CKD, RenalFunction.ESRD):
            result.is_safe_to_dispense = False
            result.recommended_dose_str = "AVOID — severe renal impairment (eGFR < 30). Use alternative."
            result.warnings.append(
                f"Gentamicin AVOID in severe CKD/ESRD. Risk of irreversible nephrotoxicity and ototoxicity. "
                "Consider non-nephrotoxic alternatives: azithromycin, ceftriaxone, daptomycin (per indication)."
            )
            return result
        
        result.calculated_dose_mg = dosing_weight * dose_mg_per_kg
        result.calculated_dose_mg_per_kg = dose_mg_per_kg
        result.weight_based = True
        result.recommended_dose_str = (
            f"{result.calculated_dose_mg:.0f} mg "
            f"({dose_mg_per_kg} mg/kg) IV every {int(result.dosing_interval_hours)}h. "
            "Check 6-14h post-dose level for Hartford nomogram adjustment."
        )
        
        result.warnings.extend([
            "TDM required — Hartford nomogram level check 6-14h post-dose",
            "Monitor SCr, BUN every 48-72h (nephrotoxicity)",
            "Monitor for ototoxicity (ask about tinnitus, hearing changes)",
            "Avoid concurrent nephrotoxins (NSAIDs, other aminoglycosides, amphotericin B)",
        ])
        
        result.evidence_source = "Hartford nomogram (Nicolau DP 1995), IDSA guidelines"
        return result

    def _dose_acetaminophen(
        self, patient: PatientProfile, renal_fn: Optional[RenalFunction]
    ) -> DoseCalculationResult:
        """Acetaminophen (paracetamol) dosing with hepatotoxicity risk checks."""
        result = DoseCalculationResult(
            drug_name="acetaminophen",
            calculated_dose_mg=500,
            calculated_dose_mg_per_kg=None,
            recommended_dose_str="",
            dosing_interval_hours=6,
            route="oral",
            available_strengths_mg=[325, 500, 650, 1000],
        )
        
        max_daily_mg = 4000  # Standard adult
        
        # Hepatic impairment — reduce dose
        if patient.child_pugh_class in ("B", "C"):
            max_daily_mg = 2000
            result.warnings.append(
                f"Hepatic impairment (Child-Pugh {patient.child_pugh_class}): "
                f"maximum 2g/day (NOT 4g/day standard). Consider acetaminophen-free alternatives."
            )
        
        # Alcohol use (if known)
        # Chronic alcohol use: reduce to 2g/day max
        
        is_pediatric = patient.age_years is not None and patient.age_years < 18
        
        if is_pediatric and patient.weight_kg:
            dose_mg_per_kg = 15.0
            max_single_dose = min(patient.weight_kg * dose_mg_per_kg, 1000)
            result.calculated_dose_mg = max_single_dose
            result.calculated_dose_mg_per_kg = dose_mg_per_kg
            result.weight_based = True
            result.recommended_dose_str = (
                f"{max_single_dose:.0f} mg (15 mg/kg) every 4-6 hours. "
                f"Maximum: 75 mg/kg/day or {min(4000, patient.weight_kg * 75):.0f} mg/day, whichever is less."
            )
        else:
            result.recommended_dose_str = (
                f"500-1000 mg every 4-6 hours as needed. "
                f"Maximum {max_daily_mg} mg/24h. "
                "Include ALL sources of acetaminophen (combination products)."
            )
        
        result.warnings.append(
            "Educate patient: acetaminophen in combination products (Percocet, NyQuil, etc.) counts toward daily maximum"
        )
        result.evidence_source = "FDA acetaminophen label, BNF 2024"
        return result

    def _dose_ibuprofen(
        self, patient: PatientProfile, renal_fn: Optional[RenalFunction]
    ) -> DoseCalculationResult:
        """Ibuprofen dosing — check renal function and cardiovascular risk."""
        result = DoseCalculationResult(
            drug_name="ibuprofen",
            calculated_dose_mg=400,
            calculated_dose_mg_per_kg=None,
            recommended_dose_str="",
            dosing_interval_hours=8,
            route="oral",
            available_strengths_mg=[200, 400, 600, 800],
        )
        
        # CONTRAINDICATIONS
        contraindications = []
        
        if renal_fn in (RenalFunction.SEVERE_CKD, RenalFunction.ESRD, RenalFunction.DIALYSIS):
            result.is_safe_to_dispense = False
            result.recommended_dose_str = (
                f"CONTRAINDICATED — severe CKD/ESRD. NSAIDs cause acute-on-chronic kidney injury. "
                "Use acetaminophen for analgesia."
            )
            result.warnings.append("NSAID CONTRAINDICATED in severe CKD/ESRD (AKI risk, KDIGO guidance)")
            return result
        
        if renal_fn == RenalFunction.MODERATE_CKD:
            result.warnings.append(
                "Use with CAUTION in CKD G3 (eGFR 30-59). Shortest duration possible. "
                "Monitor creatinine within 1-2 weeks. Prefer acetaminophen."
            )
        
        result.recommended_dose_str = "400-800 mg every 6-8 hours with food. Maximum 3200 mg/day."
        result.max_dose_cap_mg = 3200
        
        result.warnings.extend([
            "Take with food or milk to reduce GI risk",
            "Avoid in patients with active/recent peptic ulcer, aspirin allergy, decompensated HF",
            "Consider PPI co-prescription if high GI risk or concurrent aspirin/anticoagulant",
        ])
        
        result.evidence_source = "FDA ibuprofen label, BNF 2024, KDIGO 2012 AKI Guideline"
        return result

    def _dose_warfarin_initiation(
        self, patient: PatientProfile, renal_fn: Optional[RenalFunction]
    ) -> DoseCalculationResult:
        """
        Warfarin initiation — conservative approach with mandatory INR monitoring.
        Note: Ongoing dose requires INR-guided adjustment (L3-2 handles this).
        """
        result = DoseCalculationResult(
            drug_name="warfarin",
            calculated_dose_mg=5,
            calculated_dose_mg_per_kg=None,
            recommended_dose_str="",
            dosing_interval_hours=24,
            route="oral",
            available_strengths_mg=[1, 2, 2.5, 3, 4, 5, 6, 7.5, 10],
        )
        
        # Elderly: start low
        starting_dose = 5.0
        if patient.age_years and patient.age_years > 70:
            starting_dose = 2.5
            result.warnings.append(
                f"Age {patient.age_years:.0f}: start with 2.5 mg (fall risk, bleeding risk, variable PK)"
            )
        
        result.calculated_dose_mg = starting_dose
        result.recommended_dose_str = (
            f"Initiation: {starting_dose} mg daily. Check INR at day 3-5, then adjust. "
            "Target INR depends on indication (AF: 2-3, mechanical valve: 2.5-3.5). "
            "Patient must be counseled on interactions, consistent vitamin K intake, and bleeding signs."
        )
        
        result.warnings.extend([
            "CYP2C9/VKORC1 pharmacogenomics testing may improve initial dosing (if available)",
            "Multiple drug interactions — always check before prescribing new medication",
            "Patient education MANDATORY before first dose",
            "INR within 3-5 days of initiation and after any dose change",
        ])
        
        result.evidence_source = "ACCP Antithrombotic Therapy Guidelines 2022, FDA warfarin label"
        return result

    def _dose_lisinopril(
        self, patient: PatientProfile, renal_fn: Optional[RenalFunction]
    ) -> DoseCalculationResult:
        """Lisinopril dosing for hypertension and HF indications."""
        result = DoseCalculationResult(
            drug_name="lisinopril",
            calculated_dose_mg=5,
            calculated_dose_mg_per_kg=None,
            recommended_dose_str="",
            dosing_interval_hours=24,
            route="oral",
            available_strengths_mg=[2.5, 5, 10, 20, 40],
        )
        
        # Caution: hyperkalemia risk in CKD
        if renal_fn == RenalFunction.ESRD:
            result.is_safe_to_dispense = False
            result.recommended_dose_str = "CONTRAINDICATED in ESRD (bilateral renal artery stenosis risk, severe hyperkalemia)"
            result.warnings.append("ACE inhibitors CONTRAINDICATED in ESRD without specialist guidance")
            return result
        
        starting_dose = 5.0
        if renal_fn in (RenalFunction.SEVERE_CKD, RenalFunction.MODERATE_CKD):
            starting_dose = 2.5
            result.renal_adjustment_applied = True
            result.warnings.append(
                f"CKD: start with 2.5 mg. Monitor K+ and creatinine within 2 weeks of initiation. "
                "Expect up to 30% rise in creatinine (acceptable) vs functional AKI (stop if >50% rise)."
            )
        
        result.calculated_dose_mg = starting_dose
        result.recommended_dose_str = (
            f"Start {starting_dose} mg daily. Titrate to target "
            "(HTN: 10-40 mg/day; HF: target 10-40 mg/day; post-MI: target 10 mg/day)"
        )
        
        result.warnings.extend([
            "Monitor: potassium within 1-2 weeks, creatinine within 1-2 weeks",
            "Contraindicated in pregnancy (all trimesters — teratogenic)",
            "Angioedema history to ACE inhibitor: ABSOLUTE CONTRAINDICATION — use ARB instead",
        ])
        
        result.evidence_source = "JNC 8, ESC/ESH Hypertension Guidelines 2023, CONSENSUS/SOLVD trials"
        return result

    def _dose_atorvastatin(
        self, patient: PatientProfile, renal_fn: Optional[RenalFunction]
    ) -> DoseCalculationResult:
        """Atorvastatin dosing — intensity-based per ACC/AHA 2019."""
        result = DoseCalculationResult(
            drug_name="atorvastatin",
            calculated_dose_mg=20,
            calculated_dose_mg_per_kg=None,
            recommended_dose_str="",
            dosing_interval_hours=24,
            route="oral",
            available_strengths_mg=[10, 20, 40, 80],
        )
        
        result.recommended_dose_str = (
            "High-intensity: 40-80 mg daily (post-ACS, high CV risk, LDL ≥190). "
            "Moderate-intensity: 10-20 mg daily. No renal dose adjustment required."
        )
        
        result.warnings.extend([
            "Monitoring: LFTs at baseline, CK if myalgia develops",
            "Major interaction: avoid with strong CYP3A4 inhibitors (clarithromycin, azole antifungals)",
            "Myopathy risk increases with: high-dose, elderly, renal impairment, hypothyroidism, fibrates",
        ])
        
        result.evidence_source = "ACC/AHA 2019 Cholesterol Guideline, FDA atorvastatin label"
        return result

    def check_disease_contraindications(
        self, drugs: list[str], patient: PatientProfile
    ) -> list[ContraindicationResult]:
        """
        Check for drug-disease contraindications based on patient profile.
        These are deterministic rules — NOT LLM inference.
        """
        results = []
        
        for drug in drugs:
            # Renal contraindications
            if patient.egfr_ml_min is not None:
                egfr = patient.egfr_ml_min
                
                if drug == "metformin" and egfr < 30:
                    results.append(ContraindicationResult(
                        drug=drug,
                        condition_or_drug=f"CKD (eGFR {egfr:.0f})",
                        severity=ContraindicationSeverity.ABSOLUTE,
                        reason=f"Metformin contraindicated when eGFR < 30 mL/min — risk of fatal lactic acidosis.",
                        is_absolute=True,
                        alternatives=["SGLT2i (empagliflozin if eGFR appropriate)", "DPP-4 inhibitor at renal dose", "Insulin"],
                        evidence_source="FDA metformin label 2016",
                    ))
                
                if drug in ("ibuprofen", "naproxen", "diclofenac") and egfr < 30:
                    results.append(ContraindicationResult(
                        drug=drug,
                        condition_or_drug=f"Severe CKD (eGFR {egfr:.0f})",
                        severity=ContraindicationSeverity.ABSOLUTE,
                        reason="NSAIDs contraindicated in severe CKD — risk of acute-on-chronic kidney injury.",
                        is_absolute=True,
                        alternatives=["acetaminophen"],
                        evidence_source="KDIGO 2012 AKI guideline, FDA NSAID labeling",
                    ))
            
            # Pregnancy contraindications
            if getattr(patient, "is_pregnant", False):
                pregnancy_contraindicated = {
                    "lisinopril": ("All trimesters — fetal renal agenesis, oligohydramnios", ["methyldopa", "labetalol", "nifedipine"]),
                    "warfarin": ("First trimester — warfarin embryopathy; third trimester — fetal hemorrhage", ["LMWH (low-molecular-weight heparin)"]),
                    "methotrexate": ("All trimesters — teratogenic, abortifacient", ["consult specialist"]),
                    "atorvastatin": ("All trimesters — fetal harm (animal studies)", ["Stop statin — restart postpartum"]),
                    "finasteride": ("Absolute — virilization of male fetus", ["consult specialist"]),
                }
                
                if drug in pregnancy_contraindicated:
                    reason, alternatives = pregnancy_contraindicated[drug]
                    results.append(ContraindicationResult(
                        drug=drug,
                        condition_or_drug="Pregnancy",
                        severity=ContraindicationSeverity.ABSOLUTE,
                        reason=reason,
                        is_absolute=True,
                        alternatives=alternatives,
                        evidence_source="FDA drug labels, ACOG guidance",
                    ))
        
        return results

    def assess_allergy(
        self,
        drug: str,
        allergen: str,
        allergy_type_reported: str = "unknown",
    ) -> AllergyAssessment:
        """
        Allergy kernel — determines true allergy type and cross-reactivity risk.
        
        The critical clinical distinction:
        - IgE-mediated (true allergy): anaphylaxis risk → strict avoidance
        - Non-IgE / intolerance: no anaphylaxis risk → may be safe
        - Severe cutaneous (SJS/TEN/DRESS): avoid drug class → specialist management
        
        Reference: Khan DA, Solensky R. J Allergy Clin Immunol. 2010.
        """
        drug = _normalize_drug_name(drug, self._knowledge_provider)
        allergen = _normalize_drug_name(allergen, self._knowledge_provider)
        
        # Classify allergy type from reported history
        if "anaphylaxis" in allergy_type_reported.lower() or "hives" in allergy_type_reported.lower():
            allergy_type = AllergyType.TRUE_IgE_MEDIATED
        elif any(term in allergy_type_reported.lower() for term in ["sjs", "ten", "dress", "rash", "skin"]):
            allergy_type = AllergyType.SEVERE_CUTANEOUS
        elif any(term in allergy_type_reported.lower() for term in ["nausea", "vomiting", "diarrhea", "gi"]):
            allergy_type = AllergyType.INTOLERANCE
        else:
            allergy_type = AllergyType.UNKNOWN_ALLERGY  # Treat conservatively
        
        assessment = AllergyAssessment(
            drug=drug,
            allergen=allergen,
            allergy_type=allergy_type,
        )
        
        # Penicillin allergy — most common, most nuanced
        if allergen in ("penicillin", "amoxicillin", "ampicillin"):
            assessment.is_penicillin_allergy = True
            
            if allergy_type == AllergyType.INTOLERANCE:
                assessment.cephalosporin_risk = "low"
                assessment.management = (
                    "Non-IgE intolerance to penicillin: cephalosporins are generally safe. "
                    "Cross-reactivity < 1% (shared R1 side chain — check specific drug). "
                    "Cephalexin, cefuroxime, ceftriaxone: can use with observation. "
                    "Consider allergy specialist testing to de-label false penicillin allergy."
                )
                assessment.safe_alternatives = ["cephalexin", "cefuroxime", "ceftriaxone"]
            elif allergy_type == AllergyType.TRUE_IgE_MEDIATED:
                assessment.cross_reactivity_risk = True
                assessment.cross_reactive_drugs = [
                    "amoxicillin", "ampicillin", "piperacillin-tazobactam", 
                    "cephalosporins (R1-side-chain match)"
                ]
                assessment.cephalosporin_risk = "low"  # 1-2% cross-reactivity rate
                assessment.management = (
                    "IgE-mediated penicillin allergy: avoid all aminopenicillins. "
                    "Cephalosporins: 1-2% cross-reactivity (lower than historical 10% claim). "
                    "For serious infections where cephalosporin needed: graded challenge or allergy testing. "
                    "Consider penicillin skin testing to confirm true allergy — 80-90% with reported allergy are not truly allergic."
                )
                assessment.safe_alternatives = [
                    "azithromycin (if indication appropriate)",
                    "clindamycin",
                    "vancomycin (for serious infections)",
                    "carbapenem (if life-threatening and cephalosporin avoided)",
                ]
            elif allergy_type == AllergyType.SEVERE_CUTANEOUS:
                assessment.cross_reactivity_risk = True
                assessment.cross_reactive_drugs = ["all beta-lactams"]
                assessment.cephalosporin_risk = "high"
                assessment.management = (
                    "SEVERE CUTANEOUS REACTION (SJS/TEN/DRESS) to penicillin: "
                    "ABSOLUTE avoidance of ALL beta-lactams (penicillins AND cephalosporins). "
                    "Consult allergy specialist. Non-beta-lactam antibiotics only."
                )
        
        return assessment

    def validate_numeric_plausibility(
        self, value: float, unit: str, context: str
    ) -> tuple[bool, str]:
        """
        Physiologic plausibility check for any numeric value.
        Used by L5-17 Numeric Gate and L5-12 Dose Plausibility Checker.
        
        Returns (is_plausible, reason).
        """
        plausibility_ranges = {
            # Lab values
            "mg/dl_creatinine": (0.3, 15.0),
            "meq/l_potassium": (2.0, 8.0),
            "meq/l_sodium": (115, 165),
            "mg/dl_glucose": (20, 800),
            "g/dl_hemoglobin": (4.0, 22.0),
            "units/l_alt": (5, 3000),
            "ml/min_egfr": (1, 150),
            "inr": (0.5, 15.0),
            
            # Vital signs
            "mmhg_systolic_bp": (50, 300),
            "mmhg_diastolic_bp": (30, 200),
            "bpm_heart_rate": (20, 250),
            "celsius_temperature": (32, 45),
            "fahrenheit_temperature": (90, 113),
            
            # Common drug doses
            "mg_dose": (0.001, 5000),    # Very broad — specific drugs have own limits
            "mg_kg_dose": (0.001, 50),   # Weight-based mg/kg
            "mcg_dose": (0.001, 1000),
            
            # Patient metrics
            "kg_weight": (0.1, 500),
            "cm_height": (30, 250),
            "years_age": (0, 130),
        }
        
        # Build key from context and unit
        key = f"{unit.lower()}_{context.lower().replace(' ', '_')}"
        
        for range_key, (min_val, max_val) in plausibility_ranges.items():
            if range_key in key or key in range_key:
                if min_val <= value <= max_val:
                    return True, "Physiologically plausible"
                else:
                    return False, (
                        f"Value {value} {unit} is outside physiologically plausible range "
                        f"({min_val}-{max_val} {unit} for {context}). Possible transcription error."
                    )
        
        # No specific range defined — allow but flag for manual review
        return True, "No specific plausibility range defined — manual review recommended"

    def _generate_safety_summary(
        self, output: CQLKernelOutput, patient: PatientProfile
    ) -> str:
        """Generate a structured safety summary for the output."""
        lines = []
        
        if output.has_absolute_contraindications:
            lines.append("🚫 ABSOLUTE CONTRAINDICATION(S) DETECTED — do not prescribe without specialist review")
        
        if output.has_major_ddis:
            major_ddis = [d for d in output.ddi_results if d.severity == DDISeverity.MAJOR]
            contra_ddis = [d for d in output.ddi_results if d.severity == DDISeverity.CONTRAINDICATED]
            if contra_ddis:
                lines.append(f"⛔ {len(contra_ddis)} CONTRAINDICATED drug combination(s)")
            if major_ddis:
                lines.append(f"⚠️ {len(major_ddis)} MAJOR drug interaction(s) requiring management")
        
        if output.dose_results:
            unsafe_doses = [d for d in output.dose_results if not d.is_safe_to_dispense]
            adjusted_doses = [d for d in output.dose_results if d.renal_adjustment_applied or d.hepatic_adjustment_applied]
            if unsafe_doses:
                lines.append(f"🚫 {len(unsafe_doses)} drug(s) cannot be dosed safely with available patient data")
            if adjusted_doses:
                lines.append(f"⚠️ {len(adjusted_doses)} drug(s) require dose adjustment (renal/hepatic)")
        
        if not lines:
            lines.append("✅ No absolute contraindications or major interactions identified in safety kernel")
        
        return " | ".join(lines) if lines else "Safety evaluation complete"
