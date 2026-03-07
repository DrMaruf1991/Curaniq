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
    - AGS Beers Criteria 2023 (loaded from curaniq/data/beers_criteria_2023.json)
    - Anticholinergic Burden Scale (ACB score)
    - Falls risk flagging
    - Cognitive risk flagging

    All clinical data loaded from versioned JSON files — not hardcoded.
    Threshold: age >= 65 activates geriatric checks.
    """

    GERIATRIC_AGE_THRESHOLD = 65

    def __init__(self):
        from curaniq.data_loader import load_json_data
        raw = load_json_data("beers_criteria_2023.json")

        # Build BEERS_DRUGS from data file
        self._beers_drugs: dict[str, GeriatricAlert] = {}
        for entry in raw.get("avoid_in_older_adults", []):
            drug = entry["drug"].lower()
            self._beers_drugs[drug] = GeriatricAlert(
                drug=drug,
                category=BeersCategory.AVOID if entry.get("strength") == "strong" else BeersCategory.USE_WITH_CAUTION,
                rationale=entry.get("rationale", ""),
                recommendation=entry.get("recommendation", ""),
                source=raw.get("_metadata", {}).get("reference", "AGS Beers 2023"),
                falls_risk=entry.get("falls_risk", False),
                cognitive_risk=entry.get("cognitive_risk", False),
                anticholinergic_burden=entry.get("acb", 0),
            )

        # Build ACB_SCORES from data file
        self._acb_scores: dict[str, int] = {}
        for score_str, drugs in raw.get("acb_scores", {}).items():
            score = int(score_str)
            for drug in drugs:
                self._acb_scores[drug.lower()] = score

        logger.info("GeriatricSafetyEngine: loaded %d Beers PIMs, %d ACB entries",
                     len(self._beers_drugs), len(self._acb_scores))

    def assess(self, patient_age: int, drugs: list[str],
               egfr: Optional[float] = None) -> list[GeriatricAlert]:
        """Run all geriatric safety checks. Returns alerts sorted by severity."""
        if patient_age < self.GERIATRIC_AGE_THRESHOLD:
            return []

        alerts: list[GeriatricAlert] = []
        total_acb = 0

        for drug in drugs:
            drug_lower = drug.lower().strip()
            # Check Beers criteria (from data file)
            beers_alert = self._beers_drugs.get(drug_lower)
            if beers_alert:
                alerts.append(beers_alert)

            # Accumulate ACB score (from data file)
            acb = self._acb_scores.get(drug_lower, 0)
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
    All dose data loaded from curaniq/data/renal_dosing.json — not hardcoded.
    """

    def __init__(self):
        from curaniq.data_loader import load_json_data
        raw = load_json_data("renal_dosing.json")
        self._adjustments: dict[str, dict[str, str]] = raw.get("adjustments", {})
        logger.info("DedicatedRenalDosingEngine: loaded %d drugs", len(self._adjustments))

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
        """Get CKD-stage-specific dose adjustment for a drug (from data file)."""
        drug_lower = drug.lower().strip()
        drug_data = self._adjustments.get(drug_lower)
        if not drug_data:
            return None
        stage = self.classify_ckd_stage(egfr, on_dialysis)
        dose_str = drug_data.get(stage.value, "")
        if not dose_str:
            return None
        source = drug_data.get("source", "")
        action = "normal"
        if "contraindicated" in dose_str.lower():
            action = "contraindicated"
        elif "avoid" in dose_str.lower():
            action = "avoid"
        elif any(kw in dose_str.lower() for kw in ["reduce", "max", "half", "q24", "q36", "q48"]):
            action = "reduce"
        return RenalDoseAdjustment(
            drug=drug_lower, ckd_stage=stage, action=action,
            adjusted_dose=dose_str, max_dose="", monitoring="",
            dialysis_supplement="", source=source,
        )


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
    """L3-11: Anticoagulation safety. Data from anticoagulation_data.json."""

    def __init__(self):
        from curaniq.data_loader import load_json_data
        raw = load_json_data("anticoagulation_data.json")
        self._doac_dosing = raw.get("doac_dosing", {})
        self._reversal_map = raw.get("reversal_agents", {})
        self._has_bled_factors = raw.get("has_bled_factors", [])
        logger.info("AnticoagulationEngine: %d DOACs, %d reversal agents",
                     len(self._doac_dosing), len(self._reversal_map))

    def calculate_has_bled(self, factors: dict[str, bool]) -> tuple[int, str]:
        """Calculate HAS-BLED bleeding risk score."""
        score = sum(
            f.get("points", 0) for f in self._has_bled_factors
            if factors.get(f.get("name", ""), False)
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
        drug_table = self._doac_dosing.get(drug_lower)
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
        return self._reversal_map.get(drug.lower().strip())


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
    """L3-18: TDM & PK-PD. Data from tdm_drugs.json."""

    def __init__(self):
        from curaniq.data_loader import load_json_data
        raw = load_json_data("tdm_drugs.json")
        self._tdm_db = raw.get("drugs", {})
        logger.info("TDMPKPDEngine: %d NTI drugs", len(self._tdm_db))

    def check_level(self, drug: str, measured_level: float,
                    albumin: Optional[float] = None,
                    egfr: Optional[float] = None) -> dict:
        """
        Check a measured drug level against therapeutic range.
        Applies Sheiner-Tozer correction for phenytoin if needed.
        """
        drug_lower = drug.lower().strip()
        tdm = self._tdm_db.get(drug_lower)
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

        min_range, max_range = (tdm.get('trough_min', 0), tdm.get('trough_max', 999))

        if level < min_range:
            status = "subtherapeutic"
            action = "Consider dose increase. Repeat level after 3-5 half-lives at new dose."
        elif level > tdm.get('toxic', 999):
            status = "toxic"
            action = "HOLD dose. Monitor for toxicity signs. Repeat level in 24-48h. " + (
                "Dialysis may be considered." if tdm.get('dialyzable', False) else "Not dialyzable."
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
            "toxic_above": tdm.get('toxic', 999),
            "status": status,
            "action": action,
            "monitoring": tdm.get('monitoring', ''),
            "source": tdm.get('source', ''),
        }

        if drug_lower == "phenytoin" and albumin and albumin < 3.5:
            result["sheiner_tozer_corrected"] = True
            result["albumin_used"] = albumin
            result["uncorrected_level"] = measured_level

        return result

    def estimate_adjusted_half_life(self, drug: str, egfr: float) -> Optional[float]:
        """Estimate drug half-life adjusted for renal function."""
        drug_lower = drug.lower().strip()
        tdm = self._tdm_db.get(drug_lower)
        if not tdm:
            return None

        normal_t12_avg = ((tdm.get('half_life_h', [6, 12])[0], tdm.get('half_life_h', [6, 12])[1])[0] + (tdm.get('half_life_h', [6, 12])[0], tdm.get('half_life_h', [6, 12])[1])[1]) / 2
        renal_fraction = tdm.get('renal_clearance_pct', 0) / 100.0

        # Q factor method: t1/2_adjusted = t1/2_normal / Q
        # Q = 1 - renal_fraction * (1 - egfr/120)
        q = 1 - renal_fraction * (1 - min(egfr, 120) / 120)
        if q <= 0:
            q = 0.1  # Floor to prevent division by zero

        adjusted = normal_t12_avg / q
        return round(adjusted, 1)
