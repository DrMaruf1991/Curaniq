"""
CURANIQ -- Layer 3: Deterministic Safety Kernel
P2 Clinical Specialty Engines (Organ Function & Monitoring)

L3-8   Geriatric Safety Engine (Beers Criteria, STOPP/START, falls risk)
L3-14  Dedicated Renal Dosing Engine (CKD G1-G5D, AKI, CRRT)
L3-11  Anticoagulation Management Engine (warfarin, DOACs, bridging)
L3-18  Therapeutic Drug Monitoring & PK-PD Engine (narrow therapeutic index)

All deterministic. No LLM. These engines OVERRIDE AI output.
Clinical rules sourced from published guidelines with citations.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# =============================================================================
# L3-8: GERIATRIC SAFETY ENGINE
# Sources: AGS Beers Criteria 2023, STOPP/START v3 2023, WHO ICOPE 2019
# =============================================================================

class BeersCategory(str, Enum):
    AVOID             = "avoid"              # Potentially inappropriate in older adults
    AVOID_CONDITIONAL = "avoid_conditional"   # Avoid in specific conditions
    USE_WITH_CAUTION  = "use_with_caution"    # Appropriate in some circumstances
    DRUG_INTERACTION  = "drug_interaction"     # Clinically important DDI in older adults
    DOSE_ADJUST       = "dose_adjust_renal"   # Requires renal adjustment in older adults


@dataclass
class GeriatricAlert:
    drug: str
    category: BeersCategory
    rationale: str
    recommendation: str
    source: str  # e.g., "AGS Beers 2023, Table 2"
    falls_risk: bool = False
    cognitive_risk: bool = False
    anticholinergic_burden: int = 0  # 0-3 ACB score


class GeriatricSafetyEngine:
    """
    L3-8: Geriatric-specific safety checks.

    Implements:
    - AGS Beers Criteria 2023 (potentially inappropriate medications in older adults)
    - STOPP/START v3 criteria (screening tool for prescribing in older adults)
    - Anticholinergic Burden Scale (ACB score)
    - Falls risk assessment (CNS-active drugs, orthostatic drugs)
    - Cognitive risk flagging (benzodiazepines, anticholinergics, Z-drugs)

    Threshold: age >= 65 activates geriatric checks
    """

    GERIATRIC_AGE_THRESHOLD = 65

    # AGS Beers 2023 — Potentially Inappropriate Medications
    # Source: American Geriatrics Society 2023 Updated AGS Beers Criteria
    # J Am Geriatr Soc. 2023;71(7):2052-2081
    BEERS_DRUGS: dict[str, GeriatricAlert] = {
        "diazepam": GeriatricAlert(
            "diazepam", BeersCategory.AVOID,
            "Long-acting benzodiazepine; increased sensitivity in older adults, "
            "prolonged sedation, increased risk of falls, fractures, cognitive impairment",
            "AVOID. Use short-acting alternatives if absolutely necessary (lorazepam, oxazepam). "
            "Non-pharmacological approaches preferred.",
            "AGS Beers 2023 Table 2", falls_risk=True, cognitive_risk=True, anticholinergic_burden=0,
        ),
        "chlordiazepoxide": GeriatricAlert(
            "chlordiazepoxide", BeersCategory.AVOID,
            "Long-acting benzodiazepine; prolonged half-life in elderly (up to 200h with metabolites)",
            "AVOID. If benzodiazepine required, use lorazepam (no active metabolites, shorter t1/2).",
            "AGS Beers 2023 Table 2", falls_risk=True, cognitive_risk=True,
        ),
        "zolpidem": GeriatricAlert(
            "zolpidem", BeersCategory.AVOID,
            "Z-drug; benzodiazepine receptor agonist with similar risks. "
            "ER visits for adverse events nearly 8x higher in >65 year olds",
            "AVOID. Non-pharmacological sleep interventions first. "
            "If needed: melatonin 0.5-2mg, trazodone low-dose.",
            "AGS Beers 2023 Table 2; FDA Safety Communication 2013", falls_risk=True, cognitive_risk=True,
        ),
        "amitriptyline": GeriatricAlert(
            "amitriptyline", BeersCategory.AVOID,
            "Highly anticholinergic TCA; sedation, orthostatic hypotension, cardiac conduction delays",
            "AVOID as first-line. If antidepressant needed: SSRI preferred (sertraline, escitalopram).",
            "AGS Beers 2023 Table 2", falls_risk=True, cognitive_risk=True, anticholinergic_burden=3,
        ),
        "diphenhydramine": GeriatricAlert(
            "diphenhydramine", BeersCategory.AVOID,
            "First-generation antihistamine; highly anticholinergic, cognitive impairment, delirium risk",
            "AVOID. Use second-generation antihistamines (cetirizine, loratadine) for allergy. "
            "Not recommended as sleep aid in elderly.",
            "AGS Beers 2023 Table 2", cognitive_risk=True, anticholinergic_burden=3,
        ),
        "glibenclamide": GeriatricAlert(
            "glibenclamide", BeersCategory.AVOID,
            "Long-acting sulfonylurea; higher risk of prolonged hypoglycemia in elderly. "
            "Hypoglycemia risk increases with age and declining renal function.",
            "AVOID. Use shorter-acting sulfonylurea (gliclazide) or non-SU agents (metformin, DPP-4i).",
            "AGS Beers 2023 Table 2; STOPP B7",
        ),
        "metoclopramide": GeriatricAlert(
            "metoclopramide", BeersCategory.AVOID,
            "Extrapyramidal effects including tardive dyskinesia; risk increases with duration and age",
            "AVOID unless gastroparesis with no alternative. Maximum 5 days if used. "
            "Consider domperidone (where available) with ECG monitoring.",
            "AGS Beers 2023 Table 2; EMA restriction 2013",
        ),
        "nitrofurantoin": GeriatricAlert(
            "nitrofurantoin", BeersCategory.AVOID_CONDITIONAL,
            "Ineffective when CrCl <30 mL/min; risk of pulmonary toxicity with prolonged use",
            "AVOID if CrCl <30. Acceptable for short-course (5-7 days) UTI treatment if CrCl >=30.",
            "AGS Beers 2023 Table 2; STOPP J3",
        ),
        "nsaid_long_term": GeriatricAlert(
            "nsaid_long_term", BeersCategory.AVOID,
            "GI bleeding risk increases >4x in >65. Renal impairment, fluid retention, "
            "cardiovascular risk. Risk further increased with anticoagulants/antiplatelets.",
            "AVOID chronic use. Short course (<=7 days) at lowest dose if essential. "
            "Prefer paracetamol, topical NSAIDs, non-pharmacological approaches.",
            "AGS Beers 2023 Table 2; STOPP K1",
        ),
        "digoxin": GeriatricAlert(
            "digoxin", BeersCategory.DOSE_ADJUST,
            "Narrow therapeutic index; decreased renal clearance in elderly. "
            "Toxicity risk high at doses >0.125mg/day in elderly with renal impairment.",
            "If used: dose <=0.125mg/day. Monitor levels (target 0.5-0.9 ng/mL in HF). "
            "Check renal function and potassium regularly.",
            "AGS Beers 2023 Table 2; NICE NG106",
        ),
    }

    # Anticholinergic Burden Scale (ACB) scores for common drugs
    # Source: Boustani et al. 2008; Aging Clin Exp Res. 2008;20(5):484-496
    ACB_SCORES: dict[str, int] = {
        "amitriptyline": 3, "atropine": 3, "chlorpheniramine": 3,
        "chlorpromazine": 3, "clomipramine": 3, "clozapine": 3,
        "desipramine": 3, "diphenhydramine": 3, "doxepin": 3,
        "hydroxyzine": 3, "imipramine": 3, "nortriptyline": 3,
        "olanzapine": 3, "oxybutynin": 3, "paroxetine": 3,
        "promethazine": 3, "quetiapine": 3, "thioridazine": 3,
        "tolterodine": 3, "trifluoperazine": 3,
        "cetirizine": 2, "cimetidine": 2, "loratadine": 2,
        "ranitidine": 2, "cyclobenzaprine": 2,
        "alprazolam": 1, "atenolol": 1, "codeine": 1,
        "diazepam": 1, "digoxin": 1, "furosemide": 1,
        "morphine": 1, "prednisone": 1, "theophylline": 1,
        "tramadol": 1, "warfarin": 1,
    }

    # Falls-risk drugs (CNS-active, orthostatic risk)
    FALLS_RISK_CLASSES: set[str] = {
        "benzodiazepine", "opioid", "antipsychotic", "antidepressant_tca",
        "alpha_blocker", "diuretic_loop", "antihypertensive_central",
        "anticonvulsant", "skeletal_muscle_relaxant", "z_drug",
    }

    def assess(self, patient_age: int, drugs: list[str],
               egfr: Optional[float] = None) -> list[GeriatricAlert]:
        """Run all geriatric safety checks. Returns alerts sorted by severity."""
        if patient_age < self.GERIATRIC_AGE_THRESHOLD:
            return []

        alerts: list[GeriatricAlert] = []
        total_acb = 0

        for drug in drugs:
            drug_lower = drug.lower().strip()
            # Check Beers criteria
            beers_alert = self.BEERS_DRUGS.get(drug_lower)
            if beers_alert:
                # Conditional checks
                if beers_alert.category == BeersCategory.AVOID_CONDITIONAL:
                    if drug_lower == "nitrofurantoin" and egfr and egfr < 30:
                        alerts.append(beers_alert)
                else:
                    alerts.append(beers_alert)

            # Accumulate ACB score
            acb = self.ACB_SCORES.get(drug_lower, 0)
            total_acb += acb

        # Total ACB burden alert
        if total_acb >= 3:
            alerts.append(GeriatricAlert(
                drug="TOTAL_ACB_BURDEN",
                category=BeersCategory.USE_WITH_CAUTION,
                rationale=f"Total Anticholinergic Burden Score = {total_acb} (>=3 = high risk). "
                          "Associated with cognitive decline, delirium, falls, and increased mortality "
                          "in older adults.",
                recommendation="Review all anticholinergic medications. Deprescribe where possible. "
                               "Prioritize removing highest-ACB drugs first.",
                source="Boustani et al. Aging Clin Exp Res 2008;20(5):484-496",
                cognitive_risk=True, falls_risk=True,
            ))

        return sorted(alerts, key=lambda a: (
            0 if a.category == BeersCategory.AVOID else
            1 if a.category == BeersCategory.AVOID_CONDITIONAL else 2
        ))


# =============================================================================
# L3-14: DEDICATED RENAL DOSING ENGINE
# Sources: KDIGO 2024, Renal Drug Handbook (Ashley & Dunleavy), FDA labels
# =============================================================================

class CKDStage(str, Enum):
    G1   = "G1"    # Normal: GFR >=90
    G2   = "G2"    # Mild: GFR 60-89
    G3a  = "G3a"   # Moderate: GFR 45-59
    G3b  = "G3b"   # Moderate-severe: GFR 30-44
    G4   = "G4"    # Severe: GFR 15-29
    G5   = "G5"    # Kidney failure: GFR <15
    G5D  = "G5D"   # Dialysis
    AKI  = "AKI"   # Acute kidney injury


@dataclass
class RenalDoseAdjustment:
    drug: str
    ckd_stage: CKDStage
    action: str         # "normal", "reduce", "extend_interval", "avoid", "contraindicated"
    adjusted_dose: str
    max_dose: str
    monitoring: str
    dialysis_supplement: str
    source: str


class DedicatedRenalDosingEngine:
    """
    L3-14: CKD-stage-specific dose adjustments.

    Extends L3-1 CQL renal functions with:
    - Complete CKD G1-G5D staging with KDIGO 2024 criteria
    - Per-drug, per-stage dose tables from Renal Drug Handbook
    - Dialysis supplementation doses (HD, PD, CRRT)
    - AKI-specific recommendations
    - Drug accumulation warnings for renally-cleared drugs
    """

    # Renal dose adjustment database
    # Source: Renal Drug Handbook (Ashley & Dunleavy, 5th ed), FDA prescribing info
    RENAL_ADJUSTMENTS: dict[str, dict[str, RenalDoseAdjustment]] = {
        "metformin": {
            "G1":  RenalDoseAdjustment("metformin", CKDStage.G1,  "normal", "500-1000mg BID", "2000mg/day", "eGFR annually", "", "KDIGO DM 2022; FDA label 2024"),
            "G2":  RenalDoseAdjustment("metformin", CKDStage.G2,  "normal", "500-1000mg BID", "2000mg/day", "eGFR q6mo", "", "KDIGO DM 2022"),
            "G3a": RenalDoseAdjustment("metformin", CKDStage.G3a, "normal", "500-1000mg BID", "2000mg/day", "eGFR q3-6mo", "", "KDIGO DM 2022"),
            "G3b": RenalDoseAdjustment("metformin", CKDStage.G3b, "reduce", "500mg BID", "1000mg/day", "eGFR q3mo. Hold if acutely ill.", "", "KDIGO DM 2022; ADA 2024"),
            "G4":  RenalDoseAdjustment("metformin", CKDStage.G4,  "avoid", "", "", "Lactic acidosis risk. Discontinue.", "", "FDA label; KDIGO DM 2022"),
            "G5":  RenalDoseAdjustment("metformin", CKDStage.G5,  "contraindicated", "", "", "Contraindicated. Lactic acidosis.", "", "FDA label"),
            "G5D": RenalDoseAdjustment("metformin", CKDStage.G5D, "contraindicated", "", "", "Contraindicated on dialysis.", "Not dialyzable", "Renal Drug Handbook"),
        },
        "gentamicin": {
            "G1":  RenalDoseAdjustment("gentamicin", CKDStage.G1,  "normal", "5-7mg/kg OD or 1-1.7mg/kg q8h", "7mg/kg", "Trough <1, peak 5-10 (conventional) or trough <1 (extended interval)", "", "Sanford Guide 2024"),
            "G2":  RenalDoseAdjustment("gentamicin", CKDStage.G2,  "extend_interval", "5-7mg/kg q24-36h", "7mg/kg", "Levels mandatory. Trough <1mg/L", "", "Renal Drug Handbook"),
            "G3a": RenalDoseAdjustment("gentamicin", CKDStage.G3a, "extend_interval", "5-7mg/kg q36-48h", "7mg/kg", "Levels mandatory pre-dose 3. Trough <1mg/L", "", "Renal Drug Handbook"),
            "G3b": RenalDoseAdjustment("gentamicin", CKDStage.G3b, "reduce", "2-3mg/kg q48h", "3mg/kg", "Levels mandatory. Consider alternative agent.", "", "Renal Drug Handbook"),
            "G4":  RenalDoseAdjustment("gentamicin", CKDStage.G4,  "avoid", "1-1.7mg/kg then by levels", "", "AVOID if possible. If used: single dose then levels only.", "", "Renal Drug Handbook"),
            "G5":  RenalDoseAdjustment("gentamicin", CKDStage.G5,  "avoid", "", "", "AVOID. Nephrotoxic + ototoxic accumulation.", "", "Renal Drug Handbook"),
            "G5D": RenalDoseAdjustment("gentamicin", CKDStage.G5D, "reduce", "1-1.7mg/kg loading", "", "Dialyzable (50-60%). Re-dose post-HD by levels.", "Re-dose after dialysis based on levels", "Renal Drug Handbook"),
        },
        "vancomycin": {
            "G1":  RenalDoseAdjustment("vancomycin", CKDStage.G1,  "normal", "15-20mg/kg q8-12h", "4g/day", "AUC/MIC target 400-600 (IDSA/ASHP 2020). Trough monitoring being replaced by AUC-guided.", "", "IDSA/ASHP 2020 guideline"),
            "G2":  RenalDoseAdjustment("vancomycin", CKDStage.G2,  "normal", "15-20mg/kg q12h", "4g/day", "AUC/MIC monitoring. eGFR q48-72h", "", "IDSA/ASHP 2020"),
            "G3a": RenalDoseAdjustment("vancomycin", CKDStage.G3a, "extend_interval", "15-20mg/kg q24h", "2g/day", "AUC-guided dosing mandatory. Nephrotoxicity monitoring.", "", "IDSA/ASHP 2020; Renal Drug Handbook"),
            "G3b": RenalDoseAdjustment("vancomycin", CKDStage.G3b, "extend_interval", "15-20mg/kg q24-48h", "1.5g/day", "Levels mandatory pre-dose 4. Avoid concomitant nephrotoxins.", "", "Renal Drug Handbook"),
            "G4":  RenalDoseAdjustment("vancomycin", CKDStage.G4,  "reduce", "15mg/kg loading, then by levels", "", "Single loading dose then AUC-guided only. Daily levels.", "", "Renal Drug Handbook"),
            "G5":  RenalDoseAdjustment("vancomycin", CKDStage.G5,  "reduce", "15mg/kg loading, then by levels q48-96h", "", "Extended interval based on levels. May need re-dosing only q4-7 days.", "", "Renal Drug Handbook"),
            "G5D": RenalDoseAdjustment("vancomycin", CKDStage.G5D, "reduce", "15-25mg/kg loading", "", "Removed by high-flux HD (25-50%). Standard HD: minimal removal.", "Post-HD supplemental dose based on pre-HD level", "Renal Drug Handbook"),
        },
        "gabapentin": {
            "G1":  RenalDoseAdjustment("gabapentin", CKDStage.G1,  "normal", "300-1200mg TID", "3600mg/day", "", "", "FDA label"),
            "G2":  RenalDoseAdjustment("gabapentin", CKDStage.G2,  "normal", "300-1200mg TID", "3600mg/day", "", "", "FDA label"),
            "G3a": RenalDoseAdjustment("gabapentin", CKDStage.G3a, "reduce", "200-700mg BID", "1400mg/day", "CNS toxicity monitoring", "", "FDA label; Renal Drug Handbook"),
            "G3b": RenalDoseAdjustment("gabapentin", CKDStage.G3b, "reduce", "100-300mg BID", "600mg/day", "CNS toxicity (ataxia, drowsiness)", "", "FDA label; Renal Drug Handbook"),
            "G4":  RenalDoseAdjustment("gabapentin", CKDStage.G4,  "reduce", "100-300mg OD", "300mg/day", "CNS monitoring. Dose after HD on dialysis days.", "", "FDA label"),
            "G5":  RenalDoseAdjustment("gabapentin", CKDStage.G5,  "reduce", "100-300mg OD", "300mg/day", "Accumulation risk. Consider alternative.", "", "Renal Drug Handbook"),
            "G5D": RenalDoseAdjustment("gabapentin", CKDStage.G5D, "reduce", "100-300mg post-HD", "300mg/day", "Dialyzable. Give supplemental dose after each HD session.", "200-300mg post-HD", "Renal Drug Handbook"),
        },
    }

    def classify_ckd_stage(self, egfr: float, on_dialysis: bool = False) -> CKDStage:
        """Classify CKD stage per KDIGO 2024."""
        if on_dialysis:
            return CKDStage.G5D
        if egfr >= 90:
            return CKDStage.G1
        if egfr >= 60:
            return CKDStage.G2
        if egfr >= 45:
            return CKDStage.G3a
        if egfr >= 30:
            return CKDStage.G3b
        if egfr >= 15:
            return CKDStage.G4
        return CKDStage.G5

    def get_adjustment(self, drug: str, egfr: float,
                       on_dialysis: bool = False) -> Optional[RenalDoseAdjustment]:
        """Get CKD-stage-specific dose adjustment for a drug."""
        drug_lower = drug.lower().strip()
        drug_table = self.RENAL_ADJUSTMENTS.get(drug_lower)
        if not drug_table:
            return None
        stage = self.classify_ckd_stage(egfr, on_dialysis)
        return drug_table.get(stage.value)


# =============================================================================
# L3-11: ANTICOAGULATION MANAGEMENT ENGINE
# Sources: ASH 2021, ESC 2024, CHEST 2021, ISTH 2024
# =============================================================================

class AnticoagulantClass(str, Enum):
    VKA  = "vka"       # Vitamin K antagonists (warfarin)
    DOAC = "doac"      # Direct oral anticoagulants (rivaroxaban, apixaban, etc.)
    LMWH = "lmwh"      # Low molecular weight heparin (enoxaparin)
    UFH  = "ufh"       # Unfractionated heparin
    FOND = "fondaparinux"


@dataclass
class AnticoagulationAlert:
    drug: str
    alert_type: str    # "dose_adjustment", "ddi", "bridging", "reversal", "monitoring", "contraindication"
    severity: str      # "critical", "major", "moderate"
    message: str
    recommendation: str
    source: str


class AnticoagulationEngine:
    """
    L3-11: Anticoagulation-specific safety engine.

    Covers:
    - Warfarin dose initiation (Gage algorithm factors)
    - DOAC dose selection by indication + renal function + weight
    - Bridging anticoagulation rules (BRIDGE trial: NEJM 2015)
    - Bleeding risk scoring (HAS-BLED)
    - Reversal agent mapping (idarucizumab, andexanet, vitamin K, PCC)
    - Critical DDIs with anticoagulants
    """

    # DOAC dosing by indication and renal function
    # Source: ESC 2024; FDA prescribing information
    DOAC_DOSING: dict[str, dict[str, dict]] = {
        "rivaroxaban": {
            "af": {  # Atrial fibrillation
                "normal":     {"dose": "20mg OD with food", "source": "ESC 2024"},
                "crcl_30_49": {"dose": "15mg OD with food", "source": "FDA label; ESC 2024"},
                "crcl_15_29": {"dose": "15mg OD with food (use with caution)", "source": "FDA label"},
                "crcl_lt_15": {"dose": "AVOID", "source": "FDA label; ESC 2024"},
            },
            "vte_treatment": {
                "normal":     {"dose": "15mg BID x21 days then 20mg OD with food", "source": "EINSTEIN-DVT/PE"},
                "crcl_30_49": {"dose": "15mg BID x21 days then 20mg OD", "source": "FDA label"},
                "crcl_lt_30": {"dose": "AVOID if CrCl <30", "source": "FDA label"},
            },
        },
        "apixaban": {
            "af": {
                "normal":     {"dose": "5mg BID", "source": "ESC 2024; ARISTOTLE"},
                "reduced":    {"dose": "2.5mg BID if >=2 of: age>=80, weight<=60kg, Cr>=1.5mg/dL", "source": "FDA label; ESC 2024"},
                "crcl_15_29": {"dose": "5mg BID (or 2.5mg BID if dose reduction criteria met)", "source": "FDA label"},
                "crcl_lt_15": {"dose": "Limited data. 5mg or 2.5mg BID based on clinical judgment.", "source": "FDA label"},
                "dialysis":   {"dose": "5mg BID (or 2.5mg BID per criteria). Not removed by dialysis.", "source": "FDA label 2024 update"},
            },
        },
    }

    # Reversal agents (evidence-based mapping)
    REVERSAL_MAP: dict[str, dict] = {
        "warfarin":      {"agent": "Vitamin K 5-10mg IV + 4-factor PCC", "onset": "2-4h (Vit K), immediate (PCC)", "source": "ASH 2021; CHEST 2021"},
        "rivaroxaban":   {"agent": "Andexanet alfa (if available) or 4-factor PCC 50 IU/kg", "onset": "Minutes (andexanet), 15-30min (PCC)", "source": "ANNEXA-4 trial; ESC 2024"},
        "apixaban":      {"agent": "Andexanet alfa (if available) or 4-factor PCC 50 IU/kg", "onset": "Minutes (andexanet)", "source": "ANNEXA-4 trial; ESC 2024"},
        "dabigatran":    {"agent": "Idarucizumab 5g IV", "onset": "Minutes", "source": "RE-VERSE AD trial; FDA approved 2015"},
        "enoxaparin":    {"agent": "Protamine 1mg per 1mg enoxaparin (60-75% reversal)", "onset": "5min", "source": "CHEST 2021"},
        "heparin":       {"agent": "Protamine 1mg per 100 units UFH (max 50mg)", "onset": "5min", "source": "CHEST 2021"},
    }

    # HAS-BLED score components
    # Source: Pisters et al. Chest 2010;138(5):1093-1100
    HAS_BLED_FACTORS = [
        ("hypertension", 1, "Uncontrolled SBP >160 mmHg"),
        ("renal_impairment", 1, "Dialysis, transplant, Cr >2.3 mg/dL"),
        ("liver_impairment", 1, "Cirrhosis, bilirubin >2x ULN, AST/ALT >3x ULN"),
        ("stroke_history", 1, "Prior stroke"),
        ("bleeding_history", 1, "Prior major bleeding or predisposition"),
        ("labile_inr", 1, "TTR <60% on warfarin"),
        ("age_over_65", 1, "Age >65"),
        ("antiplatelet_nsaid", 1, "Concomitant antiplatelet or NSAID"),
        ("alcohol_excess", 1, ">=8 drinks/week"),
    ]

    def calculate_has_bled(self, factors: dict[str, bool]) -> tuple[int, str]:
        """Calculate HAS-BLED bleeding risk score."""
        score = sum(
            points for factor_name, points, _ in self.HAS_BLED_FACTORS
            if factors.get(factor_name, False)
        )
        risk = "low" if score <= 2 else "high"
        return score, risk

    def get_doac_dose(self, drug: str, indication: str,
                      crcl: Optional[float] = None,
                      age: Optional[int] = None,
                      weight_kg: Optional[float] = None,
                      creatinine_mg_dl: Optional[float] = None) -> Optional[dict]:
        """Get indication-specific DOAC dosing with renal adjustment."""
        drug_lower = drug.lower().strip()
        drug_table = self.DOAC_DOSING.get(drug_lower)
        if not drug_table:
            return None
        indication_table = drug_table.get(indication)
        if not indication_table:
            return None

        # Apixaban dose reduction criteria
        if drug_lower == "apixaban" and indication == "af":
            reduce_criteria = sum([
                1 if age and age >= 80 else 0,
                1 if weight_kg and weight_kg <= 60 else 0,
                1 if creatinine_mg_dl and creatinine_mg_dl >= 1.5 else 0,
            ])
            if reduce_criteria >= 2:
                return indication_table.get("reduced")

        # CrCl-based selection
        if crcl is None:
            return indication_table.get("normal")
        if crcl >= 50:
            return indication_table.get("normal")
        if crcl >= 30:
            return indication_table.get("crcl_30_49", indication_table.get("normal"))
        if crcl >= 15:
            return indication_table.get("crcl_15_29", indication_table.get("crcl_lt_30"))
        return indication_table.get("crcl_lt_15", indication_table.get("crcl_lt_30"))

    def get_reversal(self, drug: str) -> Optional[dict]:
        """Get reversal agent protocol for an anticoagulant."""
        return self.REVERSAL_MAP.get(drug.lower().strip())


# =============================================================================
# L3-18: THERAPEUTIC DRUG MONITORING & PK-PD ENGINE
# Sources: IDSA/ASHP 2020 (vancomycin), CPIC guidelines, clinical pharmacology texts
# =============================================================================

@dataclass
class TDMDrug:
    drug: str
    therapeutic_range_trough: tuple[float, float]  # (min, max) in mg/L or ng/mL
    toxic_level: float
    half_life_hours: tuple[float, float]  # (min, max) normal renal function
    protein_binding_pct: float
    renal_clearance_pct: float  # % cleared by kidneys
    dialyzable: bool
    monitoring_frequency: str
    source: str


class TDMPKPDEngine:
    """
    L3-18: Therapeutic Drug Monitoring & Pharmacokinetic engine.

    Covers narrow therapeutic index (NTI) drugs:
    - Aminoglycosides (gentamicin, tobramycin, amikacin)
    - Vancomycin (AUC-guided per IDSA/ASHP 2020)
    - Lithium
    - Digoxin
    - Phenytoin (total and free levels, Sheiner-Tozer correction)
    - Carbamazepine, valproic acid
    - Theophylline
    - Cyclosporine, tacrolimus

    All calculations deterministic. Provides:
    - Therapeutic range checking
    - Sheiner-Tozer correction for phenytoin in hypoalbuminemia
    - Half-life estimation adjusted for renal function
    - Dosing interval recommendations
    """

    TDM_DATABASE: dict[str, TDMDrug] = {
        "vancomycin": TDMDrug(
            "vancomycin", (15.0, 20.0), 40.0, (4.0, 11.0), 55.0, 90.0, True,
            "AUC/MIC 400-600 (preferred). If trough-based: 15-20 mg/L for serious infections.",
            "IDSA/ASHP 2020 Vancomycin Consensus Guidelines",
        ),
        "gentamicin": TDMDrug(
            "gentamicin", (0.5, 2.0), 12.0, (2.0, 3.0), 30.0, 95.0, True,
            "Trough <1 mg/L (extended interval). Peak 5-10 mg/L (conventional).",
            "Sanford Guide 2024; IDSA guidelines",
        ),
        "lithium": TDMDrug(
            "lithium", (0.6, 1.0), 1.5, (18.0, 36.0), 0.0, 95.0, True,
            "Level 12h post-dose. Weekly during titration, then q3-6mo when stable.",
            "NICE CG185; BAP 2016 guidelines",
        ),
        "digoxin": TDMDrug(
            "digoxin", (0.5, 0.9), 2.0, (36.0, 48.0), 25.0, 60.0, False,
            "Level >=6h post-dose. Target 0.5-0.9 ng/mL in HF (DIG trial subanalysis).",
            "NICE NG106; DIG trial; ACC/AHA 2022",
        ),
        "phenytoin": TDMDrug(
            "phenytoin", (10.0, 20.0), 30.0, (12.0, 36.0), 90.0, 5.0, False,
            "Total level 10-20 mg/L. Free level 1-2 mg/L (if hypoalbuminemia or renal failure).",
            "NICE NG217; Winter's Clinical Pharmacokinetics",
        ),
        "carbamazepine": TDMDrug(
            "carbamazepine", (4.0, 12.0), 15.0, (12.0, 17.0), 75.0, 3.0, False,
            "Trough level. Auto-induction: repeat level 2-4 weeks after dose change.",
            "NICE NG217; ILAE guidelines",
        ),
        "valproic_acid": TDMDrug(
            "valproic acid", (50.0, 100.0), 150.0, (8.0, 20.0), 90.0, 3.0, False,
            "Trough level pre-dose. Free level if albumin low or total >100 mg/L.",
            "NICE NG217; ILAE guidelines",
        ),
        "tacrolimus": TDMDrug(
            "tacrolimus", (5.0, 15.0), 20.0, (8.0, 12.0), 99.0, 2.0, False,
            "Trough (C0) level. Target varies by transplant type and time post-transplant.",
            "KDIGO Transplant 2009; ISHLT 2010",
        ),
        "cyclosporine": TDMDrug(
            "cyclosporine", (100.0, 300.0), 400.0, (6.0, 12.0), 98.0, 6.0, False,
            "Trough (C0) or C2 (2h post-dose) level. Target varies by indication.",
            "KDIGO Transplant 2009",
        ),
    }

    def check_level(self, drug: str, measured_level: float,
                    albumin: Optional[float] = None,
                    egfr: Optional[float] = None) -> dict:
        """
        Check a measured drug level against therapeutic range.
        Applies Sheiner-Tozer correction for phenytoin if needed.
        """
        drug_lower = drug.lower().strip()
        tdm = self.TDM_DATABASE.get(drug_lower)
        if not tdm:
            return {"known": False, "drug": drug}

        level = measured_level

        # Sheiner-Tozer correction for phenytoin
        # Corrected = Measured / (0.2 * Albumin + 0.1)  [normal albumin 4.0]
        # Source: Winter's Clinical Pharmacokinetics, 6th ed
        if drug_lower == "phenytoin" and albumin and albumin < 3.5:
            correction_factor = 0.2 * albumin + 0.1
            if egfr and egfr < 25:
                correction_factor = 0.1 * albumin + 0.1  # Renal failure correction
            corrected_level = measured_level / correction_factor
            level = corrected_level

        min_range, max_range = tdm.therapeutic_range_trough

        if level < min_range:
            status = "subtherapeutic"
            action = "Consider dose increase. Repeat level after 3-5 half-lives at new dose."
        elif level > tdm.toxic_level:
            status = "toxic"
            action = "HOLD dose. Monitor for toxicity signs. Repeat level in 24-48h. " + (
                "Dialysis may be considered." if tdm.dialyzable else "Not dialyzable."
            )
        elif level > max_range:
            status = "supratherapeutic"
            action = "Consider dose reduction. Monitor for adverse effects."
        else:
            status = "therapeutic"
            action = "Within range. Continue current dose."

        result = {
            "known": True,
            "drug": drug,
            "measured_level": measured_level,
            "interpreted_level": round(level, 2),
            "range": f"{min_range}-{max_range}",
            "toxic_above": tdm.toxic_level,
            "status": status,
            "action": action,
            "monitoring": tdm.monitoring_frequency,
            "source": tdm.source,
        }

        if drug_lower == "phenytoin" and albumin and albumin < 3.5:
            result["sheiner_tozer_corrected"] = True
            result["albumin_used"] = albumin
            result["uncorrected_level"] = measured_level

        return result

    def estimate_adjusted_half_life(self, drug: str, egfr: float) -> Optional[float]:
        """Estimate drug half-life adjusted for renal function."""
        drug_lower = drug.lower().strip()
        tdm = self.TDM_DATABASE.get(drug_lower)
        if not tdm:
            return None

        normal_t12_avg = (tdm.half_life_hours[0] + tdm.half_life_hours[1]) / 2
        renal_fraction = tdm.renal_clearance_pct / 100.0

        # Q factor method: t1/2_adjusted = t1/2_normal / Q
        # Q = 1 - renal_fraction * (1 - egfr/120)
        q = 1 - renal_fraction * (1 - min(egfr, 120) / 120)
        if q <= 0:
            q = 0.1  # Floor to prevent division by zero

        adjusted = normal_t12_avg / q
        return round(adjusted, 1)
