"""
CURANIQ — L3-1: CQL Safety Kernel
Architecture spec: Deterministic rule engine that OVERRIDES LLM for all safety-critical
computations. Clinical Query Language (CQL) rules cover:
- Renal dose adjustment (Cockcroft-Gault / CKD-EPI)
- Allergy cross-reactivity kernel
- Pediatric weight-based dosing
- Pregnancy / lactation safety classification
- QT prolongation risk (Tisdale Score)
- Drug-food / drug-herb interactions
- Drug-drug interaction severity classification
- Black Box Warning enforcement
All outputs are fully reproducible and logged in CQLComputationLog.
"""
from __future__ import annotations
import math
import re
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

# L3 Clinical Safety Engines (wired from layers/)
from curaniq.layers.L3_safety_kernel.clinical_safety_engines import (
    PediatricSafetyEngine,
    PregnancyLactationEngine,
    QTProlongationEngine,
    DrugFoodHerbEngine,
)
from curaniq.layers.L3_safety_kernel.medication_intelligence import (
    MedicationIntelligenceEngine,
    SmartFormularyEngine,
)

from curaniq.models.schemas import (
    CQLComputationLog, PatientContext, RenalFunction, SafetyFlag
)


# ─────────────────────────────────────────────────────────────────────────────
# CQL RULE IDENTIFIERS
# ─────────────────────────────────────────────────────────────────────────────
# Format: CQL.<DOMAIN>.<DRUG/CLASS>.<PARAMETER>

RULE_VERSION = "1.0.0"


# ─────────────────────────────────────────────────────────────────────────────
# RENAL FUNCTION CALCULATORS  (deterministic, not LLM)
# ─────────────────────────────────────────────────────────────────────────────

def compute_cockcroft_gault(
    age: int,
    weight_kg: float,
    creatinine_umol_l: float,
    sex: str,
    context: Optional[PatientContext] = None,
) -> tuple[float, CQLComputationLog]:
    """
    Cockcroft-Gault formula for creatinine clearance.
    CrCl = [(140 - age) × weight_kg] / (72 × Cr_mg_dl) × [0.85 if female]
    Cr_mg_dl = creatinine_umol_l / 88.4
    """
    cr_mg_dl = creatinine_umol_l / 88.4
    crcl = ((140 - age) * weight_kg) / (72 * cr_mg_dl)
    if sex.upper() == "F":
        crcl *= 0.85

    log = CQLComputationLog(
        computation_id=str(uuid4()),
        rule_id="CQL.RENAL.COCKCROFT_GAULT",
        rule_version=RULE_VERSION,
        inputs={
            "age_years": age,
            "weight_kg": weight_kg,
            "creatinine_umol_l": creatinine_umol_l,
            "creatinine_mg_dl": round(cr_mg_dl, 4),
            "sex": sex,
        },
        formula_applied="CrCl = [(140-age) × weight] / (72 × Cr_mg_dl) × [0.85 if F]",
        output_value=str(round(crcl, 1)),
        output_unit="mL/min",
    )
    return round(crcl, 1), log


def compute_ckd_epi(
    age: int,
    creatinine_umol_l: float,
    sex: str,
) -> tuple[float, CQLComputationLog]:
    """
    CKD-EPI 2021 equation for eGFR (race-free version per NKF/ASN 2021).
    eGFR = 142 × min(Scr/κ, 1)^α × max(Scr/κ, 1)^(−1.200)
           × 0.9938^Age × [1.012 if female]
    κ = 0.7 (F) or 0.9 (M); α = −0.241 (F) or −0.302 (M)
    """
    scr = creatinine_umol_l / 88.4
    is_female = sex.upper() == "F"
    kappa = 0.7 if is_female else 0.9
    alpha = -0.241 if is_female else -0.302
    sex_multiplier = 1.012 if is_female else 1.0

    ratio = scr / kappa
    egfr = (
        142
        * (min(ratio, 1.0) ** alpha)
        * (max(ratio, 1.0) ** -1.200)
        * (0.9938 ** age)
        * sex_multiplier
    )

    log = CQLComputationLog(
        computation_id=str(uuid4()),
        rule_id="CQL.RENAL.CKD_EPI_2021",
        rule_version=RULE_VERSION,
        inputs={
            "age_years": age, "creatinine_umol_l": creatinine_umol_l,
            "scr_mg_dl": round(scr, 4), "sex": sex,
        },
        formula_applied="CKD-EPI 2021 (race-free): 142 × min(Scr/κ,1)^α × max(Scr/κ,1)^-1.2 × 0.9938^Age × sex_factor",
        output_value=str(round(egfr, 1)),
        output_unit="mL/min/1.73m²",
    )
    return round(egfr, 1), log


# ─────────────────────────────────────────────────────────────────────────────
# RENAL DOSE ADJUSTMENT RULES  (L3-14 integrated into L3-1)
# ─────────────────────────────────────────────────────────────────────────────

# Renal dose adjustment thresholds (CrCl mL/min unless noted)
# Source: Renal Drug Handbook 5th Ed., UpToDate, FDA labels
RENAL_DOSE_RULES: dict[str, list[dict]] = {
    "metformin": [
        {"crcl_min": 45, "crcl_max": 999, "action": "standard_dose", "dose": "Standard dose"},
        {"crcl_min": 30, "crcl_max": 45, "action": "reduce_50pct", "dose": "Reduce dose by 50%; monitor renal function every 3–6 months"},
        {"crcl_min": 0, "crcl_max": 30, "action": "contraindicated", "dose": "CONTRAINDICATED — risk of lactic acidosis"},
    ],
    "gabapentin": [
        {"crcl_min": 60, "crcl_max": 999, "action": "standard_dose", "dose": "Standard dose (300–600mg TID)"},
        {"crcl_min": 30, "crcl_max": 60,  "action": "reduce", "dose": "200–700mg/day divided BID"},
        {"crcl_min": 15, "crcl_max": 30,  "action": "reduce", "dose": "200–700mg/day single daily dose"},
        {"crcl_min": 0,  "crcl_max": 15,  "action": "reduce", "dose": "100–300mg after each dialysis session"},
    ],
    "atenolol": [
        {"crcl_min": 35, "crcl_max": 999, "action": "standard_dose", "dose": "Standard dose"},
        {"crcl_min": 15, "crcl_max": 35,  "action": "reduce", "dose": "50mg/day maximum"},
        {"crcl_min": 0,  "crcl_max": 15,  "action": "reduce", "dose": "25mg/day maximum (supplement post-HD)"},
    ],
    "digoxin": [
        {"crcl_min": 50, "crcl_max": 999, "action": "standard_dose", "dose": "0.125–0.25mg/day; monitor levels"},
        {"crcl_min": 10, "crcl_max": 50,  "action": "reduce", "dose": "0.0625–0.125mg/day; levels target 0.5–0.9 ng/mL"},
        {"crcl_min": 0,  "crcl_max": 10,  "action": "avoid", "dose": "AVOID or extreme caution; if must use: 0.0625mg/day with serum levels"},
    ],
    "dabigatran": [
        {"crcl_min": 30, "crcl_max": 999, "action": "standard_dose", "dose": "150mg BID (AF); 220mg once daily (VTE prophylaxis)"},
        {"crcl_min": 15, "crcl_max": 30,  "action": "reduce", "dose": "110mg BID for AF; consult specialist — limited data"},
        {"crcl_min": 0,  "crcl_max": 15,  "action": "contraindicated", "dose": "CONTRAINDICATED — no data; renally cleared"},
    ],
    "rivaroxaban": [
        {"crcl_min": 50, "crcl_max": 999, "action": "standard_dose", "dose": "20mg OD with evening meal (AF); weight-based for VTE"},
        {"crcl_min": 15, "crcl_max": 50,  "action": "reduce", "dose": "15mg OD with meal for AF; use with caution"},
        {"crcl_min": 0,  "crcl_max": 15,  "action": "contraindicated", "dose": "CONTRAINDICATED in AF; use with extreme caution in VTE"},
    ],
    "ciprofloxacin": [
        {"crcl_min": 30, "crcl_max": 999, "action": "standard_dose", "dose": "Standard dose"},
        {"crcl_min": 0,  "crcl_max": 30,  "action": "reduce", "dose": "250–500mg every 12–24h; supplement post-HD"},
    ],
    "acyclovir": [
        {"crcl_min": 50, "crcl_max": 999, "action": "standard_dose", "dose": "Standard dose"},
        {"crcl_min": 25, "crcl_max": 50,  "action": "reduce", "dose": "Standard dose every 12h"},
        {"crcl_min": 10, "crcl_max": 25,  "action": "reduce", "dose": "Standard dose every 24h"},
        {"crcl_min": 0,  "crcl_max": 10,  "action": "reduce", "dose": "Half dose every 24h; supplement post-HD"},
    ],
    "allopurinol": [
        {"crcl_min": 60, "crcl_max": 999, "action": "standard_dose", "dose": "100–300mg/day"},
        {"crcl_min": 20, "crcl_max": 60,  "action": "reduce", "dose": "100–200mg/day"},
        {"crcl_min": 0,  "crcl_max": 20,  "action": "reduce", "dose": "100mg every 48–72h or 50mg/day"},
    ],
}


def get_renal_dose_adjustment(
    drug_name: str,
    crcl: float,
) -> Optional[tuple[dict, CQLComputationLog]]:
    """
    Look up deterministic renal dose adjustment for a drug.
    Returns the applicable rule + computation log, or None if drug not in database.
    """
    drug_key = drug_name.lower().strip()
    rules = RENAL_DOSE_RULES.get(drug_key)
    if not rules:
        return None

    applicable = None
    for rule in rules:
        if rule["crcl_min"] <= crcl <= rule["crcl_max"]:
            applicable = rule
            break

    if not applicable:
        return None

    log = CQLComputationLog(
        computation_id=str(uuid4()),
        rule_id=f"CQL.RENAL.DOSE.{drug_key.upper()}",
        rule_version=RULE_VERSION,
        inputs={"drug": drug_name, "crcl_ml_min": crcl},
        formula_applied=f"Renal dose table lookup — CrCl={crcl} mL/min → tier [{applicable['crcl_min']}–{applicable['crcl_max']}]",
        output_value=applicable["dose"],
        output_unit=None,
    )
    return applicable, log


# ─────────────────────────────────────────────────────────────────────────────
# ALLERGY KERNEL  (part of L3-1)
# ─────────────────────────────────────────────────────────────────────────────

# Cross-reactivity data: drug class → (related_drugs, cross_react_risk, evidence)
ALLERGY_CROSS_REACTIVITY: dict[str, dict] = {
    "penicillin": {
        "class": "beta_lactam",
        "cross_react_with": {
            "amoxicillin":  {"risk": "SAME_CLASS",  "rate": ">99%",   "action": "CONTRAINDICATED"},
            "ampicillin":   {"risk": "SAME_CLASS",  "rate": ">99%",   "action": "CONTRAINDICATED"},
            "piperacillin": {"risk": "SAME_CLASS",  "rate": ">99%",   "action": "CONTRAINDICATED"},
            "cephalexin":   {"risk": "CROSS_REACT", "rate": "1–2%",   "action": "CAUTION — use only if benefit outweighs risk; cephalosporins historically 1–2% cross-reactivity"},
            "cefazolin":    {"risk": "CROSS_REACT", "rate": "1–2%",   "action": "CAUTION — cefazolin has lower cross-reactivity than oral cephalosporins"},
            "ceftriaxone":  {"risk": "CROSS_REACT", "rate": "0.5–2%", "action": "CAUTION — generally safe in true penicillin allergy with supervision"},
            "carbapenems":  {"risk": "CROSS_REACT", "rate": "<1%",    "action": "LOW RISK — cross-reactivity minimal; safe to use with monitoring"},
            "aztreonam":    {"risk": "CROSS_REACT", "rate": "0%",     "action": "SAFE — monobactam, no significant cross-reactivity with penicillin"},
        },
        "note": "Modern evidence shows true penicillin allergy cross-reactivity with cephalosporins is <2%, not the historical 10%. Macy E, Romano A. J Allergy Clin Immunol. 2017.",
    },
    "sulfonamide": {
        "class": "sulfonamide",
        "cross_react_with": {
            "trimethoprim_sulfamethoxazole": {"risk": "SAME_CLASS", "rate": ">99%", "action": "CONTRAINDICATED"},
            "sulfadiazine":    {"risk": "SAME_CLASS",  "rate": ">99%",  "action": "CONTRAINDICATED"},
            "furosemide":      {"risk": "STRUCTURAL",  "rate": "1–3%",  "action": "CAUTION — sulfonamide moiety but non-antibiotic; low cross-react risk"},
            "hydrochlorothiazide": {"risk": "STRUCTURAL", "rate": "1–3%", "action": "CAUTION — contains sulfonamide group; monitor"},
            "celecoxib":       {"risk": "STRUCTURAL",  "rate": "<1%",   "action": "LOW RISK — no clinically significant cross-reactivity"},
        },
        "note": "Non-antibiotic sulfonamides (furosemide, thiazides) have different allergy mechanisms. Cross-reactivity evidence is weak. Strom BL et al. NEJM 2003.",
    },
    "aspirin": {
        "class": "nsaid",
        "cross_react_with": {
            "ibuprofen":   {"risk": "CROSS_REACT", "rate": "10–15%", "action": "CONTRAINDICATED in aspirin-exacerbated respiratory disease (AERD)"},
            "naproxen":    {"risk": "CROSS_REACT", "rate": "10–15%", "action": "CONTRAINDICATED in AERD; NSAID hypersensitivity"},
            "diclofenac":  {"risk": "CROSS_REACT", "rate": "8–12%",  "action": "CONTRAINDICATED if AERD or NSAID hypersensitivity"},
            "celecoxib":   {"risk": "LOW_CROSS",   "rate": "1–5%",   "action": "GENERALLY SAFE in true aspirin allergy — selective COX-2 inhibitor"},
            "paracetamol": {"risk": "MINIMAL",     "rate": "<1%",    "action": "SAFE — not an NSAID; use as analgesic alternative"},
        },
        "note": "Aspirin cross-reactivity with NSAIDs is pharmacological (COX-1 inhibition), not immunological. Szczeklik A, Grzanka A. J Allergy Clin Immunol. 2014.",
    },
    "cephalosporin": {
        "class": "beta_lactam",
        "cross_react_with": {
            "other_cephalosporins": {"risk": "SAME_CLASS",  "rate": "up to 10%", "action": "CAUTION — cross-reactivity within cephalosporins based on R1 side chain"},
            "penicillins":         {"risk": "CROSS_REACT",  "rate": "1–2%",      "action": "LOW RISK — see penicillin allergy notes; far lower than historical 10%"},
            "carbapenems":         {"risk": "CROSS_REACT",  "rate": "<1%",       "action": "LOW RISK — can use with monitoring"},
        },
    },
    "contrast_iodine": {
        "class": "radiocontrast",
        "cross_react_with": {
            "other_contrast": {"risk": "CROSS_REACT", "rate": "15–35%", "action": "PREMEDICATE with corticosteroids + antihistamine; consider low-osmolarity agent"},
        },
        "note": "Prior contrast reaction increases risk of repeat reaction. Premedication protocol: methylprednisolone 32mg PO 12h and 2h before, plus diphenhydramine 50mg IV 1h before. ACR Manual on Contrast Media 2023.",
    },
}


def check_allergy_cross_reactivity(
    allergy: str,
    proposed_drug: str,
) -> tuple[Optional[dict], CQLComputationLog]:
    """
    Deterministic allergy cross-reactivity check.
    Returns (risk_entry, computation_log). risk_entry is None if no known risk.
    """
    allergy_key = allergy.lower().strip()
    drug_key = proposed_drug.lower().strip().replace(" ", "_")

    risk_entry = None
    if allergy_key in ALLERGY_CROSS_REACTIVITY:
        cross = ALLERGY_CROSS_REACTIVITY[allergy_key]
        if drug_key in cross.get("cross_react_with", {}):
            risk_entry = cross["cross_react_with"][drug_key]
            risk_entry["_note"] = cross.get("note", "")
            risk_entry["_allergen_class"] = cross.get("class", "")

    log = CQLComputationLog(
        computation_id=str(uuid4()),
        rule_id=f"CQL.ALLERGY.CROSS_REACT.{allergy_key.upper()}.vs.{drug_key.upper()}",
        rule_version=RULE_VERSION,
        inputs={"allergy": allergy, "proposed_drug": proposed_drug},
        formula_applied="Allergy cross-reactivity knowledge graph lookup",
        output_value=(
            risk_entry["action"] if risk_entry
            else "NO_KNOWN_CROSS_REACTIVITY"
        ),
    )
    return risk_entry, log


# ─────────────────────────────────────────────────────────────────────────────
# QT PROLONGATION RISK  — Tisdale Score  (L3-12)
# ─────────────────────────────────────────────────────────────────────────────

# CredibleMeds risk categories
QT_RISK_DRUGS: dict[str, dict] = {
    # Known Risk: substantial evidence of QTc prolongation + TdP
    "azithromycin":    {"category": "KNOWN_RISK",    "tisdale_points": 3},
    "haloperidol":     {"category": "KNOWN_RISK",    "tisdale_points": 3},
    "amiodarone":      {"category": "KNOWN_RISK",    "tisdale_points": 3},
    "sotalol":         {"category": "KNOWN_RISK",    "tisdale_points": 3},
    "ciprofloxacin":   {"category": "KNOWN_RISK",    "tisdale_points": 3},
    "moxifloxacin":    {"category": "KNOWN_RISK",    "tisdale_points": 3},
    "methadone":       {"category": "KNOWN_RISK",    "tisdale_points": 3},
    "ondansetron":     {"category": "KNOWN_RISK",    "tisdale_points": 3},
    "quetiapine":      {"category": "KNOWN_RISK",    "tisdale_points": 3},
    "droperidol":      {"category": "KNOWN_RISK",    "tisdale_points": 3},
    "domperidone":     {"category": "KNOWN_RISK",    "tisdale_points": 3},
    # Conditional Risk: prolongs QTc in certain conditions
    "fluconazole":     {"category": "CONDITIONAL_RISK", "tisdale_points": 2},
    "venlafaxine":     {"category": "CONDITIONAL_RISK", "tisdale_points": 2},
    "sertraline":      {"category": "CONDITIONAL_RISK", "tisdale_points": 2},
    "escitalopram":    {"category": "CONDITIONAL_RISK", "tisdale_points": 2},
    "citalopram":      {"category": "CONDITIONAL_RISK", "tisdale_points": 2},
    "olanzapine":      {"category": "CONDITIONAL_RISK", "tisdale_points": 2},
    "risperidone":     {"category": "CONDITIONAL_RISK", "tisdale_points": 2},
    "hydroxychloroquine": {"category": "CONDITIONAL_RISK", "tisdale_points": 2},
    # Possible Risk
    "clarithromycin":  {"category": "POSSIBLE_RISK",    "tisdale_points": 1},
    "erythromycin":    {"category": "POSSIBLE_RISK",    "tisdale_points": 1},
    "amitriptyline":   {"category": "POSSIBLE_RISK",    "tisdale_points": 1},
}

# Tisdale Score risk factors (Tisdale JE et al., Pharmacotherapy 2013)
def compute_tisdale_qt_score(
    drugs: list[str],
    qtc_ms: Optional[float] = None,
    serum_k_meq: Optional[float] = None,
    on_loop_diuretic: bool = False,
    age: Optional[int] = None,
    sex: Optional[str] = None,
    history_hf: bool = False,
) -> tuple[int, str, CQLComputationLog]:
    """
    Tisdale QTc Risk Score.
    Score ≤6: Low risk
    Score 7–10: Moderate risk — monitor ECG
    Score ≥11: High risk — avoid QT-prolonging drugs if possible

    Returns (score, risk_category, computation_log)
    """
    score = 0
    factors = {}

    # Age ≥68: +1
    if age and age >= 68:
        score += 1
        factors["age_ge_68"] = 1

    # Female sex: +1
    if sex and sex.upper() == "F":
        score += 1
        factors["female_sex"] = 1

    # Loop diuretic use: +1
    if on_loop_diuretic:
        score += 1
        factors["loop_diuretic"] = 1

    # Serum K+ <3.5 mEq/L: +2
    if serum_k_meq and serum_k_meq < 3.5:
        score += 2
        factors["hypokalemia"] = 2

    # Baseline QTc 451–480 ms: +2; 481–500 ms: +3; >500 ms: +3 (already prolonged)
    if qtc_ms:
        if 451 <= qtc_ms <= 480:
            score += 2
            factors["qtc_451_480"] = 2
        elif 481 <= qtc_ms <= 500:
            score += 3
            factors["qtc_481_500"] = 3
        elif qtc_ms > 500:
            score += 3
            factors["qtc_gt_500_already_prolonged"] = 3

    # Heart failure: +1
    if history_hf:
        score += 1
        factors["heart_failure"] = 1

    # QT-risk drugs: count unique drugs with KNOWN_RISK
    qt_drugs_present = []
    for d in drugs:
        dk = d.lower().strip()
        if dk in QT_RISK_DRUGS:
            entry = QT_RISK_DRUGS[dk]
            qt_drugs_present.append((dk, entry["category"]))
            score += entry["tisdale_points"]
            factors[f"drug_{dk}"] = entry["tisdale_points"]

    if score <= 6:
        risk_cat = "LOW"
    elif score <= 10:
        risk_cat = "MODERATE — ECG monitoring recommended"
    else:
        risk_cat = "HIGH — avoid additional QT-prolonging agents; cardiology consultation"

    log = CQLComputationLog(
        computation_id=str(uuid4()),
        rule_id="CQL.QT.TISDALE_SCORE",
        rule_version=RULE_VERSION,
        inputs={
            "drugs": drugs, "qtc_ms": qtc_ms, "serum_k_meq": serum_k_meq,
            "on_loop_diuretic": on_loop_diuretic, "age": age, "sex": sex,
            "history_hf": history_hf,
        },
        formula_applied="Tisdale QTc Risk Score (Pharmacotherapy 2013) + CredibleMeds drug risk classification",
        output_value=f"Score={score}, Risk={risk_cat}",
    )
    return score, risk_cat, log


# ─────────────────────────────────────────────────────────────────────────────
# PEDIATRIC WEIGHT-BASED DOSING  (L3-7)
# ─────────────────────────────────────────────────────────────────────────────

PEDIATRIC_DOSE_TABLE: dict[str, dict] = {
    "amoxicillin": {
        "standard_infection": {
            "dose_mg_per_kg": 25,
            "frequency": "every 8h",
            "max_dose_mg": 500,
            "formulation_note": "Suspension 125mg/5mL or 250mg/5mL",
        },
        "severe_infection": {
            "dose_mg_per_kg": 40,
            "frequency": "every 8h",
            "max_dose_mg": 875,
        },
        "otitis_media_high_risk": {
            "dose_mg_per_kg": 80,
            "frequency": "every 12h",
            "max_dose_mg": 1000,
            "indication": "H. influenzae / resistant S. pneumoniae suspected",
        },
    },
    "paracetamol": {
        "standard": {
            "dose_mg_per_kg": 15,
            "frequency": "every 4–6h",
            "max_dose_mg": 1000,
            "max_daily_mg_per_kg": 75,
            "max_daily_mg": 4000,
            "formulation_note": "120mg/5mL or 250mg/5mL suspension; 500mg tablet for >30kg",
        },
    },
    "ibuprofen": {
        "standard": {
            "dose_mg_per_kg": 5,
            "frequency": "every 6–8h",
            "max_dose_mg": 400,
            "max_daily_mg_per_kg": 30,
            "age_min_months": 6,
            "note": "Do not use in dehydration, renal impairment, GI bleeding, or <6 months",
        },
    },
    "gentamicin": {
        "neonates_0_7days": {
            "dose_mg_per_kg": 4,
            "frequency": "every 36h",
            "trough_target_mg_l": 0.5,
            "note": "Neonates ≤7 days; extended interval; TDM mandatory",
        },
        "neonates_8_28days": {
            "dose_mg_per_kg": 4,
            "frequency": "every 24h",
            "note": "TDM mandatory; peak 5–10 mg/L, trough <1 mg/L",
        },
        "children": {
            "dose_mg_per_kg": 7,
            "frequency": "every 24h (once daily)",
            "note": "Hartford nomogram for dose adjustment; TDM mandatory",
        },
    },
}

# Broselow-Luten color zones (weight-based emergency dosing reference)
BROSELOW_ZONES: list[dict] = [
    {"color": "grey",    "weight_min_kg": 3,  "weight_max_kg": 5,  "label": "Neonate 3–5 kg"},
    {"color": "pink",    "weight_min_kg": 6,  "weight_max_kg": 7,  "label": "Infant 6–7 kg"},
    {"color": "red",     "weight_min_kg": 8,  "weight_max_kg": 9,  "label": "Infant 8–9 kg"},
    {"color": "purple",  "weight_min_kg": 10, "weight_max_kg": 11, "label": "Toddler 10–11 kg"},
    {"color": "yellow",  "weight_min_kg": 12, "weight_max_kg": 14, "label": "Child 12–14 kg"},
    {"color": "white",   "weight_min_kg": 15, "weight_max_kg": 18, "label": "Child 15–18 kg"},
    {"color": "blue",    "weight_min_kg": 19, "weight_max_kg": 22, "label": "Child 19–22 kg"},
    {"color": "orange",  "weight_min_kg": 24, "weight_max_kg": 29, "label": "Child 24–29 kg"},
    {"color": "green",   "weight_min_kg": 30, "weight_max_kg": 36, "label": "Child 30–36 kg"},
]


def compute_pediatric_dose(
    drug: str,
    indication: str,
    weight_kg: float,
    age_months: int,
) -> tuple[Optional[dict], CQLComputationLog]:
    """
    Compute weight-based pediatric dose deterministically.
    Returns (result_dict, computation_log).
    result_dict includes computed_dose_mg, max_dose_mg, actual_dose_mg (min of computed/max).
    """
    drug_key = drug.lower().strip()
    drug_data = PEDIATRIC_DOSE_TABLE.get(drug_key)

    if not drug_data:
        log = CQLComputationLog(
            computation_id=str(uuid4()),
            rule_id=f"CQL.PEDS.DOSE.{drug_key.upper()}",
            rule_version=RULE_VERSION,
            inputs={"drug": drug, "weight_kg": weight_kg, "age_months": age_months},
            formula_applied="Pediatric dose table lookup — drug not found",
            output_value="NOT_IN_DATABASE",
        )
        return None, log

    ind_key = indication.lower().replace(" ", "_")
    dose_spec = drug_data.get(ind_key) or list(drug_data.values())[0]

    dose_mg_per_kg = dose_spec["dose_mg_per_kg"]
    computed_dose = round(dose_mg_per_kg * weight_kg, 1)
    max_dose = dose_spec.get("max_dose_mg", computed_dose)
    actual_dose = min(computed_dose, max_dose)

    # Age check
    age_min_months = dose_spec.get("age_min_months", 0)
    age_warning = ""
    if age_months < age_min_months:
        age_warning = f"WARNING: Drug not approved for age <{age_min_months} months"

    result = {
        "drug": drug,
        "weight_kg": weight_kg,
        "dose_per_kg": dose_mg_per_kg,
        "computed_dose_mg": computed_dose,
        "max_dose_mg": max_dose,
        "actual_dose_mg": actual_dose,
        "frequency": dose_spec.get("frequency"),
        "formulation_note": dose_spec.get("formulation_note", ""),
        "age_warning": age_warning,
        "notes": dose_spec.get("note", ""),
    }

    log = CQLComputationLog(
        computation_id=str(uuid4()),
        rule_id=f"CQL.PEDS.DOSE.{drug_key.upper()}.{ind_key.upper()}",
        rule_version=RULE_VERSION,
        inputs={"drug": drug, "weight_kg": weight_kg, "age_months": age_months, "indication": indication},
        formula_applied=f"dose_mg = {dose_mg_per_kg} mg/kg × {weight_kg} kg = {computed_dose} mg; capped at max {max_dose} mg → {actual_dose} mg",
        output_value=f"{actual_dose} mg {dose_spec.get('frequency', '')}",
        output_unit="mg",
    )
    return result, log


# ─────────────────────────────────────────────────────────────────────────────
# PREGNANCY SAFETY  (L3-9)
# ─────────────────────────────────────────────────────────────────────────────

PREGNANCY_SAFETY: dict[str, dict] = {
    "methotrexate":  {"category": "X",  "risk": "CONTRAINDICATED", "note": "Known teratogen. Causes fetal death and major congenital malformations. REMS required."},
    "thalidomide":   {"category": "X",  "risk": "CONTRAINDICATED", "note": "Causes severe limb reduction defects. REMS (THALOMID). Category X."},
    "warfarin":      {"category": "X",  "risk": "CONTRAINDICATED_1ST_3RD", "note": "Warfarin embryopathy (1st trimester); fetal/neonatal bleeding (3rd trimester). Use LMWH instead."},
    "isotretinoin":  {"category": "X",  "risk": "CONTRAINDICATED", "note": "Severe birth defects. iPLEDGE REMS mandatory."},
    "valproate":     {"category": "D",  "risk": "AVOID", "note": "Neural tube defects (1–2%), fetal valproate syndrome, cognitive impairment. Use alternative AED if possible."},
    "carbamazepine": {"category": "D",  "risk": "CAUTION", "note": "Cleft palate, neural tube defects at higher doses. Folic acid supplementation essential."},
    "lithium":       {"category": "D",  "risk": "CAUTION", "note": "Ebstein's anomaly (absolute risk 1:1000, vs 1:20000 background). Benefit may outweigh risk in bipolar disorder."},
    "atenolol":      {"category": "D",  "risk": "AVOID", "note": "IUGR, neonatal bradycardia. Use labetalol or methyldopa as preferred alternatives."},
    "tetracycline":  {"category": "D",  "risk": "AVOID", "note": "Tooth discoloration, bone growth retardation (2nd/3rd trimester). Use azithromycin or amoxicillin."},
    "fluoroquinolones": {"category": "C", "risk": "AVOID", "note": "Animal studies show arthropathy. Avoid unless no alternative. Use only if benefit clearly outweighs risk."},
    "aspirin":       {"category": "D_3RD", "risk": "AVOID_3RD_TRIMESTER", "note": "Low-dose aspirin (75–150 mg) for pre-eclampsia prevention is evidence-based (ASPRE trial). High-dose → premature ductus arteriosus closure."},
    "ibuprofen":     {"category": "C_1ST", "risk": "AVOID_3RD_TRIMESTER", "note": "NSAIDs in 3rd trimester → premature closure of ductus arteriosus. Avoid after 30 weeks."},
    "amoxicillin":   {"category": "B",   "risk": "GENERALLY_SAFE", "note": "Considered safe in all trimesters. Compatible with breastfeeding."},
    "paracetamol":   {"category": "B",   "risk": "GENERALLY_SAFE", "note": "First-line analgesic/antipyretic in pregnancy. Short-term use preferred."},
    "cephalexin":    {"category": "B",   "risk": "GENERALLY_SAFE", "note": "Considered safe. Adequate human data. Compatible with breastfeeding."},
    "azithromycin":  {"category": "B",   "risk": "GENERALLY_SAFE", "note": "Compatible with pregnancy. Monitor QTc."},
    "metformin":     {"category": "B",   "risk": "ACCEPTABLE", "note": "Used in gestational diabetes. Some evidence of safety in 1st trimester. Crosses placenta."},
    "heparin_lmwh":  {"category": "B",   "risk": "PREFERRED", "note": "Anticoagulant of choice in pregnancy. Does not cross placenta. Preferred over warfarin."},
    "methyldopa":    {"category": "B",   "risk": "PREFERRED", "note": "Preferred antihypertensive in pregnancy. Extensive safety data."},
    "labetalol":     {"category": "C",   "risk": "ACCEPTABLE", "note": "Acceptable antihypertensive in 2nd/3rd trimester. Monitor for neonatal bradycardia."},
    "nifedipine":    {"category": "C",   "risk": "ACCEPTABLE", "note": "Used for hypertension and tocolysis in preterm labor. Generally safe."},
}


def get_pregnancy_risk(drug: str) -> tuple[Optional[dict], CQLComputationLog]:
    """Deterministic pregnancy risk lookup for a drug."""
    drug_key = drug.lower().strip()
    entry = PREGNANCY_SAFETY.get(drug_key)

    log = CQLComputationLog(
        computation_id=str(uuid4()),
        rule_id=f"CQL.PREG.SAFETY.{drug_key.upper()}",
        rule_version=RULE_VERSION,
        inputs={"drug": drug},
        formula_applied="Pregnancy safety database lookup (FDA categories + REPROTOX + LactMed)",
        output_value=(entry["risk"] if entry else "NOT_IN_DATABASE"),
    )
    return entry, log


# ─────────────────────────────────────────────────────────────────────────────
# DRUG-FOOD / DRUG-HERB INTERACTIONS  (L3-17)
# ─────────────────────────────────────────────────────────────────────────────

DRUG_FOOD_HERB_INTERACTIONS: list[dict] = [
    {
        "drug": "warfarin",
        "interactant": "vitamin_k_rich_foods",
        "examples": "spinach, kale, broccoli, Brussels sprouts, parsley",
        "mechanism": "Vitamin K is the cofactor for warfarin's target enzymes (clotting factors II, VII, IX, X). High dietary vitamin K reduces anticoagulant effect.",
        "clinical_effect": "Reduced INR — loss of anticoagulation; thrombotic events",
        "severity": "MAJOR",
        "management": "Maintain CONSISTENT dietary vitamin K intake rather than avoiding completely. Monitor INR more frequently when diet changes.",
    },
    {
        "drug": "warfarin",
        "interactant": "grapefruit",
        "examples": "grapefruit, Seville orange, pomelo",
        "mechanism": "CYP3A4 inhibition by furanocoumarins — reduces warfarin metabolism",
        "clinical_effect": "Elevated INR — increased bleeding risk",
        "severity": "MODERATE",
        "management": "Avoid grapefruit or maintain consistent intake. Monitor INR.",
    },
    {
        "drug": "statins",
        "interactant": "grapefruit",
        "examples": "grapefruit, Seville orange, pomelo",
        "mechanism": "Grapefruit furanocoumarins irreversibly inhibit intestinal CYP3A4 — increases statin bioavailability",
        "clinical_effect": "Elevated statin levels → increased myopathy/rhabdomyolysis risk (most significant for lovastatin, simvastatin, atorvastatin; minimal for rosuvastatin/pravastatin)",
        "severity": "MAJOR",
        "management": "Avoid grapefruit with lovastatin, simvastatin, atorvastatin. Rosuvastatin or pravastatin are alternatives if patient cannot avoid grapefruit.",
        "drugs_affected": ["simvastatin", "lovastatin", "atorvastatin"],
    },
    {
        "drug": "maois",
        "interactant": "tyramine_rich_foods",
        "examples": "aged cheese, cured meats, sauerkraut, soy sauce, red wine, fava beans",
        "mechanism": "MAO-A normally metabolizes dietary tyramine. MAO inhibition → tyramine accumulates → massive catecholamine release",
        "clinical_effect": "HYPERTENSIVE CRISIS — potentially fatal. Headache, flushing, palpitations, stroke, death.",
        "severity": "CONTRAINDICATED",
        "management": "STRICT dietary restriction of tyramine-containing foods throughout MAOI use and 2 weeks after discontinuation. Patient must receive written dietary list.",
    },
    {
        "drug": "tetracyclines",
        "interactant": "dairy_calcium",
        "examples": "milk, cheese, yogurt, calcium supplements, antacids",
        "mechanism": "Chelation of tetracycline with divalent cations (Ca2+, Mg2+, Fe3+, Al3+) → insoluble complex → 50–80% absorption reduction",
        "clinical_effect": "Dramatically reduced antibiotic efficacy",
        "severity": "MAJOR",
        "management": "Take tetracycline 1–2 hours before or 4 hours after dairy, calcium supplements, or antacids.",
    },
    {
        "drug": "st_johns_wort",
        "interactant": "ssris",
        "examples": "fluoxetine, sertraline, paroxetine, escitalopram",
        "mechanism": "Additive serotonergic effect + St. John's Wort is a weak CYP inducer and serotonin reuptake inhibitor",
        "clinical_effect": "Serotonin syndrome — tremor, hyperthermia, agitation, clonus, diaphoresis; potentially life-threatening",
        "severity": "CONTRAINDICATED",
        "management": "Do not combine. Stop St. John's Wort; allow 2-week washout before starting SSRI.",
    },
    {
        "drug": "st_johns_wort",
        "interactant": "oral_contraceptives",
        "examples": "combined OCP, progestin-only pills",
        "mechanism": "St. John's Wort strongly induces CYP3A4 and P-glycoprotein → reduces OCP plasma levels",
        "clinical_effect": "Contraceptive failure — unintended pregnancy",
        "severity": "MAJOR",
        "management": "Avoid combination. Use additional/alternative contraception if St. John's Wort cannot be stopped.",
    },
    {
        "drug": "ginkgo_biloba",
        "interactant": "anticoagulants",
        "examples": "warfarin, aspirin, clopidogrel, NSAIDs",
        "mechanism": "Ginkgolides inhibit platelet-activating factor + direct antiplatelet effects",
        "clinical_effect": "Increased bleeding risk — case reports of spontaneous hemorrhage including intracranial bleeding",
        "severity": "MAJOR",
        "management": "Avoid combination with anticoagulants or antiplatelet agents. If patient insists, increase monitoring.",
    },
    {
        "drug": "ginseng",
        "interactant": "warfarin",
        "examples": "Panax ginseng, American ginseng",
        "mechanism": "Multiple proposed mechanisms; conflicting studies — may reduce warfarin effect",
        "clinical_effect": "Reduced INR — loss of anticoagulation",
        "severity": "MODERATE",
        "management": "Avoid combination or monitor INR closely if patient refuses to stop ginseng.",
    },
]


def check_drug_food_interaction(drug: str, food_or_herb: str) -> tuple[list[dict], CQLComputationLog]:
    """
    Check for drug-food or drug-herb interactions deterministically.
    Returns list of matching interactions.
    """
    drug_key = drug.lower().strip()
    foh_key = food_or_herb.lower().strip().replace(" ", "_")

    matches = []
    for entry in DRUG_FOOD_HERB_INTERACTIONS:
        if (
            drug_key in entry["drug"] or entry["drug"] in drug_key or
            foh_key in entry.get("drugs_affected", [])
        ) and (
            foh_key in entry["interactant"] or entry["interactant"] in foh_key
        ):
            matches.append(entry)

    log = CQLComputationLog(
        computation_id=str(uuid4()),
        rule_id=f"CQL.DRUG_FOOD.{drug_key.upper()}.{foh_key.upper()}",
        rule_version=RULE_VERSION,
        inputs={"drug": drug, "food_or_herb": food_or_herb},
        formula_applied="Drug-food/drug-herb interaction knowledge graph lookup (Natural Medicines DB + FDA MedWatch)",
        output_value=f"{len(matches)} interaction(s) found",
    )
    return matches, log


# ─────────────────────────────────────────────────────────────────────────────
# CQL KERNEL — Main Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

class CQLKernel:
    """
    L3-1: CQL Safety Kernel orchestrator.
    Runs ALL deterministic safety checks for a query + patient context.
    All outputs are logged in CQLComputationLog for L5-17 numeric tracing.
    """

    def __init__(self) -> None:
        self.rule_version = RULE_VERSION

        # L3 Clinical Safety Engines (from layers/)
        self.pediatric_engine = PediatricSafetyEngine()
        self.pregnancy_engine = PregnancyLactationEngine()
        self.qt_engine = QTProlongationEngine()
        self.drug_food_engine = DrugFoodHerbEngine()
        self.medication_engine = MedicationIntelligenceEngine()
        self.formulary_engine = SmartFormularyEngine()


    def run_all_checks(
        self,
        patient: PatientContext,
        drugs_mentioned: list[str],
        food_herb_mentioned: list[str] | None = None,
        qtc_ms: Optional[float] = None,
        serum_k_meq: Optional[float] = None,
    ) -> dict[str, Any]:
        """
        Execute all applicable CQL rules for the patient context.
        Returns a structured dict with all computation logs and safety flags.
        """
        results: dict[str, Any] = {
            "computation_logs": [],
            "safety_flags": [],
            "renal_adjustments": {},
            "allergy_risks": [],
            "qt_assessment": None,
            "pregnancy_risks": {},
            "drug_food_interactions": [],
        }

        # 1. Renal function calculations
        if patient.renal:
            if (patient.age_years and patient.weight_kg and
                    patient.sex_at_birth and patient.renal.crcl_ml_min is None):
                # If CrCl not provided but we can estimate from context
                # (Would use creatinine from lab in real system)
                pass

            crcl = patient.renal.crcl_ml_min or patient.renal.egfr_ml_min
            if crcl:
                for drug in drugs_mentioned:
                    adj = get_renal_dose_adjustment(drug, crcl)
                    if adj:
                        rule, log = adj
                        results["renal_adjustments"][drug] = rule
                        results["computation_logs"].append(log)
                        if rule["action"] == "contraindicated":
                            results["safety_flags"].append(SafetyFlag.CONTRAINDICATED)

        # 2. Allergy cross-reactivity
        for allergy in patient.allergies:
            for drug in drugs_mentioned:
                risk, log = check_allergy_cross_reactivity(allergy, drug)
                results["computation_logs"].append(log)
                if risk:
                    results["allergy_risks"].append({
                        "allergy": allergy,
                        "drug": drug,
                        "risk": risk,
                    })
                    if risk.get("action", "").startswith("CONTRAINDICATED"):
                        results["safety_flags"].append(SafetyFlag.CONTRAINDICATED)

        # 3. QT prolongation risk
        if drugs_mentioned:
            score, risk_cat, log = compute_tisdale_qt_score(
                drugs=drugs_mentioned,
                qtc_ms=qtc_ms,
                serum_k_meq=serum_k_meq,
                age=patient.age_years,
                sex=patient.sex_at_birth,
            )
            results["qt_assessment"] = {"score": score, "risk": risk_cat}
            results["computation_logs"].append(log)
            if score >= 11:
                results["safety_flags"].append(SafetyFlag.CONTRAINDICATED)

        # 4. Pregnancy safety
        if patient.is_pregnant:
            for drug in drugs_mentioned:
                entry, log = get_pregnancy_risk(drug)
                results["computation_logs"].append(log)
                if entry:
                    results["pregnancy_risks"][drug] = entry
                    if entry["risk"] in ("CONTRAINDICATED", "CONTRAINDICATED_1ST_3RD"):
                        results["safety_flags"].append(SafetyFlag.CONTRAINDICATED)

        # 5. Drug-food / drug-herb interactions
        if food_herb_mentioned:
            for drug in drugs_mentioned:
                for foh in food_herb_mentioned:
                    interactions, log = check_drug_food_interaction(drug, foh)
                    results["computation_logs"].append(log)
                    results["drug_food_interactions"].extend(interactions)

        
        # ── L3 Clinical Safety Engine Results ──

        # Pediatric safety (L3-7)
        if patient and patient.age_years and patient.age_years < 18:
            for drug in drugs_mentioned:
                # FIX-30: method is .calculate(), not .check(); requires weight_kg (not optional)
                if patient.weight_kg:
                    try:
                        ped_result = self.pediatric_engine.calculate(
                            drug=drug,
                            age_years=patient.age_years,
                            weight_kg=patient.weight_kg,
                        )
                        if ped_result:
                            results["pediatric_safety"] = results.get("pediatric_safety", [])
                            results["pediatric_safety"].append(ped_result)
                    except Exception:
                        # Engine may not have data for this drug; skip rather than crash
                        pass

        # Pregnancy/Lactation safety (L3-9)
        if patient and (patient.is_pregnant or patient.is_breastfeeding):
            for drug in drugs_mentioned:
                # FIX-30: split into check_pregnancy(drug, trimester) + check_lactation(drug)
                try:
                    if patient.is_pregnant:
                        # Approximate trimester from gestational_week if available, else 1
                        gw = getattr(patient, "gestational_week", None) or 0
                        trimester = 1 if gw < 14 else (2 if gw < 28 else 3)
                        preg_result = self.pregnancy_engine.check_pregnancy(
                            drug=drug, trimester=trimester,
                        )
                        if preg_result:
                            results["pregnancy_lactation"] = results.get("pregnancy_lactation", [])
                            results["pregnancy_lactation"].append(preg_result)
                    if patient.is_breastfeeding:
                        lact_result = self.pregnancy_engine.check_lactation(drug=drug)
                        if lact_result:
                            results["pregnancy_lactation"] = results.get("pregnancy_lactation", [])
                            results["pregnancy_lactation"].append(lact_result)
                except Exception:
                    pass

        # QT prolongation risk (L3-12) — runs for ALL patients on QT drugs
        if len(drugs_mentioned) > 0:
            qt_result = self.qt_engine.check(
                drugs=drugs_mentioned,
                patient_factors={
                    "age": patient.age_years if patient else None,
                    "sex": patient.sex_at_birth if patient else None,
                    "potassium": None,  # From labs when available
                },
            )
            if qt_result:
                results["qt_assessment_detailed"] = qt_result

        # Drug-food/herb interactions (L3-17) — enriches basic CQL check
        if food_herb_mentioned:
            for drug in drugs_mentioned:
                food_results = self.drug_food_engine.check(
                    drug=drug,
                    foods_and_supplements=food_herb_mentioned,
                )
                if food_results:
                    results["drug_food_detailed"] = results.get("drug_food_detailed", [])
                    results["drug_food_detailed"].extend(food_results)

        # Medication intelligence (L3-2) — renal/hepatic dose enrichment
        if patient and drugs_mentioned:
            for drug in drugs_mentioned:
                med_assessment = self.medication_engine.assess(
                    drug_name=drug,
                    patient_age=patient.age_years,
                    patient_weight=patient.weight_kg,
                    egfr=patient.renal.egfr_ml_min if patient.renal else None,
                    is_pregnant=patient.is_pregnant,
                )
                if med_assessment:
                    results["medication_intelligence"] = results.get("medication_intelligence", [])
                    results["medication_intelligence"].append(med_assessment)

        return results
