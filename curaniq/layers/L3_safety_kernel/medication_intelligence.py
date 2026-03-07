"""
CURANIQ — Medical Evidence Operating System
Layer 3: Deterministic Safety Kernel

L3-2  Medication Intelligence Engine — THE FIRST WEDGE
       Renal/hepatic dose adjustment, DDI checking, Black Box flags
L3-5  Smart Formulary Engine
L3-6  Time-Aware Clinical Timeline Builder
"""
from __future__ import annotations
import logging, re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# L3-2: MEDICATION INTELLIGENCE — THE FIRST WEDGE
# Architecture: 'Renal/hepatic dose adjustment. DDI checking.
# This is THE differentiator that makes CURANIQ safer than GPT.'
# ─────────────────────────────────────────────────────────────────────────────

class RenalDoseAction(str, Enum):
    NORMAL          = "normal"
    REDUCE_DOSE     = "reduce_dose"
    EXTEND_INTERVAL = "extend_interval"
    REDUCE_AND_INTERVAL = "reduce_dose_and_extend_interval"
    AVOID           = "avoid"
    CONTRAINDICATED = "contraindicated"
    USE_WITH_CAUTION = "use_with_caution"
    MONITOR_CLOSELY = "monitor_closely"

class HepaticClass(str, Enum):
    CHILD_PUGH_A = "child_pugh_a"   # Mild (5-6 points)
    CHILD_PUGH_B = "child_pugh_b"   # Moderate (7-9 points)
    CHILD_PUGH_C = "child_pugh_c"   # Severe (10-15 points)

@dataclass
class RenalDoseRule:
    drug: str
    egfr_threshold_ml_min: float
    action: RenalDoseAction
    dose_adjustment: Optional[str]
    monitoring: Optional[str]
    rationale: str
    source: str

@dataclass
class HepaticDoseRule:
    drug: str
    hepatic_class: HepaticClass
    action: RenalDoseAction
    dose_adjustment: Optional[str]
    monitoring: Optional[str]
    rationale: str
    source: str

@dataclass
class DDIRule:
    drug_a: str
    drug_b: str
    severity: str         # "contraindicated" | "major" | "moderate" | "minor"
    mechanism: str
    effect: str
    management: str
    source: str

@dataclass
class MedicationAssessment:
    drug: str
    egfr: Optional[float]
    hepatic_class: Optional[HepaticClass]
    renal_action: RenalDoseAction
    renal_adjustment: Optional[str]
    renal_rationale: Optional[str]
    hepatic_action: Optional[RenalDoseAction]
    hepatic_adjustment: Optional[str]
    ddi_alerts: list[DDIRule]
    black_box_warning: Optional[str]
    is_safe: bool
    safety_message: str
    monitoring_required: list[str]


# ── RENAL DOSE ADJUSTMENT RULES (comprehensive, CKD staging based) ──────────
RENAL_DOSE_RULES: list[RenalDoseRule] = [
    # Metformin — lactic acidosis risk
    RenalDoseRule("metformin", 60, RenalDoseAction.USE_WITH_CAUTION, "Max 1000mg/day if eGFR 45-60", "Monitor eGFR every 3-6 months", "Reduced renal clearance increases plasma concentration", "FDA / MHRA / NICE NG28"),
    RenalDoseRule("metformin", 45, RenalDoseAction.REDUCE_DOSE, "Max 500mg/day if eGFR 30-45", "Monitor eGFR monthly", "Accumulation risk with eGFR 30-45", "FDA / MHRA"),
    RenalDoseRule("metformin", 30, RenalDoseAction.CONTRAINDICATED, None, None, "Contraindicated eGFR <30: fatal lactic acidosis risk", "FDA Black Box / MHRA"),
    # Gabapentin — dose-related toxicity
    RenalDoseRule("gabapentin", 60, RenalDoseAction.REDUCE_DOSE, "Reduce to 600-1800mg/day", "Monitor for sedation, dizziness", "Renally cleared; accumulation in CKD", "BNF / Prescribers' Digital Reference"),
    RenalDoseRule("gabapentin", 30, RenalDoseAction.REDUCE_DOSE, "Reduce to 200-700mg/day", "Monitor closely for CNS toxicity", "Significant accumulation eGFR <30", "BNF / PDR"),
    RenalDoseRule("gabapentin", 15, RenalDoseAction.REDUCE_DOSE, "Max 100-300mg/day. Haemodialysis: supplemental dose post-dialysis", "TDM if available", "Major accumulation in end-stage renal disease", "BNF / PDR"),
    # Dabigatran — stroke prevention AF
    RenalDoseRule("dabigatran", 50, RenalDoseAction.REDUCE_DOSE, "Reduce to 110mg BD (AF): manufacturer and NICE guidance", "Monitor renal function every 6 months", "80% renally eliminated; accumulation risk in CKD", "NICE TA249 / EMA SmPC"),
    RenalDoseRule("dabigatran", 30, RenalDoseAction.CONTRAINDICATED, None, None, "Contraindicated eGFR <30: major bleeding risk due to accumulation", "NICE / FDA / EMA"),
    # Rivaroxaban
    RenalDoseRule("rivaroxaban", 50, RenalDoseAction.REDUCE_DOSE, "Reduce to 15mg OD in AF if eGFR 15-50", "Monitor renal function 3-6 monthly", "33% renally cleared", "NICE / EMA SmPC"),
    RenalDoseRule("rivaroxaban", 15, RenalDoseAction.AVOID, None, "Avoid; data limited", "Insufficient data eGFR <15", "EMA SmPC"),
    # Apixaban
    RenalDoseRule("apixaban", 25, RenalDoseAction.REDUCE_DOSE, "Reduce if ≥2 of: age≥80, weight≤60kg, creatinine≥133µmol/L", "Monitor renal function 6-monthly", "27% renally eliminated", "NICE / FDA"),
    # Digoxin — narrow therapeutic index
    RenalDoseRule("digoxin", 50, RenalDoseAction.REDUCE_DOSE, "Reduce loading and maintenance dose by 25-50%", "Serum digoxin levels. Target 0.5-0.9 ng/mL", "70% renally eliminated; toxicity risk in CKD", "BNF / AHA"),
    RenalDoseRule("digoxin", 10, RenalDoseAction.AVOID, None, "TDM mandatory. Consider alternative", "High toxicity risk in dialysis patients", "BNF"),
    # ACEI/ARB — hyperkalaemia and AKI risk
    RenalDoseRule("lisinopril", 30, RenalDoseAction.REDUCE_DOSE, "Start at low dose 2.5-5mg. Titrate with monitoring", "eGFR and K+ within 1-2 weeks of initiation/dose change", "Risk of hyperkalaemia and AKI in advanced CKD", "NICE CKD guidelines / BNF"),
    RenalDoseRule("ramipril", 30, RenalDoseAction.REDUCE_DOSE, "Max 5mg/day if eGFR <30. Reduce further if hyperkalaemia", "eGFR, K+, creatinine at 1-2 weeks", "RAAS blockade in CKD: monitor carefully", "NICE / BNF"),
    # NSAIDs — nephrotoxic
    RenalDoseRule("ibuprofen", 30, RenalDoseAction.CONTRAINDICATED, None, None, "NSAIDs contraindicated in eGFR <30: nephrotoxic, worsen CKD", "NICE / MHRA"),
    RenalDoseRule("naproxen", 30, RenalDoseAction.CONTRAINDICATED, None, None, "NSAIDs contraindicated in advanced CKD", "NICE / MHRA"),
    RenalDoseRule("diclofenac", 30, RenalDoseAction.CONTRAINDICATED, None, None, "NSAIDs contraindicated in advanced CKD", "NICE / MHRA"),
    # Spironolactone — hyperkalaemia
    RenalDoseRule("spironolactone", 45, RenalDoseAction.USE_WITH_CAUTION, "Use with extreme caution. Monitor K+ closely", "K+ within 1 week, then monthly", "Risk of life-threatening hyperkalaemia in CKD", "NICE / BNF"),
    RenalDoseRule("spironolactone", 30, RenalDoseAction.CONTRAINDICATED, None, None, "Contraindicated eGFR <30: severe hyperkalaemia risk", "NICE / BNF"),
    # Trimethoprim — raises creatinine + hyperkalaemia
    RenalDoseRule("trimethoprim", 30, RenalDoseAction.REDUCE_DOSE, "Half dose if eGFR 15-30. Avoid if eGFR <15", "Monitor K+ and creatinine after 5 days (creatinine rise ≠ true AKI)", "Competes with creatinine secretion, blocks K+ excretion", "MHRA / BNF"),
    # Allopurinol
    RenalDoseRule("allopurinol", 60, RenalDoseAction.REDUCE_DOSE, "Max 100mg/day if eGFR 30-60; increase slowly", "Urate levels, skin reactions", "Accumulation of oxypurinol metabolite in CKD — severe skin reactions (SJS/TEN)", "BNF / MHRA"),
    RenalDoseRule("allopurinol", 30, RenalDoseAction.REDUCE_DOSE, "50mg every other day; specialist input advised", "Urate, renal function, skin", "Severe hypersensitivity risk", "BNF"),
    # Codeine/opioids
    RenalDoseRule("codeine", 30, RenalDoseAction.AVOID, None, "Use alternative opioid. Consider morphine with caution or fentanyl", "Active metabolite (morphine-6-glucuronide) accumulates — respiratory depression risk", "BNF / MHRA"),
    RenalDoseRule("morphine", 30, RenalDoseAction.REDUCE_DOSE, "Reduce dose and frequency. Consider fentanyl", "Respiratory rate, sedation score", "M6G accumulation in CKD causes prolonged sedation", "BNF"),
    # Gentamicin
    RenalDoseRule("gentamicin", 60, RenalDoseAction.EXTEND_INTERVAL, "Extend dosing interval based on levels. Hartmann nomogram", "Pre- and post-dose levels. Audiometry", "Nephrotoxic + ototoxic; accumulates in CKD", "BNF / PHE"),
    RenalDoseRule("gentamicin", 30, RenalDoseAction.REDUCE_AND_INTERVAL, "Single daily dosing with extended intervals per TDM", "Daily levels. Renal function. Hearing", "High toxicity risk — TDM mandatory", "BNF"),
    # Vancomycin
    RenalDoseRule("vancomycin", 60, RenalDoseAction.EXTEND_INTERVAL, "Extend dosing interval. Target AUC/MIC 400-600", "Trough levels or AUC-guided TDM", "Renally cleared — accumulation and nephrotoxicity in CKD", "ASHP/IDSA 2020 guidelines"),
    # Lithium
    RenalDoseRule("lithium", 60, RenalDoseAction.REDUCE_DOSE, "Reduce dose. Target serum level 0.4-0.8 mmol/L", "Serum lithium monthly. eGFR 3-6 monthly. TFTs annually", "Renally eliminated — narrow therapeutic index. Toxicity risk in CKD", "NICE / BNF"),
    RenalDoseRule("lithium", 30, RenalDoseAction.AVOID, None, "Specialist nephrology/psychiatry input. Consider alternative mood stabiliser", "Lithium toxicity in advanced CKD: tremor, confusion, seizures", "NICE / BNF"),
]

# ── HEPATIC DOSE ADJUSTMENT RULES ────────────────────────────────────────────
HEPATIC_DOSE_RULES: list[HepaticDoseRule] = [
    HepaticDoseRule("methotrexate", HepaticClass.CHILD_PUGH_B, RenalDoseAction.AVOID, None, "LFTs weekly", "Hepatotoxic — avoid in hepatic impairment", "BNF / MHRA"),
    HepaticDoseRule("paracetamol", HepaticClass.CHILD_PUGH_B, RenalDoseAction.REDUCE_DOSE, "Max 2g/day. Avoid prolonged use", "LFTs if prolonged use", "Reduced glucuronidation; N-acetyl-p-aminobenzoquinone imine accumulates", "BNF / MHRA"),
    HepaticDoseRule("paracetamol", HepaticClass.CHILD_PUGH_C, RenalDoseAction.AVOID, None, "Use alternative", "Severe hepatic impairment — hepatotoxicity risk", "BNF / MHRA"),
    HepaticDoseRule("statins", HepaticClass.CHILD_PUGH_B, RenalDoseAction.USE_WITH_CAUTION, "Use low dose with LFT monitoring", "LFTs at baseline, 3 months, 6 months", "Hepatically metabolised; accumulation in liver disease", "BNF / MHRA"),
    HepaticDoseRule("statins", HepaticClass.CHILD_PUGH_C, RenalDoseAction.CONTRAINDICATED, None, None, "Contraindicated in severe hepatic impairment", "BNF / MHRA"),
    HepaticDoseRule("warfarin", HepaticClass.CHILD_PUGH_B, RenalDoseAction.REDUCE_DOSE, "Significantly reduced dose; INR erratic", "Daily INR initially. Close monitoring essential", "Reduced clotting factor synthesis — unpredictable anticoagulation", "BNF"),
    HepaticDoseRule("warfarin", HepaticClass.CHILD_PUGH_C, RenalDoseAction.CONTRAINDICATED, None, None, "Contraindicated in severe hepatic failure — coagulopathy risk", "BNF / MHRA"),
    HepaticDoseRule("morphine", HepaticClass.CHILD_PUGH_B, RenalDoseAction.REDUCE_DOSE, "Reduce dose by 50%. Extend interval", "Sedation score, respiratory rate", "First-pass metabolism reduced — increased bioavailability", "BNF"),
    HepaticDoseRule("rifampicin", HepaticClass.CHILD_PUGH_B, RenalDoseAction.AVOID, None, "Consider alternative; specialist input", "Hepatotoxic — avoid in active liver disease", "BNF / MHRA"),
    HepaticDoseRule("azathioprine", HepaticClass.CHILD_PUGH_B, RenalDoseAction.REDUCE_DOSE, "Reduce dose. Monitor LFTs and FBC", "Weekly FBC and LFTs initially", "Hepatically metabolised; myelosuppression risk increased", "BNF / MHRA"),
]

# ── DRUG-DRUG INTERACTION RULES ───────────────────────────────────────────────
DDI_RULES: list[DDIRule] = [
    DDIRule("warfarin", "aspirin", "major", "Additive anticoagulant effect + GI mucosal damage", "Significantly increased bleeding risk (GI, intracranial)", "Avoid combination unless clear indication (e.g. mechanical heart valve + AF). If used: low-dose aspirin, PPI cover, close INR monitoring", "MHRA / BNF"),
    DDIRule("warfarin", "nsaids", "major", "Displacement from albumin + inhibition of platelet function + GI mucosal damage", "Major bleeding risk", "Avoid NSAIDs in anticoagulated patients. Use paracetamol for analgesia", "NICE / BNF"),
    DDIRule("metformin", "alcohol", "moderate", "Both cause lactic acidosis; combined risk", "Lactic acidosis risk amplified", "Advise patients to avoid heavy alcohol use with metformin", "BNF / MHRA"),
    DDIRule("ssri", "tramadol", "major", "Additive serotonergic effect", "Serotonin syndrome: hyperthermia, agitation, clonus, autonomic instability", "Avoid combination. If essential: start tramadol at lowest dose, monitor for serotonin syndrome features", "MHRA / FDA"),
    DDIRule("maoi", "ssri", "contraindicated", "Serotonin syndrome: additive serotonergic toxicity", "Life-threatening serotonin syndrome", "CONTRAINDICATED. Washout period required: 14 days after stopping MAOI before starting SSRI; 7 days (fluoxetine: 5 weeks) after stopping SSRI before starting MAOI", "BNF / MHRA / FDA Black Box"),
    DDIRule("simvastatin", "amiodarone", "major", "Amiodarone inhibits CYP3A4 — simvastatin plasma levels markedly elevated", "Myopathy, rhabdomyolysis", "Simvastatin max 20mg/day with amiodarone. Consider switching to atorvastatin or pravastatin (not CYP3A4-dependent)", "MHRA / FDA"),
    DDIRule("simvastatin", "clarithromycin", "contraindicated", "Clarithromycin potent CYP3A4 inhibitor — simvastatin 10-fold increase", "Severe myopathy, rhabdomyolysis, acute kidney injury", "CONTRAINDICATED. Suspend simvastatin during clarithromycin course. Use azithromycin if macrolide needed", "MHRA / FDA"),
    DDIRule("statins", "grapefruit", "moderate", "Furanocoumarins in grapefruit inhibit intestinal CYP3A4", "Increased statin plasma levels — myopathy risk", "Avoid grapefruit/grapefruit juice with atorvastatin, simvastatin, lovastatin. Pravastatin/rosuvastatin unaffected", "MHRA / FDA"),
    DDIRule("lithium", "nsaids", "major", "NSAIDs reduce renal prostaglandin synthesis → reduced lithium excretion", "Lithium toxicity: tremor, vomiting, confusion, seizures, renal failure", "Avoid NSAIDs with lithium. Use paracetamol if analgesia needed. Monitor lithium levels if NSAID unavoidable", "MHRA / BNF"),
    DDIRule("lithium", "thiazide", "major", "Thiazides reduce renal lithium excretion", "Lithium toxicity", "Avoid thiazides with lithium. If initiated: halve lithium dose, measure levels in 4-7 days", "BNF / MHRA"),
    DDIRule("ace_inhibitor", "potassium_sparing_diuretic", "major", "Both reduce aldosterone → additive hyperkalaemic effect", "Life-threatening hyperkalaemia (K+ >6.5 mmol/L, cardiac arrest)", "Use with extreme caution. Monitor K+ within 1 week. Avoid in CKD (additive risk)", "NICE / BNF"),
    DDIRule("ace_inhibitor", "nsaids", "major", "NSAIDs antagonise ACEi effect + additive nephrotoxicity", "AKI risk significantly increased ('triple whammy' with diuretic)", "Avoid triple combination ACEi + NSAID + diuretic. Monitor renal function if essential", "MHRA 'Triple Whammy' warning"),
    DDIRule("clopidogrel", "ppi", "moderate", "Omeprazole/esomeprazole inhibit CYP2C19 — reduces clopidogrel activation", "Reduced antiplatelet effect — increased thrombosis risk", "Use pantoprazole or lansoprazole (minimal CYP2C19 interaction) if PPI required with clopidogrel", "MHRA / FDA"),
    DDIRule("digoxin", "amiodarone", "major", "Amiodarone inhibits P-glycoprotein → increased digoxin levels", "Digoxin toxicity: bradycardia, heart block, nausea, visual disturbances", "Reduce digoxin dose by 30-50% when starting amiodarone. Monitor digoxin levels", "BNF / MHRA"),
    DDIRule("warfarin", "amiodarone", "major", "Amiodarone inhibits CYP2C9 — major warfarin metabolism inhibition", "INR markedly elevated — severe bleeding risk", "Reduce warfarin dose by 30-50%. Monitor INR weekly for 4-6 weeks. Effect persists for months after stopping amiodarone", "MHRA / BNF"),
    DDIRule("ciprofloxacin", "theophylline", "major", "Ciprofloxacin inhibits CYP1A2 — theophylline accumulation", "Theophylline toxicity: seizures, arrhythmias", "Reduce theophylline dose by 30-50% with ciprofloxacin. TDM mandatory. Use alternative antibiotic if possible", "BNF / MHRA"),
    DDIRule("methotrexate", "nsaids", "major", "NSAIDs reduce renal methotrexate excretion + displace from albumin", "Methotrexate toxicity: bone marrow suppression, mucositis, hepatotoxicity", "Avoid combination. If essential: reduce methotrexate dose, increase folinic acid, weekly FBC and LFTs", "MHRA / BNF"),
    DDIRule("sildenafil", "nitrates", "contraindicated", "Additive vasodilation via cGMP pathway", "Severe hypotension, syncope, myocardial ischaemia, death", "ABSOLUTELY CONTRAINDICATED. 24h washout after sildenafil; 48h for tadalafil before nitrate use", "FDA Black Box / MHRA"),
    DDIRule("phenytoin", "fluconazole", "major", "Fluconazole inhibits CYP2C9 — phenytoin levels can double", "Phenytoin toxicity: nystagmus, ataxia, confusion", "Monitor phenytoin levels closely. Reduce dose if toxicity develops", "BNF / MHRA"),
    DDIRule("maoi", "pethidine", "contraindicated", "Serotonin syndrome and opioid toxicity synergy", "Hyperpyrexia, excitation, convulsions, coma, death", "CONTRAINDICATED. Use morphine or fentanyl if opioid essential with MAOI (also use with extreme caution)", "BNF / FDA"),
]


def _normalize(name: str) -> str:
    return name.lower().strip().replace('-', '').replace(' ', '')


def _drug_matches(rule_drug: str, query_drug: str) -> bool:
    """Check if a rule applies to a given drug (handles partial matches)."""
    rd = _normalize(rule_drug)
    qd = _normalize(query_drug)
    return rd in qd or qd in rd


class MedicationIntelligenceEngine:
    """
    L3-2: Medication Intelligence Engine — THE FIRST WEDGE.
    
    Deterministic CQL-based checking of:
    - Renal dose adjustments (eGFR-stratified)
    - Hepatic dose adjustments (Child-Pugh class)
    - Drug-drug interactions (severity-graded)
    - Black Box Warning flags
    
    This engine runs BEFORE any LLM output. Its output is deterministic
    and cannot be overridden by the AI layer.
    """

    def assess(
        self,
        drug: str,
        egfr: Optional[float] = None,
        hepatic_class: Optional[HepaticClass] = None,
        concurrent_drugs: Optional[list[str]] = None,
        black_box_text: Optional[str] = None,
    ) -> MedicationAssessment:
        """Full medication safety assessment."""
        concurrent_drugs = concurrent_drugs or []

        # Renal check
        renal_action, renal_adj, renal_rat = self._check_renal(drug, egfr)

        # Hepatic check
        hepatic_action, hepatic_adj = self._check_hepatic(drug, hepatic_class)

        # DDI check
        ddi_alerts = self._check_ddi(drug, concurrent_drugs)

        # Safety summary
        is_safe = (
            renal_action not in (RenalDoseAction.CONTRAINDICATED, RenalDoseAction.AVOID)
            and not any(d.severity == "contraindicated" for d in ddi_alerts)
        )
        monitoring = self._build_monitoring_list(drug, egfr, renal_action, ddi_alerts)
        safety_message = self._build_safety_message(
            drug, egfr, renal_action, renal_adj, renal_rat,
            hepatic_action, hepatic_adj, ddi_alerts, black_box_text
        )

        return MedicationAssessment(
            drug=drug,
            egfr=egfr,
            hepatic_class=hepatic_class,
            renal_action=renal_action,
            renal_adjustment=renal_adj,
            renal_rationale=renal_rat,
            hepatic_action=hepatic_action,
            hepatic_adjustment=hepatic_adj,
            ddi_alerts=ddi_alerts,
            black_box_warning=black_box_text,
            is_safe=is_safe,
            safety_message=safety_message,
            monitoring_required=monitoring,
        )

    def _check_renal(
        self, drug: str, egfr: Optional[float]
    ) -> tuple[RenalDoseAction, Optional[str], Optional[str]]:
        if egfr is None:
            return RenalDoseAction.NORMAL, None, None

        applicable = [
            r for r in RENAL_DOSE_RULES
            if _drug_matches(r.drug, drug)
            and egfr < r.egfr_threshold_ml_min
        ]
        if not applicable:
            return RenalDoseAction.NORMAL, None, None

        # Take most restrictive rule
        priority_order = [
            RenalDoseAction.CONTRAINDICATED, RenalDoseAction.AVOID,
            RenalDoseAction.REDUCE_AND_INTERVAL, RenalDoseAction.REDUCE_DOSE,
            RenalDoseAction.EXTEND_INTERVAL, RenalDoseAction.USE_WITH_CAUTION,
            RenalDoseAction.MONITOR_CLOSELY, RenalDoseAction.NORMAL,
        ]
        def priority(r: RenalDoseRule) -> int:
            try: return priority_order.index(r.action)
            except ValueError: return 99

        most_restrictive = min(applicable, key=priority)
        return most_restrictive.action, most_restrictive.dose_adjustment, most_restrictive.rationale

    def _check_hepatic(
        self, drug: str, hepatic_class: Optional[HepaticClass]
    ) -> tuple[Optional[RenalDoseAction], Optional[str]]:
        if not hepatic_class:
            return None, None
        applicable = [
            r for r in HEPATIC_DOSE_RULES
            if _drug_matches(r.drug, drug)
            and r.hepatic_class == hepatic_class
        ]
        if not applicable:
            return None, None
        rule = applicable[0]
        return rule.action, rule.dose_adjustment

    def _check_ddi(self, drug: str, concurrent: list[str]) -> list[DDIRule]:
        alerts = []
        for rule in DDI_RULES:
            drug_a_match = _drug_matches(rule.drug_a, drug)
            drug_b_match = _drug_matches(rule.drug_b, drug)
            for concurrent_drug in concurrent:
                if drug_a_match and _drug_matches(rule.drug_b, concurrent_drug):
                    alerts.append(rule)
                    break
                if drug_b_match and _drug_matches(rule.drug_a, concurrent_drug):
                    alerts.append(rule)
                    break
        # Sort: contraindicated first, then major, moderate, minor
        severity_order = {"contraindicated": 0, "major": 1, "moderate": 2, "minor": 3}
        alerts.sort(key=lambda r: severity_order.get(r.severity, 9))
        return alerts

    def _build_monitoring_list(self, drug, egfr, action, ddi_alerts) -> list[str]:
        monitoring = []
        if action in (RenalDoseAction.REDUCE_DOSE, RenalDoseAction.USE_WITH_CAUTION, RenalDoseAction.MONITOR_CLOSELY):
            monitoring.append("eGFR — recheck within 4 weeks")
        for rule in [r for r in RENAL_DOSE_RULES if _drug_matches(r.drug, drug) and r.monitoring]:
            if rule.monitoring and rule.monitoring not in monitoring:
                monitoring.append(rule.monitoring)
        for ddi in ddi_alerts:
            if ddi.severity in ("contraindicated", "major"):
                monitoring.append(f"Monitor for {ddi.effect} [{ddi.drug_a} + {ddi.drug_b}]")
        return monitoring[:8]

    def _build_safety_message(
        self, drug, egfr, renal_action, renal_adj, renal_rat,
        hepatic_action, hepatic_adj, ddi_alerts, black_box_text
    ) -> str:
        lines = []
        if black_box_text:
            lines.append(f"⬛ BLACK BOX WARNING: {black_box_text[:300]}")
        if renal_action == RenalDoseAction.CONTRAINDICATED:
            lines.append(f"🚫 CONTRAINDICATED: {drug} contraindicated at eGFR {egfr:.0f} mL/min/1.73m²")
            if renal_rat: lines.append(f"   Reason: {renal_rat}")
        elif renal_action == RenalDoseAction.AVOID:
            lines.append(f"⚠️ AVOID: {drug} should be avoided at eGFR {egfr:.0f}. {renal_rat or ''}")
        elif renal_action in (RenalDoseAction.REDUCE_DOSE, RenalDoseAction.EXTEND_INTERVAL, RenalDoseAction.REDUCE_AND_INTERVAL):
            lines.append(f"⚠️ DOSE ADJUSTMENT REQUIRED: eGFR {egfr:.0f} mL/min/1.73m²")
            if renal_adj: lines.append(f"   Adjustment: {renal_adj}")
        if hepatic_action == RenalDoseAction.CONTRAINDICATED:
            lines.append(f"🚫 HEPATIC CONTRAINDICATION: {drug} contraindicated in {hepatic_adj or 'severe'} hepatic impairment")
        elif hepatic_action == RenalDoseAction.REDUCE_DOSE:
            lines.append(f"⚠️ HEPATIC DOSE REDUCTION: {hepatic_adj}")
        for ddi in ddi_alerts:
            severity_icons = {"contraindicated": "🚫", "major": "⚠️", "moderate": "⚡", "minor": "ℹ️"}
            icon = severity_icons.get(ddi.severity, "⚡")
            lines.append(f"{icon} {ddi.severity.upper()} DDI: {ddi.drug_a} + {ddi.drug_b}")
            lines.append(f"   Effect: {ddi.effect}")
            lines.append(f"   Management: {ddi.management}")
        if not lines:
            lines.append(f"✅ {drug}: No dose adjustment required at eGFR {egfr:.0f}" if egfr else f"✅ {drug}: No specific renal/hepatic precautions identified")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# L3-5: SMART FORMULARY ENGINE
# Architecture: 'Restricts to drugs available in jurisdiction/formulary.
# Ties to L2-6 and L3-2.'
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FormularyEntry:
    drug: str
    jurisdictions: list[str]
    available: bool
    tier: str        # "first_line" | "second_line" | "specialist_only" | "not_listed"
    restrictions: Optional[str]
    alternatives: list[str]
    who_essential: bool = False
    controlled: bool = False


FORMULARY: list[FormularyEntry] = [
    # Cardiovascular
    FormularyEntry("metformin", ["uz","uk","us","eu","who"], True, "first_line", None, [], who_essential=True),
    FormularyEntry("lisinopril", ["uk","us","eu","uz"], True, "first_line", None, ["ramipril","enalapril"], who_essential=True),
    FormularyEntry("amlodipine", ["uk","us","eu","uz"], True, "first_line", None, ["nifedipine"], who_essential=True),
    FormularyEntry("atorvastatin", ["uk","us","eu","uz"], True, "first_line", None, ["simvastatin","rosuvastatin"]),
    FormularyEntry("warfarin", ["uk","us","eu","uz"], True, "first_line", "INR monitoring required", ["rivaroxaban","apixaban"], who_essential=True),
    FormularyEntry("rivaroxaban", ["uk","us","eu"], True, "first_line", "Specialist initiation in some settings", ["apixaban","dabigatran"]),
    FormularyEntry("rivaroxaban", ["uz"], True, "second_line", "May have availability constraints; verify local formulary", ["warfarin"]),
    FormularyEntry("sacubitril/valsartan", ["uk","us","eu"], True, "specialist_only", "Cardiologist initiation (NICE TA388). HFrEF NYHA II-IV", ["enalapril"]),
    # Antibiotics
    FormularyEntry("amoxicillin", ["uk","us","eu","uz","who"], True, "first_line", None, [], who_essential=True),
    FormularyEntry("co-amoxiclav", ["uk","us","eu","uz"], True, "first_line", None, ["amoxicillin+metronidazole"]),
    FormularyEntry("ciprofloxacin", ["uk","us","eu","uz","who"], True, "first_line", "Reserve for specific indications (resistance stewardship)", ["trimethoprim","nitrofurantoin"], who_essential=True),
    FormularyEntry("vancomycin", ["uk","us","eu","uz","who"], True, "specialist_only", "IV — hospital only. TDM required", ["teicoplanin"], who_essential=True),
    # Analgesics
    FormularyEntry("paracetamol", ["uk","us","eu","uz","who"], True, "first_line", None, [], who_essential=True),
    FormularyEntry("ibuprofen", ["uk","us","eu","uz","who"], True, "first_line", "Avoid in CKD, cardiac failure, elderly", ["paracetamol","naproxen"], who_essential=True),
    FormularyEntry("morphine", ["uk","us","eu","uz","who"], True, "first_line", "Controlled drug — CD register required", [], who_essential=True, controlled=True),
    FormularyEntry("fentanyl", ["uk","us","eu"], True, "specialist_only", "CD — specialist palliative/anaesthetics", ["morphine"], controlled=True),
    # Respiratory
    FormularyEntry("salbutamol", ["uk","us","eu","uz","who"], True, "first_line", None, [], who_essential=True),
    FormularyEntry("beclometasone inhaler", ["uk","us","eu","uz"], True, "first_line", None, ["fluticasone"]),
    # Endocrine
    FormularyEntry("levothyroxine", ["uk","us","eu","uz","who"], True, "first_line", None, [], who_essential=True),
    FormularyEntry("insulin", ["uk","us","eu","uz","who"], True, "first_line", None, [], who_essential=True),
    FormularyEntry("dapagliflozin", ["uk","us","eu"], True, "first_line", "NICE NG28: CKD + T2DM. Cardiologist/nephrologist for CKD indication", ["empagliflozin"]),
    FormularyEntry("dapagliflozin", ["uz"], False, "not_listed", "May not be available locally — verify supply chain", ["metformin","gliclazide"]),
]


class SmartFormularyEngine:
    """
    L3-5: Smart Formulary Engine.
    Restricts recommendations to drugs available in the patient's jurisdiction.
    Provides alternatives when drugs are not formulary-listed.
    """

    def check(self, drug: str, jurisdiction: str = "intl") -> FormularyEntry:
        jur = jurisdiction.lower()
        drug_lower = drug.lower().strip()
        for entry in FORMULARY:
            if _drug_matches(entry.drug, drug_lower):
                if jur in entry.jurisdictions or jur == "intl":
                    return entry
        # Not found — not listed
        return FormularyEntry(drug, [], False, "not_listed", None, [], False, False)

    def get_alternatives(self, drug: str, jurisdiction: str) -> list[str]:
        entry = self.check(drug, jurisdiction)
        return entry.alternatives

    def is_available(self, drug: str, jurisdiction: str) -> bool:
        return self.check(drug, jurisdiction).available

    def filter_to_formulary(self, drugs: list[str], jurisdiction: str) -> list[str]:
        return [d for d in drugs if self.is_available(d, jurisdiction)]


# ─────────────────────────────────────────────────────────────────────────────
# L3-6: TIME-AWARE CLINICAL TIMELINE BUILDER
# Architecture: 'Builds patient-specific timeline from EHR data.
# Tracks drug start/stop dates, dose changes, lab trends over time.'
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TimelineEvent:
    event_type: str       # "drug_start"|"drug_stop"|"dose_change"|"lab_result"|"diagnosis"|"procedure"
    date: datetime
    description: str
    drug: Optional[str] = None
    lab_name: Optional[str] = None
    lab_value: Optional[float] = None
    lab_unit: Optional[str] = None
    icd10_code: Optional[str] = None

@dataclass
class ClinicalTimeline:
    patient_id: str
    events: list[TimelineEvent] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def add_event(self, event: TimelineEvent) -> None:
        self.events.append(event)
        self.events.sort(key=lambda e: e.date)

    def get_current_medications(self) -> list[str]:
        """Return drugs currently active (started but not stopped)."""
        started: set[str] = set()
        stopped: set[str] = set()
        for event in self.events:
            if event.drug:
                if event.event_type == "drug_start":
                    started.add(event.drug.lower())
                elif event.event_type == "drug_stop":
                    stopped.add(event.drug.lower())
        return list(started - stopped)

    def get_lab_trend(self, lab_name: str, last_n: int = 5) -> list[tuple[datetime, float]]:
        """Get recent lab trend for a specific test."""
        lab_lower = lab_name.lower()
        results = [
            (e.date, e.lab_value)
            for e in self.events
            if e.event_type == "lab_result"
            and e.lab_name and lab_lower in e.lab_name.lower()
            and e.lab_value is not None
        ]
        return sorted(results, key=lambda x: x[0])[-last_n:]

    def get_latest_egfr(self) -> Optional[float]:
        trend = self.get_lab_trend("egfr")
        return trend[-1][1] if trend else None

    def get_latest_k(self) -> Optional[float]:
        trend = self.get_lab_trend("potassium") or self.get_lab_trend("k+")
        return trend[-1][1] if trend else None

    def summarize(self) -> str:
        meds = self.get_current_medications()
        egfr = self.get_latest_egfr()
        k = self.get_latest_k()
        lines = [f"Timeline: {len(self.events)} events"]
        if meds: lines.append(f"Current medications: {', '.join(meds)}")
        if egfr: lines.append(f"Latest eGFR: {egfr:.0f} mL/min/1.73m²")
        if k: lines.append(f"Latest K+: {k:.1f} mmol/L")
        return " | ".join(lines)


class TimeAwareClinicalTimelineBuilder:
    """L3-6: Builds and queries patient clinical timelines."""

    def build_from_fhir(self, fhir_bundle: dict) -> ClinicalTimeline:
        """Parse a FHIR Bundle into a ClinicalTimeline."""
        patient_id = "unknown"
        timeline = ClinicalTimeline(patient_id=patient_id)

        entries = fhir_bundle.get("entry", [])
        for entry in entries:
            resource = entry.get("resource", {})
            rt = resource.get("resourceType", "")

            if rt == "MedicationRequest":
                self._parse_medication_request(resource, timeline)
            elif rt == "Observation":
                self._parse_observation(resource, timeline)
            elif rt == "Condition":
                self._parse_condition(resource, timeline)

        return timeline

    def _parse_medication_request(self, resource: dict, timeline: ClinicalTimeline) -> None:
        med_code = resource.get("medicationCodeableConcept", {}).get("text", "")
        status = resource.get("status", "active")
        authored_on = resource.get("authoredOn", "")
        try:
            date = datetime.fromisoformat(authored_on.replace("Z", "+00:00")) if authored_on else datetime.now(timezone.utc)
        except ValueError:
            date = datetime.now(timezone.utc)
        event_type = "drug_start" if status == "active" else "drug_stop"
        if med_code:
            timeline.add_event(TimelineEvent(
                event_type=event_type,
                date=date,
                description=f"{event_type.replace('_', ' ').title()}: {med_code}",
                drug=med_code,
            ))

    def _parse_observation(self, resource: dict, timeline: ClinicalTimeline) -> None:
        code_text = resource.get("code", {}).get("text", "")
        value_quantity = resource.get("valueQuantity", {})
        value = value_quantity.get("value")
        unit = value_quantity.get("unit", "")
        effective_date = resource.get("effectiveDateTime", "")
        try:
            date = datetime.fromisoformat(effective_date.replace("Z", "+00:00")) if effective_date else datetime.now(timezone.utc)
        except ValueError:
            date = datetime.now(timezone.utc)
        if code_text and value is not None:
            timeline.add_event(TimelineEvent(
                event_type="lab_result",
                date=date,
                description=f"{code_text}: {value} {unit}",
                lab_name=code_text,
                lab_value=float(value),
                lab_unit=unit,
            ))

    def _parse_condition(self, resource: dict, timeline: ClinicalTimeline) -> None:
        condition_text = resource.get("code", {}).get("text", "")
        onset = resource.get("onsetDateTime", "")
        icd10 = ""
        for coding in resource.get("code", {}).get("coding", []):
            if "icd" in coding.get("system", "").lower():
                icd10 = coding.get("code", "")
                break
        try:
            date = datetime.fromisoformat(onset.replace("Z", "+00:00")) if onset else datetime.now(timezone.utc)
        except ValueError:
            date = datetime.now(timezone.utc)
        if condition_text:
            timeline.add_event(TimelineEvent(
                event_type="diagnosis",
                date=date,
                description=f"Diagnosis: {condition_text}",
                icd10_code=icd10,
            ))
