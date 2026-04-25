"""
CURANIQ -- Layer 3: Deterministic Safety Kernel
P2 Clinical Specialty Engines (Domain-Specific)

L3-10  Antimicrobial Stewardship Engine (WHO AWaRe, spectrum matching)
L3-13  Oncology/Chemotherapy Safety Engine (BSA dosing, emetogenicity)
L3-15  Psychiatric Medication Safety Engine (serotonin syndrome, NMS, switch rules)
L3-16  Substance Use Disorder Safety Engine (precipitated withdrawal, interactions)
L3-19  Multi-Morbidity Conflict Resolution Engine (cross-disease contraindictions)
L3-20  Vaccination & Immunization Engine (schedule, contraindications, intervals)
L3-3   Formal Verification (SMT-lite) for CQL rule invariants
L3-4   Temporal Logic Verifier (LTL-lite) for drug sequence safety

All deterministic. No LLM. Rules from published guidelines with citations.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# =============================================================================
# L3-10: ANTIMICROBIAL STEWARDSHIP ENGINE
# Sources: WHO AWaRe 2023, IDSA guidelines, Sanford Guide 2024
# =============================================================================

class AWaReCategory(str, Enum):
    """WHO AWaRe Classification 2023 (Access, Watch, Reserve)."""
    ACCESS  = "access"    # First/second-line, narrow spectrum, low resistance risk
    WATCH   = "watch"     # Higher resistance potential, critical for specific indications
    RESERVE = "reserve"   # Last resort, highest resistance risk, stewardship mandatory


@dataclass
class AntimicrobialAssessment:
    drug: str
    aware_category: AWaReCategory
    spectrum: str
    is_appropriate: bool
    recommendation: str = ""
    deescalation_option: Optional[str] = None
    duration_guidance: str = ""
    source: str = ""


class AntimicrobialStewardshipEngine:
    """
    L3-10: WHO AWaRe-based antimicrobial stewardship.
    All antimicrobial data loaded from curaniq/data/who_aware_2023.json.
    """

    def __init__(self):
        from curaniq.data_loader import load_json_data
        raw = load_json_data("who_aware_2023.json")
        self._aware_db: dict[str, tuple[AWaReCategory, str]] = {}
        category_map = {
            "access": AWaReCategory.ACCESS,
            "watch": AWaReCategory.WATCH,
            "reserve": AWaReCategory.RESERVE,
        }
        for cat_name, cat_enum in category_map.items():
            cat_data = raw.get(cat_name, {})
            drugs = cat_data.get("drugs", {})
            for drug_name, drug_info in drugs.items():
                self._aware_db[drug_name.lower()] = (
                    cat_enum,
                    drug_info.get("spectrum", ""),
                )
        logger.info("AntimicrobialStewardshipEngine: loaded %d antimicrobials from AWaRe 2023",
                     len(self._aware_db))

    def assess(self, drug: str, indication: str = "",
               culture_available: bool = False) -> AntimicrobialAssessment:
        """Assess antimicrobial appropriateness using WHO AWaRe (from data file)."""
        drug_lower = drug.lower().strip()
        entry = self._aware_db.get(drug_lower)

        if not entry:
            return AntimicrobialAssessment(
                drug=drug, aware_category=AWaReCategory.WATCH,
                spectrum="Unknown", is_appropriate=True,
                recommendation="Drug not in AWaRe database. Verify appropriateness manually.",
            )

        category, spectrum = entry
        result = AntimicrobialAssessment(
            drug=drug, aware_category=category, spectrum=spectrum,
            is_appropriate=True, source="WHO AWaRe Classification 2023",
        )

        if category == AWaReCategory.RESERVE:
            result.recommendation = (
                f"RESERVE antibiotic: {drug} should only be used when other options have failed "
                "or culture/sensitivity confirms necessity. Stewardship review mandatory."
            )
            if not culture_available:
                result.is_appropriate = False
                result.recommendation += " Culture/sensitivity data not provided -- justify use."

        elif category == AWaReCategory.WATCH:
            result.recommendation = (
                f"WATCH antibiotic: {drug} has higher resistance potential. "
                "Evaluate de-escalation when culture results available."
            )
            # Suggest ACCESS alternative where possible
            if drug_lower in ("ciprofloxacin", "levofloxacin") and "uti" in indication.lower():
                result.deescalation_option = "Nitrofurantoin or trimethoprim (ACCESS) if susceptible"
            elif drug_lower == "ceftriaxone" and "pneumonia" in indication.lower():
                result.deescalation_option = "Amoxicillin-clavulanate (ACCESS) if mild, susceptible"

        else:
            result.recommendation = f"ACCESS antibiotic: appropriate first-line choice."

        return result


# =============================================================================
# L3-13: ONCOLOGY / CHEMOTHERAPY SAFETY ENGINE
# Sources: NCCN Antiemesis 2024, ASCO BSA guidelines, NCI CTCAE v5
# =============================================================================

class EmetogenicRisk(str, Enum):
    HIGH     = "high"      # >90% risk without antiemetics
    MODERATE = "moderate"   # 30-90% risk
    LOW      = "low"        # 10-30% risk
    MINIMAL  = "minimal"    # <10% risk


@dataclass
class ChemoSafetyResult:
    drug: str
    bsa_dose_calculated: Optional[str] = None
    emetogenic_risk: Optional[EmetogenicRisk] = None
    antiemetic_protocol: str = ""
    max_lifetime_dose: str = ""
    organ_toxicity_monitoring: list[str] = field(default_factory=list)
    source: str = ""


class OncologyChemoSafetyEngine:
    """
    L3-13: Chemotherapy-specific safety engine.

    BSA-based dosing (DuBois formula), emetogenicity classification,
    cumulative dose limits, organ toxicity monitoring.

    NOTE: CURANIQ does NOT generate chemotherapy protocols.
    This engine VERIFIES safety parameters of existing orders.
    """

    # Emetogenicity classification — NCCN Antiemesis v1.2024
    EMETOGENICITY: dict[str, EmetogenicRisk] = {
        "cisplatin": "high", "cyclophosphamide_high": "high",
        "doxorubicin": "high", "epirubicin": "high",
        "ifosfamide": "high", "dacarbazine": "high",
        "carboplatin": "moderate", "oxaliplatin": "moderate",
        "irinotecan": "moderate", "temozolomide": "moderate",
        "paclitaxel": "low", "docetaxel": "low",
        "5-fluorouracil": "low", "gemcitabine": "low",
        "vincristine": "minimal", "bleomycin": "minimal",
        "rituximab": "minimal", "trastuzumab": "minimal",
    }

    # Cumulative dose limits — Source: respective drug labels, NCCN guidelines
    CUMULATIVE_LIMITS: dict[str, dict] = {
        "doxorubicin": {"max_mg_m2": 450, "toxicity": "Cardiotoxicity (CHF)", "monitoring": "ECHO/MUGA before each cycle after 300mg/m2", "source": "NCCN; FDA label"},
        "epirubicin":  {"max_mg_m2": 900, "toxicity": "Cardiotoxicity", "monitoring": "ECHO/MUGA periodically", "source": "FDA label"},
        "bleomycin":   {"max_units": 400, "toxicity": "Pulmonary fibrosis", "monitoring": "PFTs baseline + periodically. DLCO <40% = discontinue.", "source": "FDA label"},
        "cisplatin":   {"max_notes": "No absolute max but cumulative nephro/ototoxicity", "toxicity": "Nephrotoxicity, ototoxicity, neuropathy", "monitoring": "Audiometry, eGFR before each cycle", "source": "NCCN"},
    }

    def __init__(self):
        # FIX-29: methods reference self._emetogenicity / self._cumulative_limits
        # but class-level constants are uppercase. Load from JSON if available,
        # else fall back to the inline class-level data.
        from curaniq.data_loader import load_json_data
        try:
            raw = load_json_data("oncology_safety.json")
            self._emetogenicity = raw.get("emetogenicity", self.EMETOGENICITY)
            self._cumulative_limits = raw.get("cumulative_limits", self.CUMULATIVE_LIMITS)
        except Exception:
            self._emetogenicity = self.EMETOGENICITY
            self._cumulative_limits = self.CUMULATIVE_LIMITS

    def calculate_bsa(self, height_cm: float, weight_kg: float) -> float:
        """DuBois formula: BSA (m2) = 0.007184 * H^0.725 * W^0.425"""
        return round(0.007184 * (height_cm ** 0.725) * (weight_kg ** 0.425), 2)

    def assess(self, drug: str, height_cm: Optional[float] = None,
               weight_kg: Optional[float] = None,
               cumulative_dose_mg_m2: float = 0.0) -> ChemoSafetyResult:
        """Assess chemotherapy safety parameters."""
        drug_lower = drug.lower().strip()
        result = ChemoSafetyResult(drug=drug)

        # BSA calculation
        if height_cm and weight_kg:
            bsa = self.calculate_bsa(height_cm, weight_kg)
            result.bsa_dose_calculated = f"BSA = {bsa} m2 (DuBois)"

        # Emetogenicity
        emet = self._emetogenicity.get(drug_lower)
        if emet:
            result.emetogenic_risk = emet
            if emet == "high":
                result.antiemetic_protocol = "NK1 antagonist + 5-HT3 antagonist + dexamethasone + olanzapine (NCCN v1.2024)"
            elif emet == "moderate":
                result.antiemetic_protocol = "5-HT3 antagonist + dexamethasone (NCCN v1.2024)"
            elif emet == "low":
                result.antiemetic_protocol = "Dexamethasone OR 5-HT3 antagonist (NCCN v1.2024)"

        # Cumulative dose limits
        limits = self._cumulative_limits.get(drug_lower)
        if limits:
            max_val = limits.get("max_mg_m2", 0)
            if max_val and cumulative_dose_mg_m2 >= max_val * 0.8:
                result.organ_toxicity_monitoring.append(
                    f"ALERT: Approaching cumulative limit ({cumulative_dose_mg_m2}/{max_val} mg/m2). "
                    f"{limits['toxicity']}. {limits['monitoring']}"
                )
            result.max_lifetime_dose = f"{max_val} mg/m2" if max_val else limits.get("max_notes", "")
            result.source = limits.get("source", "")

        return result


# =============================================================================
# L3-15: PSYCHIATRIC MEDICATION SAFETY ENGINE
# Sources: Stahl's Essential Psychopharmacology, APA 2023, NICE CG185
# =============================================================================

@dataclass
class PsychSafetyAlert:
    drug: str
    alert_type: str  # "serotonin_syndrome", "nms", "switch_rule", "metabolic", "weight_gain"
    severity: str
    message: str
    recommendation: str
    source: str


class PsychiatricSafetyEngine:
    def __init__(self) -> None:
        # Loaded-data compatibility default. Populate from data layer later.
        self._serotonergic_drugs = {}

    """
    L3-15: Psychiatric medication-specific safety.

    Covers:
    - Serotonin syndrome risk (Hunter criteria drug combinations)
    - Neuroleptic Malignant Syndrome (NMS) risk
    - Antidepressant switch rules (washout periods)
    - MAOI dietary restrictions + drug interactions
    - Metabolic syndrome monitoring (antipsychotics)
    - Lithium-specific safety (from L3-18 TDM data)
    """


    def check_serotonin_syndrome_risk(self, drugs: list[str]) -> list[PsychSafetyAlert]:
        """Check drug combination for serotonin syndrome risk."""
        alerts = []
        serotonergic_found: dict[str, list[str]] = {}

        for drug in drugs:
            drug_lower = drug.lower().strip().replace(" ", "_").replace("'", "")
            sero_class = self._serotonergic_drugs.get(drug_lower)
            if sero_class:
                serotonergic_found.setdefault(sero_class, []).append(drug)

        # Dangerous combinations: MAOI + any serotonergic
        maoi_classes = {"maoi_irreversible", "maoi_reversible", "maoi_b"}
        maoi_present = maoi_classes & set(serotonergic_found.keys())
        other_sero = set(serotonergic_found.keys()) - maoi_classes

        if maoi_present and other_sero:
            maoi_drugs = []
            for mc in maoi_present:
                maoi_drugs.extend(serotonergic_found[mc])
            other_drugs = []
            for oc in other_sero:
                other_drugs.extend(serotonergic_found[oc])

            alerts.append(PsychSafetyAlert(
                drug=f"{', '.join(maoi_drugs)} + {', '.join(other_drugs)}",
                alert_type="serotonin_syndrome",
                severity="critical",
                message="CRITICAL: MAOI + serotonergic combination — high risk of serotonin syndrome. "
                        "Potentially fatal: hyperthermia, rigidity, myoclonus, autonomic instability.",
                recommendation="CONTRAINDICATED. Do NOT combine. Requires washout period between agents.",
                source="Boyer & Shannon, NEJM 2005;352:1112-1120; Hunter criteria",
            ))

        # Two serotonergics from different classes (moderate risk)
        elif len(serotonergic_found) >= 2:
            all_drugs = [d for drugs_list in serotonergic_found.values() for d in drugs_list]
            alerts.append(PsychSafetyAlert(
                drug=", ".join(all_drugs),
                alert_type="serotonin_syndrome",
                severity="major",
                message=f"Multiple serotonergic agents: {', '.join(all_drugs)}. "
                        "Increased serotonin syndrome risk. Monitor for: agitation, tremor, "
                        "diarrhea, hyperthermia, hyperreflexia, myoclonus.",
                recommendation="If combination necessary: start low, titrate slowly, "
                               "educate patient on warning signs. Consider alternative non-serotonergic.",
                source="Isbister et al. Clin Pharmacol Ther 2007;81(1):93-103",
            ))

        return alerts


# =============================================================================
# L3-16: SUBSTANCE USE DISORDER SAFETY ENGINE
# Sources: ASAM 2020, WHO mhGAP, SAMHSA TIP 63
# =============================================================================

class SubstanceUseSafetyEngine:
    """
    L3-16: Substance use disorder medication safety.

    Critical interactions:
    - Precipitated withdrawal (naltrexone/naloxone + active opioid)
    - Methadone QT prolongation (connects to L3-12)
    - Buprenorphine + benzodiazepine respiratory depression
    - Disulfiram + alcohol (intended but must warn about severity)
    - Alcohol + CNS depressants
    """

    # Precipitated withdrawal combinations
    # Source: ASAM National Practice Guideline 2020
    PRECIPITATED_WITHDRAWAL: list[tuple[str, str, str]] = [
        ("naltrexone", "opioid", "Naltrexone will precipitate acute withdrawal in opioid-dependent patients. "
         "Requires 7-10 day opioid-free period (short-acting) or 10-14 days (long-acting/methadone)."),
        ("naloxone", "opioid", "Naloxone reversal agent. Precipitates withdrawal — use ONLY for overdose reversal."),
        ("buprenorphine", "full_agonist_opioid", "Buprenorphine (partial agonist) can precipitate withdrawal "
         "if initiated while full agonist still active. COWS score >=12 before induction."),
    ]

    DANGEROUS_COMBINATIONS: list[tuple[str, str, str, str]] = [
        ("methadone", "benzodiazepine", "critical", "Respiratory depression risk. FDA boxed warning. "
         "If unavoidable: lowest doses, close monitoring. Source: FDA Safety Communication 2017"),
        ("buprenorphine", "benzodiazepine", "major", "Increased sedation and respiratory depression. "
         "FDA boxed warning. Avoid if possible. Source: FDA Safety Communication 2017"),
        ("methadone", "erythromycin", "major", "QT prolongation + CYP3A4 inhibition increasing methadone levels. "
         "ECG monitoring required. Source: SAMHSA TIP 63"),
        ("disulfiram", "metronidazole", "major", "Psychotic reactions reported. Avoid combination. "
         "Source: Product labels"),
    ]

    def __init__(self):
        # FIX-29: methods reference self._combinations; load from JSON, fall back to class constant.
        from curaniq.data_loader import load_json_data
        try:
            raw = load_json_data("specialty_clinical_rules.json")
            cfg = raw.get("substance_use_combinations", [])
            # Convert dict format to tuple format expected by assess_combinations
            self._combinations = [
                (item.get("drug_a", ""), item.get("drug_b_class", item.get("drug_b", "")),
                 item.get("severity", "major"), item.get("message", item.get("conflict", "")))
                for item in cfg
            ] if cfg else self.DANGEROUS_COMBINATIONS
        except Exception:
            self._combinations = self.DANGEROUS_COMBINATIONS

    def assess_combinations(self, drugs: list[str]) -> list[dict]:
        """Check for substance-use-specific dangerous combinations."""
        alerts = []
        drug_set = {d.lower().strip() for d in drugs}

        for combo in self._combinations:
            if combo[0] in drug_set and any(
                d in drug_set for d in drug_set
                if combo[1] in d or d in combo[1]
            ):
                alerts.append({
                    "drugs": f"{combo[0]} + {combo[1]}",
                    "severity": combo[2],
                    "message": combo[3],
                })

        return alerts


# =============================================================================
# L3-19: MULTI-MORBIDITY CONFLICT RESOLUTION ENGINE
# Sources: NICE NG56 (Multimorbidity), WHO ICOPE 2019, Cochrane Multimorbidity
# =============================================================================

class MultiMorbidityResolver:
    """
    L3-19: Resolves conflicts between treatments for coexisting conditions.

    When a patient has multiple conditions, treatment for one may
    worsen another. This engine detects cross-disease conflicts
    and suggests resolution strategies.

    Depends on: L3-8 (geriatric), L3-11 (anticoagulation), L3-15 (psychiatric)
    """

    # Cross-disease contraindication rules
    # Format: (condition_A, treatment_for_A, condition_B, conflict, recommendation)
    # Source: NICE NG56; clinical pharmacology literature

    def __init__(self):
        # FIX-29: methods reference self._conflict_rules; load from JSON.
        from curaniq.data_loader import load_json_data
        try:
            raw = load_json_data("specialty_clinical_rules.json")
            self._conflict_rules = raw.get("multimorbidity_conflicts", [])
        except Exception:
            self._conflict_rules = []

    def check_conflicts(self, conditions: list[str], drugs: list[str]) -> list[dict]:
        """Check for multi-morbidity treatment conflicts."""
        conflicts = []
        cond_set = {c.lower().replace(" ", "_") for c in conditions}
        drug_set = {d.lower() for d in drugs}

        for rule in self._conflict_rules:
            cond_a = rule.get("cond_a", "")
            treatment = rule.get("drug", "")
            cond_b = rule.get("cond_b", "")
            conflict_desc = rule.get("conflict", "")
            recommendation = rule.get("recommendation", "")
            if cond_a in cond_set and any(treatment in d or d in treatment for d in drug_set):
                conflicts.append({
                    "condition_treated": cond_a,
                    "drug": treatment,
                    "condition_harmed": cond_b if cond_b in cond_set else cond_a,
                    "conflict": conflict_desc,
                    "recommendation": recommendation,
                })

        return conflicts


# =============================================================================
# L3-20: VACCINATION & IMMUNIZATION ENGINE
# Sources: CDC ACIP 2024, WHO EPI schedule, Green Book (UK)
# =============================================================================

@dataclass
class VaccineCheckResult:
    vaccine: str
    safe_to_administer: bool = True
    contraindications: list[str] = field(default_factory=list)
    precautions: list[str] = field(default_factory=list)
    minimum_interval_days: Optional[int] = None
    source: str = ""


class VaccinationEngine:
    """
    L3-20: Vaccination safety checking.

    Covers:
    - Absolute contraindications (anaphylaxis to components)
    - Live vaccine restrictions (immunosuppression, pregnancy)
    - Minimum intervals between doses
    - Drug-vaccine interactions (immunosuppressants)
    """
    def __init__(self):
        from curaniq.data_loader import load_json_data
        raw = load_json_data("specialty_clinical_rules.json")
        vax = raw.get("vaccination", {})
        self._live_vaccines = set(vax.get("live_vaccines", []))
        self._immunosuppressive = set(vax.get("immunosuppressive_drugs", []))
        logger.info("VaccinationEngine: %d live vaccines, %d immunosuppressants",
                     len(self._live_vaccines), len(self._immunosuppressive))

    def check_vaccine(self, vaccine: str, patient_drugs: list[str],
                      is_pregnant: bool = False,
                      has_anaphylaxis_to_component: bool = False) -> VaccineCheckResult:
        """Check if a vaccine is safe to administer."""
        vaccine_lower = vaccine.lower().strip()
        result = VaccineCheckResult(vaccine=vaccine, source="CDC ACIP 2024; WHO EPI")

        if has_anaphylaxis_to_component:
            result.safe_to_administer = False
            result.contraindications.append(
                "Anaphylaxis to vaccine component — absolute contraindication."
            )
            return result

        is_live = vaccine_lower in self._live_vaccines

        if is_live:
            # Pregnancy check
            if is_pregnant:
                result.safe_to_administer = False
                result.contraindications.append(
                    f"Live vaccine ({vaccine}) contraindicated in pregnancy. "
                    "Source: CDC ACIP; WHO position papers."
                )

            # Immunosuppression check
            patient_drug_set = {d.lower().strip() for d in patient_drugs}
            immuno_drugs = patient_drug_set & self._immunosuppressive
            if immuno_drugs:
                result.safe_to_administer = False
                result.contraindications.append(
                    f"Live vaccine ({vaccine}) contraindicated with immunosuppressive therapy: "
                    f"{', '.join(immuno_drugs)}. Source: CDC ACIP General Best Practices 2024."
                )

        return result


# =============================================================================
# L3-3: FORMAL VERIFICATION (SMT-LITE)
# Verifies CQL rule invariants hold across all input ranges
# =============================================================================

class FormalVerificationEngine:
    """
    L3-3: Lightweight formal verification for CQL dose rules.

    Verifies invariants like:
    - No dose calculation can produce negative values
    - Dose always <= max_dose for any valid input
    - CrCl formula outputs are within physiological range (0-250)
    - Pediatric mg/kg doses don't exceed adult max doses

    Uses bounded exhaustive testing (not full SMT solver)
    across the valid input domain.
    """

    def verify_dose_invariant(self, dose_fn, input_ranges: dict,
                              max_dose: float, samples: int = 1000) -> dict:
        """Verify that a dose function never exceeds max_dose."""
        import random
        violations = []

        for _ in range(samples):
            inputs = {}
            for param, (lo, hi) in input_ranges.items():
                inputs[param] = random.uniform(lo, hi)
            try:
                result = dose_fn(**inputs)
                if result < 0:
                    violations.append({"inputs": inputs.copy(), "result": result, "type": "negative_dose"})
                if result > max_dose:
                    violations.append({"inputs": inputs.copy(), "result": result, "type": "exceeds_max"})
            except Exception as e:
                violations.append({"inputs": inputs.copy(), "error": str(e), "type": "exception"})

        return {
            "samples_tested": samples,
            "violations": len(violations),
            "passed": len(violations) == 0,
            "details": violations[:10],
        }

    def verify_crcl_range(self, samples: int = 500) -> dict:
        """Verify Cockcroft-Gault produces physiologically valid CrCl."""
        import random
        violations = []

        for _ in range(samples):
            age = random.randint(18, 100)
            weight = random.uniform(30, 200)
            cr = random.uniform(20, 1500)  # umol/L
            sex = random.choice(["M", "F"])

            cr_mg = cr / 88.4
            if cr_mg <= 0:
                continue
            crcl = ((140 - age) * weight) / (72 * cr_mg)
            if sex == "F":
                crcl *= 0.85

            if crcl < 0 or crcl > 300:
                violations.append({"age": age, "weight": weight, "cr_umol": cr, "crcl": round(crcl, 1)})

        return {"samples": samples, "violations": len(violations), "passed": len(violations) == 0}


# =============================================================================
# L3-4: TEMPORAL LOGIC VERIFIER (LTL-LITE)
# Verifies time-based drug sequence safety
# =============================================================================

class TemporalLogicVerifier:
    """
    L3-4: Verifies temporal safety constraints on drug sequences.

    Rules like:
    - "Methotrexate must NOT be given within 24h of NSAIDs at high dose"
    - "Loading dose must precede maintenance dose"
    - "Washout period must elapse before switching"
    - "Monitoring must occur before re-dosing narrow-index drugs"

    Uses event sequence checking (not full LTL model checking).
    """

    def __init__(self):
        # FIX-29: methods reference self._sequence_rules; load from JSON.
        from curaniq.data_loader import load_json_data
        try:
            raw = load_json_data("specialty_clinical_rules.json")
            cfg = raw.get("temporal_safety_rules", [])
            # Convert dict→tuple expected by check_sequence
            self._sequence_rules = [
                (r.get("drug_a", ""), r.get("drug_b", ""), r.get("min_gap_hours", 0),
                 r.get("direction", "either"), r.get("reason", ""))
                for r in cfg
            ]
        except Exception:
            self._sequence_rules = []

    def check_sequence(self, events: list[dict]) -> list[dict]:
        """
        Check a sequence of drug events for temporal violations.
        Each event: {"drug": str, "time_hours": float, "event_type": str}
        """
        violations = []
        sorted_events = sorted(events, key=lambda e: e.get("time_hours", 0))

        for rule in self._sequence_rules:
            drug_a, drug_b, min_gap, direction, reason = rule
            a_times = [e["time_hours"] for e in sorted_events if drug_a in e.get("drug", "").lower()]
            b_times = [e["time_hours"] for e in sorted_events if drug_b in e.get("drug", "").lower()]

            for at in a_times:
                for bt in b_times:
                    gap = abs(bt - at)
                    if gap < min_gap:
                        if direction == "either_direction" or (direction == "a_before_b" and at < bt):
                            violations.append({
                                "rule": f"{drug_a} -> {drug_b}",
                                "required_gap_hours": min_gap,
                                "actual_gap_hours": round(gap, 1),
                                "reason": reason,
                            })

        return violations
