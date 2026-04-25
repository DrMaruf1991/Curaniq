"""
CURANIQ — Medical Evidence Operating System
Layer 3: Deterministic Safety Kernel

L3-7  Pediatric Safety Engine (mg/kg weight-based dosing)
L3-9  Pregnancy & Lactation Engine (LactMed integration)
L3-12 QT Prolongation Risk Engine (Tisdale score)
L3-17 Drug-Food & Herb Interaction Engine
"""
from __future__ import annotations
import logging, math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# L3-7: PEDIATRIC SAFETY ENGINE
# Architecture: 'Weight-based dosing (mg/kg) with age-banded upper limits.
# Neonatal/infant/child/adolescent strata. BNFc alignment.'
# ─────────────────────────────────────────────────────────────────────────────

class PediatricAgeBand(str, Enum):
    NEONATE     = "neonate"       # 0-28 days
    INFANT      = "infant"        # 1-12 months
    TODDLER     = "toddler"       # 1-5 years
    CHILD       = "child"         # 6-11 years
    ADOLESCENT  = "adolescent"    # 12-17 years

@dataclass
class PediatricDoseRule:
    drug: str
    age_bands: list[PediatricAgeBand]
    dose_mg_kg: float             # mg per kg body weight per dose
    frequency: str                # e.g., "every 6-8 hours"
    max_dose_mg: float            # Absolute maximum dose per dose (mg)
    max_daily_dose_mg: float      # Absolute maximum daily dose (mg)
    route: str                    # "oral" | "iv" | "im"
    indication: str
    special_notes: Optional[str]
    source: str

@dataclass
class PediatricDoseResult:
    drug: str
    age_years: float
    weight_kg: float
    age_band: PediatricAgeBand
    calculated_dose_mg: float
    capped_dose_mg: float        # After applying max dose cap
    daily_dose_mg: float
    was_capped: bool
    frequency: str
    route: str
    safe: bool
    safety_message: str
    monitoring: list[str]


PEDIATRIC_DOSE_RULES: list[PediatricDoseRule] = [
    # Amoxicillin — most common pediatric antibiotic
    PediatricDoseRule("amoxicillin", [PediatricAgeBand.INFANT, PediatricAgeBand.TODDLER, PediatricAgeBand.CHILD], 25, "every 8 hours", 500, 1500, "oral", "mild-moderate infection", "Double dose (45-80 mg/kg/day) for resistant pneumococcus, otitis media", "BNFc / NICE / AAP"),
    PediatricDoseRule("amoxicillin", [PediatricAgeBand.ADOLESCENT], 500, "every 8 hours", 500, 1500, "oral", "mild-moderate infection", "Fixed adult dose in adolescents >40kg", "BNFc"),
    # Paracetamol
    PediatricDoseRule("paracetamol", [PediatricAgeBand.NEONATE], 10, "every 8-12 hours", 30, 60, "oral", "analgesia/antipyresis", "Neonates: lower dose due to immature glucuronidation. Max 30mg/kg/day", "BNFc / Neonatal Formulary"),
    PediatricDoseRule("paracetamol", [PediatricAgeBand.INFANT, PediatricAgeBand.TODDLER, PediatricAgeBand.CHILD], 15, "every 4-6 hours", 1000, 4000, "oral", "analgesia/antipyresis", "Max 5 doses in 24h. Max single dose 1g. Max daily 4g", "BNFc"),
    PediatricDoseRule("paracetamol", [PediatricAgeBand.ADOLESCENT], 15, "every 4-6 hours", 1000, 4000, "oral", "analgesia/antipyresis", "As adult: 500mg-1g per dose", "BNFc"),
    # Ibuprofen
    PediatricDoseRule("ibuprofen", [PediatricAgeBand.TODDLER, PediatricAgeBand.CHILD], 5, "every 6-8 hours", 400, 1200, "oral", "analgesia/antipyresis/anti-inflammatory", "Not for neonates/infants <3 months. Avoid in dehydrated children", "BNFc"),
    PediatricDoseRule("ibuprofen", [PediatricAgeBand.ADOLESCENT], 10, "every 6-8 hours", 400, 1200, "oral", "analgesia/anti-inflammatory", "As adult dosing when weight >30kg", "BNFc"),
    # Amoxicillin-clavulanate
    PediatricDoseRule("co-amoxiclav", [PediatricAgeBand.TODDLER, PediatricAgeBand.CHILD], 25, "every 8 hours", 500, 1500, "oral", "moderate infections", "Dose based on amoxicillin component. BNFc 25/6.25mg/5mL suspension", "BNFc"),
    # Salbutamol nebuliser
    PediatricDoseRule("salbutamol", [PediatricAgeBand.INFANT, PediatricAgeBand.TODDLER], 2.5, "every 4-6 hours PRN", 2.5, 10, "nebulised", "acute bronchospasm", "Fixed dose 2.5mg for <5 years. Via spacer preferred", "BNFc / NICE CG101"),
    PediatricDoseRule("salbutamol", [PediatricAgeBand.CHILD, PediatricAgeBand.ADOLESCENT], 5, "every 4-6 hours PRN", 5, 20, "nebulised", "acute bronchospasm", "5mg nebulised or 100mcg via spacer 4-10 puffs for mild-moderate", "BNFc"),
    # Trimethoprim — UTI prophylaxis
    PediatricDoseRule("trimethoprim", [PediatricAgeBand.INFANT, PediatricAgeBand.TODDLER, PediatricAgeBand.CHILD], 2, "once nightly (prophylaxis)", 100, 100, "oral", "UTI prophylaxis", "Prophylactic dose: 1-2mg/kg once nightly. Max 100mg", "BNFc / NICE CG54"),
    # Prednisolone — asthma attack
    PediatricDoseRule("prednisolone", [PediatricAgeBand.TODDLER, PediatricAgeBand.CHILD], 1, "once daily x5 days", 40, 40, "oral", "acute asthma", "Max 40mg/day. Younger children 1-2mg/kg/day; adolescents up to 40mg/day", "BNFc / NICE CG101"),
    # Phenobarbital — neonatal seizures
    PediatricDoseRule("phenobarbital", [PediatricAgeBand.NEONATE], 20, "loading dose then 3-4mg/kg/day maintenance", 40, 20, "iv", "neonatal seizures", "Loading: 20mg/kg IV over 20min. Maintenance 3-4mg/kg/day. NICU only", "BNFc / Neonatal Formulary"),
    # Metoclopramide — AVOID in children <1 year
    PediatricDoseRule("metoclopramide", [PediatricAgeBand.CHILD], 0.1, "every 8 hours", 10, 30, "oral", "nausea/vomiting", "AVOID <1 year. AVOID in neurological disease. Risk of tardive dyskinesia. Only for chemotherapy/post-operative nausea", "MHRA / BNFc"),
    # Gentamicin — neonatal sepsis
    PediatricDoseRule("gentamicin", [PediatricAgeBand.NEONATE], 5, "every 24-36h (gestational age dependent)", 5, 5, "iv", "serious gram-negative infection", "Dose interval depends on gestational age and postnatal age — use BNFc neonatal dosing table. TDM mandatory", "BNFc / Neonatal Formulary"),
]


def get_age_band(age_years: float) -> PediatricAgeBand:
    if age_years < 0.077:  # <28 days
        return PediatricAgeBand.NEONATE
    elif age_years < 1:
        return PediatricAgeBand.INFANT
    elif age_years < 6:
        return PediatricAgeBand.TODDLER
    elif age_years < 12:
        return PediatricAgeBand.CHILD
    else:
        return PediatricAgeBand.ADOLESCENT


class PediatricSafetyEngine:
    """
    L3-7: Deterministic weight-based pediatric dosing engine.
    Architecture: 'Weight-based (mg/kg) with age-banded upper limits.
    Neonatal/infant/child/adolescent strata. BNFc alignment.'
    """

    def calculate(
        self,
        drug: str,
        age_years: float,
        weight_kg: float,
        indication: Optional[str] = None,
    ) -> PediatricDoseResult:
        age_band = get_age_band(age_years)
        drug_lower = drug.lower().strip()

        applicable = [
            r for r in PEDIATRIC_DOSE_RULES
            if drug_lower in r.drug.lower() or r.drug.lower() in drug_lower
            and age_band in r.age_bands
        ]

        if not applicable:
            return PediatricDoseResult(
                drug=drug, age_years=age_years, weight_kg=weight_kg, age_band=age_band,
                calculated_dose_mg=0, capped_dose_mg=0, daily_dose_mg=0,
                was_capped=False, frequency="consult BNFc", route="",
                safe=False,
                safety_message=f"⚠️ No pediatric dose data for {drug} in {age_band.value}. Consult BNFc/local pharmacy.",
                monitoring=["Specialist/pharmacy review required"],
            )

        rule = applicable[0]
        calculated = rule.dose_mg_kg * weight_kg
        capped = min(calculated, rule.max_dose_mg)
        was_capped = calculated > rule.max_dose_mg

        # Estimate daily dose
        freq_doses_per_day = {"every 4-6 hours": 4, "every 6-8 hours": 3, "every 8 hours": 3,
                              "every 8-12 hours": 2.5, "once nightly (prophylaxis)": 1,
                              "once daily x5 days": 1, "every 24-36h": 1}.get(rule.frequency, 3)
        daily = min(capped * freq_doses_per_day, rule.max_daily_dose_mg)

        safe = capped > 0

        # Special safety checks
        monitoring = []
        message_parts = [f"✅ {drug} pediatric dose ({age_band.value}, {weight_kg:.1f}kg, {age_years:.1f}yr):"]
        message_parts.append(f"   Dose: {capped:.1f}mg {rule.frequency} ({rule.route})")
        if was_capped:
            message_parts.append(f"   ⚠️ Weight-based dose ({calculated:.1f}mg) capped at adult maximum ({rule.max_dose_mg}mg)")
        if rule.special_notes:
            message_parts.append(f"   Note: {rule.special_notes}")

        # Special age checks
        if age_band == PediatricAgeBand.NEONATE:
            message_parts.append("   ⚠️ NEONATE: Use neonatal-specific dosing. NICU/neonatal team review.")
            monitoring.append("Neonatal team review mandatory")
        if drug_lower in ("metoclopramide",) and age_band in (PediatricAgeBand.NEONATE, PediatricAgeBand.INFANT):
            safe = False
            message_parts.append("   🚫 CONTRAINDICATED in <1 year — risk of tardive dyskinesia")
        monitoring.append(f"Source: {rule.source}")

        return PediatricDoseResult(
            drug=drug, age_years=age_years, weight_kg=weight_kg, age_band=age_band,
            calculated_dose_mg=round(calculated, 1),
            capped_dose_mg=round(capped, 1),
            daily_dose_mg=round(daily, 1),
            was_capped=was_capped,
            frequency=rule.frequency, route=rule.route,
            safe=safe,
            safety_message="\n".join(message_parts),
            monitoring=monitoring,
        )


# ─────────────────────────────────────────────────────────────────────────────
# L3-9: PREGNANCY & LACTATION ENGINE
# Architecture: 'LactMed integration (L1-10). Teratogenicity data.
# FDA pregnancy categories + newer PLLR labelling.'
# ─────────────────────────────────────────────────────────────────────────────

class PregnancyCategoryFDA(str, Enum):
    """FDA pregnancy risk categories (legacy — still clinically used)."""
    A = "A"   # Adequate studies show no risk
    B = "B"   # Animal studies no risk; no adequate human studies
    C = "C"   # Animal studies show adverse effects; no adequate human studies
    D = "D"   # Evidence of human fetal risk; benefit may outweigh risk
    X = "X"   # Contraindicated — risks clearly outweigh benefits
    N = "N"   # Not classified

class LactationRiskLevel(str, Enum):
    LOW      = "low"       # Compatible with breastfeeding
    MODERATE = "moderate"  # Use with caution — monitor infant
    HIGH     = "high"      # Avoid if possible — significant infant exposure
    CONTRAINDICATED = "contraindicated"  # Do not use whilst breastfeeding

@dataclass
class PregnancyDrugData:
    drug: str
    fda_category: PregnancyCategoryFDA
    first_trimester_risk: str
    second_trimester_risk: str
    third_trimester_risk: str
    lactation_risk: LactationRiskLevel
    lactation_note: str
    safer_alternative: Optional[str]
    contraindicated_in_pregnancy: bool
    source: str

PREGNANCY_DATA: list[PregnancyDrugData] = [
    PregnancyDrugData("warfarin", PregnancyCategoryFDA.X, "Teratogenic: warfarin embryopathy (nasal hypoplasia, stippled epiphyses)", "Fetal CNS anomalies risk", "Risk of fetal/neonatal bleeding", LactationRiskLevel.LOW, "Minimal passage into breast milk — compatible", "Low molecular weight heparin (LMWH)", True, "MHRA / BNF / NICE NG133"),
    PregnancyDrugData("methotrexate", PregnancyCategoryFDA.X, "CONTRAINDICATED: folic acid antagonist, severe teratogen — neural tube defects, skeletal abnormalities", "Fetal toxicity", "Not recommended", LactationRiskLevel.CONTRAINDICATED, "Excreted in breast milk — avoid", None, True, "MHRA / FDA / NICE"),
    PregnancyDrugData("valproate", PregnancyCategoryFDA.D, "Major teratogen: neural tube defects (1-2%), cardiac defects, cleft palate, limb defects. UK MHRA Valproate Pregnancy Prevention Programme mandatory", "Developmental neurotoxicity — IQ reduction of 6-9 points at 3 years", "Neonatal withdrawal — jitteriness, feeding difficulties", LactationRiskLevel.LOW, "Low levels in breast milk — probably compatible but monitor", "Lamotrigine or levetiracetam (epilepsy). Lithium with monitoring (bipolar)", True, "MHRA Valproate Pregnancy Prevention Programme 2018 / EMA"),
    PregnancyDrugData("isotretinoin", PregnancyCategoryFDA.X, "CONTRAINDICATED: severe teratogen — CNS, cardiovascular, craniofacial anomalies. Pregnancy Prevention Programme mandatory in UK (MHRA)", "Teratogenic throughout", "Teratogenic", LactationRiskLevel.CONTRAINDICATED, "Avoid — lipophilic, likely excreted", None, True, "MHRA iPLEDGE-equivalent UK programme"),
    PregnancyDrugData("acei", PregnancyCategoryFDA.D, "First trimester: possible cardiac defects (controversial)", "CONTRAINDICATED in 2nd trimester: fetal renal failure, oligohydramnios, limb contractures, lung hypoplasia", "CONTRAINDICATED in 3rd trimester: neonatal renal failure, hypotension, oliguria", LactationRiskLevel.LOW, "Most compatible — captopril/enalapril preferred", "Nifedipine or labetalol for hypertension in pregnancy", True, "MHRA / NICE NG133 / BNF"),
    PregnancyDrugData("statins", PregnancyCategoryFDA.X, "CONTRAINDICATED: suppress cholesterol synthesis required for fetal development. Discontinue 1 month before conception", "Contraindicated", "Contraindicated", LactationRiskLevel.CONTRAINDICATED, "Avoid — theoretical risk of impaired infant cholesterol synthesis", None, True, "MHRA / FDA"),
    PregnancyDrugData("aspirin", PregnancyCategoryFDA.C, "Low-dose (75-150mg) probably safe; avoid high doses", "Low-dose 75mg recommended for pre-eclampsia prevention (NICE NG133)", "AVOID high doses: premature ductus arteriosus closure. Low-dose: stop at 36 weeks", LactationRiskLevel.MODERATE, "Low-dose: small amount in breast milk — monitor infant for bruising", None, False, "NICE NG133 / BNF"),
    PregnancyDrugData("ibuprofen", PregnancyCategoryFDA.C, "Avoid if possible — potential early pregnancy loss (controversial)", "Compatible if essential for short duration", "CONTRAINDICATED after 20 weeks: fetal renal toxicity, oligohydramnios, premature ductus arteriosus closure", LactationRiskLevel.LOW, "Small amounts in breast milk — compatible with breastfeeding", "Paracetamol preferred for pain in pregnancy", True, "FDA 2020 warning / NICE / BNF"),
    PregnancyDrugData("paracetamol", PregnancyCategoryFDA.B, "Considered safe at recommended doses", "Compatible", "Compatible at standard doses", LactationRiskLevel.LOW, "Compatible with breastfeeding — preferred analgesic/antipyretic", None, False, "NICE / BNF / WHO"),
    PregnancyDrugData("amoxicillin", PregnancyCategoryFDA.B, "Compatible — widely used in pregnancy", "Compatible", "Compatible", LactationRiskLevel.LOW, "Small amounts in breast milk — compatible. Watch for infant sensitization", None, False, "BNF / NICE"),
    PregnancyDrugData("fluconazole", PregnancyCategoryFDA.D, "Single dose (150mg) for vaginal candidiasis — avoid; case reports of cardiac defects with chronic high dose", "Prolonged high dose: cardiac septal defects reported", "Avoid prolonged use", LactationRiskLevel.MODERATE, "Significant breast milk excretion — avoid if possible", "Topical clotrimazole (safe in pregnancy)", False, "FDA / MHRA / BNF"),
    PregnancyDrugData("lithium", PregnancyCategoryFDA.D, "Ebstein's anomaly risk (smaller than previously thought but real). Fetal echocardiography at 20 weeks", "Monitor lithium levels (increase in second trimester)", "Neonatal lithium toxicity: 'floppy infant', cardiac monitoring required", LactationRiskLevel.HIGH, "Significant milk excretion — 50% maternal level. Neonatal toxicity reported. Usually avoid", "Quetiapine or lamotrigine if possible (less data)", False, "NICE / BNF / SIGN"),
    PregnancyDrugData("ssri", PregnancyCategoryFDA.C, "Paroxetine: cardiac defects (FDA warning). Other SSRIs: generally preferred if antidepressant required", "Untreated depression also carries fetal risk — individualise decision", "Neonatal adaptation syndrome: jitteriness, feeding difficulties, respiratory distress (usually transient)", LactationRiskLevel.LOW, "Sertraline preferred — lowest milk transfer. Paroxetine second. Fluoxetine: avoid (long half-life)", None, False, "NICE CG45 / BNF / MHRA"),
    PregnancyDrugData("metformin", PregnancyCategoryFDA.B, "Compatible — increasingly used in gestational diabetes", "Licensed for gestational diabetes in UK", "Compatible — no teratogenicity identified", LactationRiskLevel.LOW, "Small amounts in breast milk — generally considered compatible", None, False, "NICE NG3 / BNF"),
    PregnancyDrugData("labetalol", PregnancyCategoryFDA.C, "Avoid first trimester if possible — some fetal growth restriction risk", "First-line for hypertension in pregnancy in UK", "Neonatal bradycardia and hypoglycaemia: monitor for 24-48h", LactationRiskLevel.LOW, "Small amounts in breast milk — compatible", None, False, "NICE NG133"),
    PregnancyDrugData("methyldopa", PregnancyCategoryFDA.B, "Safest evidence base — decades of use", "First-line in many guidelines for hypertension in pregnancy", "Compatible", LactationRiskLevel.LOW, "Compatible with breastfeeding", None, False, "NICE NG133 / WHO"),
    PregnancyDrugData("corticosteroids", PregnancyCategoryFDA.C, "Single course betamethasone 24-34 weeks: improves fetal lung maturity (standard of care)", "Chronic high-dose systemic: oral cleft first trimester; fetal growth restriction", "Single course for fetal lung maturity: well-established benefit", LactationRiskLevel.LOW, "Prednisolone: compatible. Peak levels 2h post-dose — feed before taking", None, False, "NICE / RCOG / WHO"),
]


class PregnancyLactationEngine:
    """
    L3-9: Deterministic pregnancy and lactation safety engine.
    Integrates LactMed data + FDA PLLR labelling + UK MHRA guidance.
    """

    def check_pregnancy(self, drug: str, trimester: int = 1) -> dict:
        drug_lower = drug.lower()
        for entry in PREGNANCY_DATA:
            if entry.drug.lower() in drug_lower or drug_lower in entry.drug.lower():
                if trimester == 1:
                    risk_text = entry.first_trimester_risk
                elif trimester == 2:
                    risk_text = entry.second_trimester_risk
                else:
                    risk_text = entry.third_trimester_risk

                return {
                    "drug": drug,
                    "fda_category": entry.fda_category.value,
                    "trimester": trimester,
                    "risk": risk_text,
                    "contraindicated": entry.contraindicated_in_pregnancy,
                    "safer_alternative": entry.safer_alternative,
                    "source": entry.source,
                    "safety_message": self._format_pregnancy_message(entry, trimester, risk_text),
                }
        return {
            "drug": drug, "fda_category": "unknown", "trimester": trimester,
            "risk": "No specific data available — consult specialist",
            "contraindicated": False, "safer_alternative": None, "source": "Consult BNF/specialist",
            "safety_message": f"ℹ️ {drug}: No specific pregnancy data in CURANIQ database. Consult specialist/BNF/product SPC.",
        }

    def check_lactation(self, drug: str) -> dict:
        drug_lower = drug.lower()
        for entry in PREGNANCY_DATA:
            if entry.drug.lower() in drug_lower or drug_lower in entry.drug.lower():
                icon = {"low": "✅", "moderate": "⚠️", "high": "⛔", "contraindicated": "🚫"}[entry.lactation_risk.value]
                return {
                    "drug": drug,
                    "risk_level": entry.lactation_risk.value,
                    "note": entry.lactation_note,
                    "safer_alternative": entry.safer_alternative,
                    "source": entry.source,
                    "safety_message": f"{icon} LACTATION ({entry.lactation_risk.value.upper()}): {entry.lactation_note}",
                }
        return {
            "drug": drug, "risk_level": "unknown",
            "note": "No specific lactation data — consult LactMed/pharmacist",
            "safer_alternative": None, "source": "LactMed (https://www.ncbi.nlm.nih.gov/books/NBK501922/)",
            "safety_message": f"ℹ️ {drug}: Check LactMed database for lactation data.",
        }

    def _format_pregnancy_message(self, entry: PregnancyDrugData, trimester: int, risk_text: str) -> str:
        if entry.contraindicated_in_pregnancy:
            lines = [f"🚫 CONTRAINDICATED IN PREGNANCY: {entry.drug}"]
        else:
            lines = [f"⚠️ PREGNANCY RISK — {entry.drug} (FDA Category {entry.fda_category.value}):"]
        lines.append(f"   Trimester {trimester}: {risk_text}")
        if entry.safer_alternative:
            lines.append(f"   Safer alternative: {entry.safer_alternative}")
        lines.append(f"   Source: {entry.source}")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# L3-12: QT PROLONGATION RISK ENGINE
# Architecture: 'Drug-induced QTc prolongation kills. Tisdale score.
# CredibleMeds risk categories. Torsades de Pointes (TdP) prevention.'
# ─────────────────────────────────────────────────────────────────────────────

class CredibleMedsRisk(str, Enum):
    KNOWN        = "known"          # Known risk of TdP
    CONDITIONAL  = "conditional"    # Risk under certain conditions
    POSSIBLE     = "possible"       # Possible risk
    SPECIAL      = "special"        # Special circumstances
    LOW          = "low"            # Low risk
    UNKNOWN      = "unknown"

# CredibleMeds QT risk classification (www.crediblemeds.org)
QT_RISK_DRUGS: dict[str, CredibleMedsRisk] = {
    # KNOWN TdP Risk
    "amiodarone": CredibleMedsRisk.KNOWN,
    "sotalol": CredibleMedsRisk.KNOWN,
    "dofetilide": CredibleMedsRisk.KNOWN,
    "ibutilide": CredibleMedsRisk.KNOWN,
    "quinidine": CredibleMedsRisk.KNOWN,
    "halofantrine": CredibleMedsRisk.KNOWN,
    "cisapride": CredibleMedsRisk.KNOWN,
    "droperidol": CredibleMedsRisk.KNOWN,
    "thioridazine": CredibleMedsRisk.KNOWN,
    "pimozide": CredibleMedsRisk.KNOWN,
    "erythromycin": CredibleMedsRisk.KNOWN,
    "clarithromycin": CredibleMedsRisk.KNOWN,
    "azithromycin": CredibleMedsRisk.KNOWN,
    "moxifloxacin": CredibleMedsRisk.KNOWN,
    "haloperidol": CredibleMedsRisk.KNOWN,
    "methadone": CredibleMedsRisk.KNOWN,
    "ondansetron": CredibleMedsRisk.CONDITIONAL,
    "ciprofloxacin": CredibleMedsRisk.CONDITIONAL,
    "fluconazole": CredibleMedsRisk.CONDITIONAL,
    "hydroxychloroquine": CredibleMedsRisk.KNOWN,
    "chloroquine": CredibleMedsRisk.KNOWN,
    "quetiapine": CredibleMedsRisk.CONDITIONAL,
    "risperidone": CredibleMedsRisk.CONDITIONAL,
    "olanzapine": CredibleMedsRisk.CONDITIONAL,
    "citalopram": CredibleMedsRisk.CONDITIONAL,
    "escitalopram": CredibleMedsRisk.CONDITIONAL,
    "amitriptyline": CredibleMedsRisk.CONDITIONAL,
    "lithium": CredibleMedsRisk.CONDITIONAL,
    "metoclopramide": CredibleMedsRisk.POSSIBLE,
    "domperidone": CredibleMedsRisk.KNOWN,
    "tamoxifen": CredibleMedsRisk.CONDITIONAL,
    "venlafaxine": CredibleMedsRisk.CONDITIONAL,
    "vancomycin": CredibleMedsRisk.POSSIBLE,
}

# Tisdale Risk Score factors (Tisdale JE, et al. Pharmacotherapy 2013)
# Score >7 = high risk of drug-induced QTc prolongation
TISDALE_RISK_FACTORS = {
    "age_>68_years": 1,
    "female_sex": 1,
    "loop_diuretic": 1,
    "serum_k_<3.5": 2,
    "admission_qtc_>450ms": 2,
    "acute_mi": 2,
    "one_qt_prolonging_drug": 3,
    "two_or_more_qt_drugs": 6,
    "serum_k_<3.0": 6,
    "sepsis": 3,
    "heart_failure": 3,
    "hepatic_failure": 1,
}


@dataclass
class QTRiskAssessment:
    drugs: list[str]
    qt_risk_drugs: list[tuple[str, CredibleMedsRisk]]
    tisdale_score: int
    tisdale_risk_level: str    # "low" (<7), "moderate" (7-11), "high" (>11)
    concurrent_qt_count: int
    is_high_risk: bool
    safety_message: str
    recommendations: list[str]


class QTProlongationEngine:
    """
    L3-12: Deterministic QT prolongation risk scoring.
    Uses CredibleMeds risk categories + Tisdale score.
    Combination of ≥2 QT-prolonging drugs = high risk regardless of individual scores.
    """

    def assess(
        self,
        drugs: list[str],
        qtc_ms: Optional[float] = None,
        age_years: Optional[float] = None,
        is_female: bool = False,
        serum_k: Optional[float] = None,
        has_heart_failure: bool = False,
        has_hepatic_failure: bool = False,
        has_acute_mi: bool = False,
        on_loop_diuretic: bool = False,
        has_sepsis: bool = False,
    ) -> QTRiskAssessment:
        drugs_lower = [d.lower().strip() for d in drugs]

        # Identify QT-prolonging drugs
        qt_risk_found: list[tuple[str, CredibleMedsRisk]] = []
        for drug_name in drugs_lower:
            for qt_drug, risk_level in QT_RISK_DRUGS.items():
                if qt_drug in drug_name or drug_name in qt_drug:
                    qt_risk_found.append((drug_name, risk_level))
                    break

        # Tisdale score
        score = 0
        if age_years and age_years > 68: score += TISDALE_RISK_FACTORS["age_>68_years"]
        if is_female: score += TISDALE_RISK_FACTORS["female_sex"]
        if on_loop_diuretic: score += TISDALE_RISK_FACTORS["loop_diuretic"]
        if serum_k and serum_k < 3.0: score += TISDALE_RISK_FACTORS["serum_k_<3.0"]
        elif serum_k and serum_k < 3.5: score += TISDALE_RISK_FACTORS["serum_k_<3.5"]
        if qtc_ms and qtc_ms > 450: score += TISDALE_RISK_FACTORS["admission_qtc_>450ms"]
        if has_acute_mi: score += TISDALE_RISK_FACTORS["acute_mi"]
        if has_heart_failure: score += TISDALE_RISK_FACTORS["heart_failure"]
        if has_hepatic_failure: score += TISDALE_RISK_FACTORS["hepatic_failure"]
        if has_sepsis: score += TISDALE_RISK_FACTORS["sepsis"]
        known_count = len([d for d, r in qt_risk_found if r in (CredibleMedsRisk.KNOWN, CredibleMedsRisk.CONDITIONAL)])
        if known_count == 1: score += TISDALE_RISK_FACTORS["one_qt_prolonging_drug"]
        elif known_count >= 2: score += TISDALE_RISK_FACTORS["two_or_more_qt_drugs"]

        if score < 7: risk_level = "low"
        elif score <= 11: risk_level = "moderate"
        else: risk_level = "high"

        is_high = risk_level == "high" or known_count >= 2 or (qtc_ms and qtc_ms > 500)

        recommendations = []
        message_lines = [f"⚡ QT RISK ASSESSMENT — Tisdale Score: {score} ({risk_level.upper()})"]

        if qt_risk_found:
            for drug_name, risk in qt_risk_found:
                icon = {"known": "🔴", "conditional": "🟠", "possible": "🟡"}.get(risk.value, "⚪")
                message_lines.append(f"   {icon} {drug_name}: CredibleMeds {risk.value.upper()} risk")

        if known_count >= 2:
            message_lines.append("   🚨 ≥2 QT-PROLONGING DRUGS: Avoid combination — high TdP risk")
            recommendations.append("ECG before starting and after each dose change")
            recommendations.append("Cardiologist review for this combination")

        if qtc_ms:
            if qtc_ms > 500:
                message_lines.append(f"   🚨 QTc {qtc_ms}ms — CRITICALLY PROLONGED: withhold QT drugs, urgent cardiology")
                recommendations.append("WITHHOLD QT-prolonging drugs immediately")
                recommendations.append("Urgent cardiology review")
            elif qtc_ms > 470:
                message_lines.append(f"   ⚠️ QTc {qtc_ms}ms — prolonged: proceed with extreme caution")
                recommendations.append("Repeat ECG in 2-4 hours")
            elif qtc_ms > 450:
                message_lines.append(f"   ⚠️ QTc {qtc_ms}ms — borderline prolonged: monitor")

        if serum_k and serum_k < 3.5:
            message_lines.append(f"   ⚠️ K+ {serum_k:.1f} mmol/L — HYPOKALAEMIA amplifies QT risk: correct before starting QT drugs")
            recommendations.append(f"Correct hypokalaemia to K+ >4.0 mmol/L before QT-prolonging drugs")

        if is_high:
            recommendations.insert(0, "Baseline ECG before prescribing")
            recommendations.append("Repeat ECG 3-5 days after starting/dose change")

        return QTRiskAssessment(
            drugs=drugs, qt_risk_drugs=qt_risk_found, tisdale_score=score,
            tisdale_risk_level=risk_level, concurrent_qt_count=known_count,
            is_high_risk=is_high, safety_message="\n".join(message_lines),
            recommendations=recommendations,
        )


# ─────────────────────────────────────────────────────────────────────────────
# L3-17: DRUG-FOOD & HERB INTERACTION ENGINE
# Architecture: 'Deterministic interaction checking: warfarin-vitamin K,
# statins-grapefruit, MAOIs-tyramine, St John's Wort CYP450 inducer.'
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DrugFoodInteraction:
    drug: str
    food_substance: str
    severity: str          # "contraindicated"|"major"|"moderate"|"minor"
    mechanism: str
    effect: str
    management: str
    examples: list[str]
    source: str

DRUG_FOOD_INTERACTIONS: list[DrugFoodInteraction] = [
    DrugFoodInteraction("warfarin", "vitamin K rich foods", "major", "Dietary vitamin K competes with warfarin's mechanism of action (vitamin K antagonism)", "Unpredictable INR fluctuation — subtherapeutic anticoagulation or over-anticoagulation", "Maintain consistent vitamin K intake — do not eliminate foods but keep intake constant week to week", ["spinach","kale","broccoli","Brussels sprouts","parsley","green tea"], "MHRA / BNF / NICE"),
    DrugFoodInteraction("statins", "grapefruit", "moderate", "Furanocoumarins in grapefruit inhibit intestinal CYP3A4 irreversibly for 24-72h", "Markedly increased statin plasma levels (up to 15x for simvastatin) — myopathy risk", "Avoid grapefruit and grapefruit juice with atorvastatin, simvastatin, lovastatin. Pravastatin and rosuvastatin unaffected", ["grapefruit","grapefruit juice","Seville orange","pomelo"], "MHRA / FDA"),
    DrugFoodInteraction("maoi", "tyramine-rich foods", "contraindicated", "MAO-A inhibition prevents tyramine metabolism in gut wall — sympathomimetic crisis", "HYPERTENSIVE CRISIS — potentially fatal: severe headache, hypertensive stroke, death", "CONTRAINDICATED: all tyramine-rich foods during MAOI therapy and 2 weeks after stopping", ["aged cheese","cured meats","sauerkraut","soy sauce","marmite","chianti","tap beer","fermented foods","overripe fruit"], "BNF / MHRA / FDA"),
    DrugFoodInteraction("ssri", "alcohol", "moderate", "Additive CNS depression + serotonergic effect potentiation", "Increased sedation, impaired psychomotor function, worsened depression", "Advise patients to limit alcohol. Avoid whilst dose stabilising", ["alcohol","beer","wine","spirits"], "BNF / MHRA"),
    DrugFoodInteraction("metformin", "alcohol", "moderate", "Both cause lactic acidosis via different mechanisms; combined risk amplified", "Lactic acidosis risk — potentially fatal", "Advise avoid heavy alcohol use. Occasional moderate alcohol: discuss with clinician", ["alcohol","beer","wine","spirits"], "BNF / MHRA"),
    DrugFoodInteraction("ciprofloxacin", "dairy products", "moderate", "Calcium in dairy chelates ciprofloxacin — reduced absorption", "Reduced ciprofloxacin bioavailability by up to 40%", "Take ciprofloxacin 1h before or 2h after dairy products, calcium-fortified foods", ["milk","yogurt","cheese","calcium supplements","fortified orange juice"], "BNF / PDR"),
    DrugFoodInteraction("tetracycline", "dairy products", "major", "Calcium chelation — forms insoluble complexes with tetracycline", "Absorption reduced by up to 80% — antibiotic failure", "Take tetracyclines on empty stomach 1h before or 2h after dairy/calcium/antacids", ["milk","cheese","yogurt","calcium supplements","antacids"], "BNF"),
    DrugFoodInteraction("levothyroxine", "calcium/iron supplements", "major", "Calcium and iron bind levothyroxine in gut — reduced absorption", "Hypothyroidism poorly controlled — TSH elevation", "Take levothyroxine 30-60 min before breakfast. Separate from calcium/iron by ≥4 hours", ["calcium supplements","iron tablets","antacids","coffee (mild effect)"], "MHRA / BNF"),
    DrugFoodInteraction("theophylline", "caffeine", "moderate", "Competitive adenosine receptor blockade + additive CNS stimulation", "Theophylline toxicity symptoms amplified: tachycardia, insomnia, seizures at toxic levels", "Limit caffeine intake. Monitor theophylline levels", ["coffee","energy drinks","cola","strong tea"], "BNF"),
    DrugFoodInteraction("lithium", "sodium/caffeine", "major", "Sodium depletion causes compensatory renal lithium retention (treating lithium like sodium). Caffeine increases lithium excretion", "Sodium restriction → lithium toxicity. Caffeine excess/sudden cessation → lithium level fluctuation", "Maintain consistent daily sodium intake. Avoid crash diets. Consistent caffeine intake. Monitor lithium levels at dietary changes", ["low-sodium diet","salt restriction","caffeine (variable)"], "BNF / MHRA"),
    DrugFoodInteraction("sildenafil", "grapefruit", "moderate", "CYP3A4 inhibition by grapefruit furanocoumarins — sildenafil plasma levels increased", "Increased hypotension, visual disturbances, priapism", "Avoid grapefruit juice with sildenafil/tadalafil/vardenafil", ["grapefruit","grapefruit juice","pomelo"], "MHRA / BNF"),
    DrugFoodInteraction("cyclosporin", "grapefruit", "major", "CYP3A4 and P-glycoprotein inhibition — major increase in cyclosporin levels", "Nephrotoxicity, hepatotoxicity — organ rejection or toxicity from level fluctuation", "AVOID all grapefruit products with cyclosporin. Use orange juice as alternative", ["grapefruit","grapefruit juice","Seville orange"], "MHRA / BNF / FDA"),
    # Herbal interactions
    DrugFoodInteraction("warfarin", "St John's Wort", "major", "St John's Wort induces CYP3A4, CYP2C9, P-glycoprotein — markedly accelerates warfarin metabolism", "Sub-therapeutic anticoagulation — thrombosis, stroke risk. Also: serotonin syndrome with SSRIs/tricyclics", "CONTRAINDICATED with warfarin. Strict avoidance. Inform patients to report all herbal use. MHRA public warning", ["St John's Wort","Hypericum perforatum","Hypericum"], "MHRA public warning / BNF"),
    DrugFoodInteraction("ssri", "St John's Wort", "major", "Additive serotonergic effect", "Serotonin syndrome risk", "Avoid St John's Wort with any serotonergic drug", ["St John's Wort","Hypericum"], "MHRA / BNF"),
    DrugFoodInteraction("anticoagulants", "garlic high dose", "moderate", "Inhibits platelet aggregation + possible anticoagulant effect", "Increased bleeding risk with warfarin, heparin, DOACs", "Advise patients on anticoagulants to inform about garlic supplements (culinary use: generally safe)", ["garlic supplements (high dose)"], "BNF"),
    DrugFoodInteraction("digoxin", "liquorice", "moderate", "Liquorice causes pseudoaldosteronism — hypokalaemia amplifies digoxin toxicity", "Digoxin toxicity: bradycardia, arrhythmias", "Avoid regular large quantities of liquorice with digoxin", ["liquorice","licorice","liquorice root"], "BNF"),
]


class DrugFoodHerbEngine:
    """L3-17: Deterministic drug-food and drug-herb interaction checking."""

    def check(self, drug: str, foods_and_supplements: Optional[list[str]] = None) -> list[DrugFoodInteraction]:
        """Check for drug-food/herb interactions."""
        drug_lower = drug.lower()
        results = []
        for rule in DRUG_FOOD_INTERACTIONS:
            if rule.drug.lower() in drug_lower or drug_lower in rule.drug.lower():
                if foods_and_supplements:
                    for f in foods_and_supplements:
                        if any(ex.lower() in f.lower() or f.lower() in ex.lower() for ex in rule.examples):
                            results.append(rule)
                            break
                else:
                    results.append(rule)
        severity_order = {"contraindicated": 0, "major": 1, "moderate": 2, "minor": 3}
        results.sort(key=lambda r: severity_order.get(r.severity, 9))
        return results

    def format_alerts(self, interactions: list[DrugFoodInteraction]) -> str:
        if not interactions:
            return ""
        icons = {"contraindicated": "🚫", "major": "⚠️", "moderate": "⚡", "minor": "ℹ️"}
        lines = ["🍽️ DRUG-FOOD/HERB INTERACTIONS:"]
        for ix in interactions:
            icon = icons.get(ix.severity, "⚡")
            lines.append(f"{icon} {ix.drug} + {ix.food_substance} ({ix.severity.upper()})")
            lines.append(f"   Effect: {ix.effect}")
            lines.append(f"   Management: {ix.management}")
        return "\n".join(lines)

# Runtime compatibility shim: older cql_kernel expects QTProlongationEngine.check().
def _qt_check(self, drugs: list[str], patient_factors: dict | None = None):
    pf = patient_factors or {}
    return self.assess(
        drugs=drugs,
        qtc_ms=pf.get("qtc_ms") or pf.get("qtc"),
        age_years=pf.get("age"),
        is_female=(str(pf.get("sex", "")).upper() == "F"),
        serum_k=pf.get("potassium") or pf.get("serum_k"),
    )

try:
    QTProlongationEngine.check = _qt_check  # type: ignore[name-defined, assignment]
except NameError:
    pass
