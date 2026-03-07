"""
CURANIQ — Medical Evidence Operating System
Layer 2: Evidence Knowledge & Synthesis

L2-1  Ontology Normalizer — RxNorm, SNOMED CT, ICD-10, LOINC
L2-15 Multi-Language Drug Name Resolver

Architecture requirements:
- RxNorm (drugs), SNOMED CT (clinical terms), ICD-10 (diagnoses), LOINC (labs)
- Monthly NLM sync
- Deterministic mapping: paracetamol = acetaminophen, adrenaline = epinephrine
- Critical for multilingual markets (UK/US/CIS terminology divergence)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# L2-15: MULTI-LANGUAGE DRUG NAME RESOLVER
# Deterministic lookup table — UK/US/CIS/Generic/INN normalization
# Architecture: 'Standalone lookup table in P1'
# Critical for Uzbekistan/CIS deployment where Russian brand names differ
# ─────────────────────────────────────────────────────────────────────────────

# Format: normalized_inn → {variant: canonical_name}
# All variants map to the INN (International Nonproprietary Name) as canonical
DRUG_NAME_VARIANTS: dict[str, dict[str, str]] = {
    # Analgesics / Antipyretics
    "paracetamol": {
        "inn": "paracetamol",
        "us": "acetaminophen",
        "uk": "paracetamol",
        "brand_us": "tylenol",
        "brand_uk": "panadol",
        "brand_cis": "панадол",
        "russian": "парацетамол",
        "uzbek": "paratsetamol",
        "cis_brand": "эффералган",
    },
    "ibuprofen": {
        "inn": "ibuprofen",
        "us": "ibuprofen",
        "uk": "ibuprofen",
        "brand_us": "advil",
        "brand_uk": "nurofen",
        "brand_cis": "нурофен",
        "russian": "ибупрофен",
        "uzbek": "ibuprofen",
    },
    "diclofenac": {
        "inn": "diclofenac",
        "us": "diclofenac",
        "uk": "diclofenac",
        "brand_uk": "voltarol",
        "brand_cis": "вольтарен",
        "russian": "диклофенак",
        "uzbek": "diklofenak",
    },
    # Cardiovascular
    "adrenaline": {
        "inn": "epinephrine",
        "us": "epinephrine",
        "uk": "adrenaline",
        "brand": "epipen",
        "russian": "адреналин",
        "uzbek": "adrenalin",
    },
    "noradrenaline": {
        "inn": "norepinephrine",
        "us": "norepinephrine",
        "uk": "noradrenaline",
        "russian": "норадреналин",
    },
    "frusemide": {
        "inn": "furosemide",
        "us": "furosemide",
        "uk": "furosemide",
        "legacy_uk": "frusemide",
        "brand_us": "lasix",
        "brand_cis": "лазикс",
        "russian": "фуросемид",
        "uzbek": "furosemid",
    },
    "atenolol": {
        "inn": "atenolol",
        "us": "atenolol",
        "uk": "atenolol",
        "brand": "tenormin",
        "russian": "атенолол",
    },
    "metoprolol": {
        "inn": "metoprolol",
        "us": "metoprolol",
        "uk": "metoprolol",
        "brand_us": "lopressor",
        "brand_cis": "беталок",
        "russian": "метопролол",
    },
    "lisinopril": {
        "inn": "lisinopril",
        "us": "lisinopril",
        "uk": "lisinopril",
        "brand_us": "zestril",
        "brand_cis": "диротон",
        "russian": "лизиноприл",
    },
    "amlodipine": {
        "inn": "amlodipine",
        "us": "amlodipine",
        "uk": "amlodipine",
        "brand_us": "norvasc",
        "brand_cis": "норваск",
        "russian": "амлодипин",
    },
    "warfarin": {
        "inn": "warfarin",
        "us": "warfarin",
        "uk": "warfarin",
        "brand_us": "coumadin",
        "brand_uk": "marevan",
        "russian": "варфарин",
    },
    "aspirin": {
        "inn": "acetylsalicylic acid",
        "us": "aspirin",
        "uk": "aspirin",
        "generic": "acetylsalicylic acid",
        "brand": "aspirin",
        "brand_cis": "аспирин",
        "russian": "аспирин",
        "uzbek": "aspirin",
    },
    # Antibiotics
    "amoxicillin": {
        "inn": "amoxicillin",
        "us": "amoxicillin",
        "uk": "amoxicillin",
        "brand_us": "amoxil",
        "brand_cis": "флемоксин",
        "russian": "амоксициллин",
        "uzbek": "amoksitsillin",
    },
    "co-amoxiclav": {
        "inn": "amoxicillin/clavulanic acid",
        "us": "amoxicillin-clavulanate",
        "uk": "co-amoxiclav",
        "brand_us": "augmentin",
        "brand_uk": "augmentin",
        "brand_cis": "аугментин",
        "russian": "амоксиклав",
        "uzbek": "amoksiklav",
    },
    "ciprofloxacin": {
        "inn": "ciprofloxacin",
        "us": "ciprofloxacin",
        "uk": "ciprofloxacin",
        "brand_us": "cipro",
        "brand_cis": "ципрофлоксацин",
        "russian": "ципрофлоксацин",
        "uzbek": "siprofloksatsin",
    },
    "metronidazole": {
        "inn": "metronidazole",
        "us": "metronidazole",
        "uk": "metronidazole",
        "brand_us": "flagyl",
        "brand_cis": "метронидазол",
        "russian": "метронидазол",
    },
    "trimethoprim": {
        "inn": "trimethoprim",
        "us": "trimethoprim",
        "uk": "trimethoprim",
        "co_formulation_us": "cotrimoxazole",
        "co_formulation_uk": "co-trimoxazole",
        "brand": "bactrim",
    },
    # Endocrinology
    "metformin": {
        "inn": "metformin",
        "us": "metformin",
        "uk": "metformin",
        "brand_us": "glucophage",
        "brand_cis": "глюкофаж",
        "russian": "метформин",
        "uzbek": "metformin",
    },
    "glibenclamide": {
        "inn": "glibenclamide",
        "us": "glyburide",
        "uk": "glibenclamide",
        "brand_us": "diabeta",
        "russian": "глибенкламид",
    },
    "levothyroxine": {
        "inn": "levothyroxine",
        "us": "levothyroxine",
        "uk": "levothyroxine",
        "brand_us": "synthroid",
        "brand_uk": "levothyroxine",
        "brand_cis": "L-тироксин",
        "russian": "левотироксин",
    },
    # Respiratory
    "salbutamol": {
        "inn": "salbutamol",
        "us": "albuterol",
        "uk": "salbutamol",
        "brand_uk": "ventolin",
        "brand_us": "proventil",
        "brand_cis": "вентолин",
        "russian": "сальбутамол",
        "uzbek": "salbutamol",
    },
    "beclometasone": {
        "inn": "beclometasone",
        "us": "beclomethasone",
        "uk": "beclometasone",
        "brand_uk": "clenil",
        "brand_us": "qvar",
        "russian": "беклометазон",
    },
    "prednisolone": {
        "inn": "prednisolone",
        "us": "prednisolone",
        "uk": "prednisolone",
        "us_equivalent": "prednisone",  # Prodrug; prednisolone is active form
        "brand_cis": "преднизолон",
        "russian": "преднизолон",
    },
    # Neurology / Psychiatry
    "diazepam": {
        "inn": "diazepam",
        "us": "diazepam",
        "uk": "diazepam",
        "brand_us": "valium",
        "brand_cis": "реланиум",
        "russian": "диазепам",
        "uzbek": "diazepam",
    },
    "haloperidol": {
        "inn": "haloperidol",
        "us": "haloperidol",
        "uk": "haloperidol",
        "brand_us": "haldol",
        "brand_cis": "галоперидол",
        "russian": "галоперидол",
    },
    "sertraline": {
        "inn": "sertraline",
        "us": "sertraline",
        "uk": "sertraline",
        "brand_us": "zoloft",
        "brand_uk": "lustral",
        "brand_cis": "золофт",
        "russian": "сертралин",
    },
    "amitriptyline": {
        "inn": "amitriptyline",
        "us": "amitriptyline",
        "uk": "amitriptyline",
        "brand_cis": "амитриптилин",
        "russian": "амитриптилин",
    },
    "carbamazepine": {
        "inn": "carbamazepine",
        "us": "carbamazepine",
        "uk": "carbamazepine",
        "brand_us": "tegretol",
        "brand_cis": "финлепсин",
        "russian": "карбамазепин",
        "uzbek": "karbamazepin",
    },
    # Anticoagulants
    "heparin": {
        "inn": "heparin",
        "us": "heparin",
        "uk": "heparin",
        "brand_cis": "гепарин",
        "russian": "гепарин",
    },
    "enoxaparin": {
        "inn": "enoxaparin",
        "us": "enoxaparin",
        "uk": "enoxaparin",
        "brand": "clexane",
        "brand_us": "lovenox",
        "brand_cis": "клексан",
        "russian": "эноксапарин",
    },
    # Gastrointestinal
    "omeprazole": {
        "inn": "omeprazole",
        "us": "omeprazole",
        "uk": "omeprazole",
        "brand_us": "prilosec",
        "brand_uk": "losec",
        "brand_cis": "омез",
        "russian": "омепразол",
        "uzbek": "omeprazol",
    },
    # Statins
    "atorvastatin": {
        "inn": "atorvastatin",
        "us": "atorvastatin",
        "uk": "atorvastatin",
        "brand_us": "lipitor",
        "brand_cis": "липримар",
        "russian": "аторвастатин",
    },
    "simvastatin": {
        "inn": "simvastatin",
        "us": "simvastatin",
        "uk": "simvastatin",
        "brand_us": "zocor",
        "brand_cis": "зокор",
        "russian": "симвастатин",
    },
    # Renal / Diabetes
    "spironolactone": {
        "inn": "spironolactone",
        "us": "spironolactone",
        "uk": "spironolactone",
        "brand_us": "aldactone",
        "brand_cis": "альдактон",
        "russian": "спиронолактон",
    },
    # Opioids
    "morphine": {
        "inn": "morphine",
        "us": "morphine",
        "uk": "morphine",
        "brand_cis": "морфин",
        "russian": "морфин",
    },
    "tramadol": {
        "inn": "tramadol",
        "us": "tramadol",
        "uk": "tramadol",
        "brand_us": "ultram",
        "brand_cis": "трамадол",
        "russian": "трамадол",
        "uzbek": "tramadol",
    },
    # Vitamins / Supplements
    "cyanocobalamin": {
        "inn": "cyanocobalamin",
        "us": "vitamin b12",
        "uk": "cyanocobalamin",
        "common": "vitamin b-12",
        "russian": "цианокобаламин",
    },
    "cholecalciferol": {
        "inn": "colecalciferol",
        "uk": "colecalciferol",
        "us": "cholecalciferol",
        "common": "vitamin d3",
        "russian": "холекальциферол",
    },
}


def _build_reverse_lookup() -> dict[str, str]:
    """Build reverse lookup: any variant name → canonical INN."""
    reverse: dict[str, str] = {}
    for canonical, variants in DRUG_NAME_VARIANTS.items():
        inn = variants.get("inn", canonical)
        # Map canonical to INN
        reverse[canonical.lower()] = inn
        # Map all variants to INN
        for variant_name, variant_value in variants.items():
            if variant_name != "inn":
                reverse[variant_value.lower()] = inn
    return reverse


# Pre-built reverse lookup for O(1) normalization
_REVERSE_DRUG_LOOKUP: dict[str, str] = _build_reverse_lookup()


def resolve_drug_name(name: str) -> tuple[str, bool]:
    """
    L2-15: Resolve any drug name to its canonical INN.
    
    Returns (canonical_inn, was_resolved).
    If not found: returns (original_name, False).
    
    Examples:
        resolve_drug_name("acetaminophen") → ("paracetamol", True)
        resolve_drug_name("albuterol") → ("salbutamol", True)
        resolve_drug_name("Tylenol") → ("paracetamol", True)
        resolve_drug_name("Панадол") → ("paracetamol", True)
        resolve_drug_name("unknown_drug") → ("unknown_drug", False)
    """
    normalized = name.strip().lower()

    # Direct lookup
    if normalized in _REVERSE_DRUG_LOOKUP:
        return _REVERSE_DRUG_LOOKUP[normalized], True

    # Fuzzy: remove common suffixes (hydrochloride, sodium, etc.)
    cleaned = re.sub(
        r'\s+(hydrochloride|hcl|sodium|potassium|citrate|maleate|tartrate|sulfate|'
        r'mesylate|tosylate|besylate|fumarate|succinate|acetate|phosphate)\s*$',
        '', normalized
    )
    if cleaned != normalized and cleaned in _REVERSE_DRUG_LOOKUP:
        return _REVERSE_DRUG_LOOKUP[cleaned], True

    # Try partial match for common drug stems
    for variant, inn in _REVERSE_DRUG_LOOKUP.items():
        if len(normalized) >= 6 and (normalized in variant or variant in normalized):
            return inn, True

    return name, False


def get_all_variants(inn: str) -> dict[str, str]:
    """
    Get all known names for a drug given its INN.
    Returns empty dict if INN not found.
    """
    inn_lower = inn.lower()
    # Search by canonical key
    if inn_lower in DRUG_NAME_VARIANTS:
        return DRUG_NAME_VARIANTS[inn_lower]
    # Search by INN value
    for canonical, variants in DRUG_NAME_VARIANTS.items():
        if variants.get("inn", "").lower() == inn_lower:
            return variants
    return {}


def get_search_synonyms(drug_name: str) -> list[str]:
    """
    Get all synonyms for a drug to use in multi-language evidence search.
    Ensures UK, US, CIS, and INN variants are all searched.
    
    Critical for evidence retrieval: a PubMed search for "acetaminophen"
    won't find UK studies using "paracetamol" unless synonyms are included.
    """
    canonical, resolved = resolve_drug_name(drug_name)
    if not resolved:
        return [drug_name]

    variants = get_all_variants(canonical)
    if not variants:
        return [drug_name, canonical]

    # Priority order: INN, US name, UK name, common brand
    synonyms = []
    priority_keys = ["inn", "us", "uk", "generic", "common"]
    for key in priority_keys:
        if key in variants and variants[key] not in synonyms:
            synonyms.append(variants[key])

    # Add original name if not already included
    if drug_name.lower() not in [s.lower() for s in synonyms]:
        synonyms.insert(0, drug_name)

    return synonyms[:6]  # Limit to 6 synonyms for query efficiency


# ─────────────────────────────────────────────────────────────────────────────
# L2-1: ONTOLOGY NORMALIZER
# RxNorm, SNOMED CT, ICD-10, LOINC normalization
# Production: uses NLM APIs with monthly sync
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class OntologyMapping:
    """A normalized concept with ontology codes."""
    original_term: str
    canonical_term: str
    rxcui: Optional[str] = None          # RxNorm Concept Unique Identifier
    snomed_code: Optional[str] = None    # SNOMED CT code
    icd10_code: Optional[str] = None     # ICD-10 code
    icd10_description: Optional[str] = None
    loinc_code: Optional[str] = None     # LOINC code (for labs)
    ontology_version: str = "2025-01"    # NLM sync version
    confidence: float = 1.0


# Curated ontology lookup for common clinical terms
# Production: augmented by monthly NLM API sync
SNOMED_LOOKUP: dict[str, tuple[str, str]] = {
    # Conditions: term → (snomed_code, canonical_name)
    "hypertension":         ("38341003",  "Hypertension"),
    "diabetes mellitus":    ("73211009",  "Diabetes mellitus"),
    "type 2 diabetes":      ("44054006",  "Type 2 diabetes mellitus"),
    "type 1 diabetes":      ("46635009",  "Type 1 diabetes mellitus"),
    "heart failure":        ("84114007",  "Heart failure"),
    "atrial fibrillation":  ("49436004",  "Atrial fibrillation"),
    "myocardial infarction":("22298006",  "Myocardial infarction"),
    "stroke":               ("230690007", "Cerebrovascular accident"),
    "chronic kidney disease":("709044004","Chronic kidney disease"),
    "asthma":               ("195967001", "Asthma"),
    "copd":                 ("13645005",  "Chronic obstructive lung disease"),
    "pneumonia":            ("233604007", "Pneumonia"),
    "sepsis":               ("91302008",  "Sepsis"),
    "hypothyroidism":       ("40930008",  "Hypothyroidism"),
    "hyperthyroidism":      ("34486009",  "Hyperthyroidism"),
    "epilepsy":             ("84757009",  "Epilepsy"),
    "depression":           ("35489007",  "Depressive disorder"),
    "anxiety":              ("48694002",  "Anxiety"),
    "anemia":               ("271737000", "Anaemia"),
    "acute kidney injury":  ("14669001",  "Acute renal failure syndrome"),
    "liver cirrhosis":      ("19943007",  "Cirrhosis of liver"),
    "gout":                 ("90560007",  "Gout"),
    "rheumatoid arthritis": ("69896004",  "Rheumatoid arthritis"),
    "osteoporosis":         ("64859006",  "Osteoporosis"),
    "breast cancer":        ("254837009", "Malignant neoplasm of breast"),
    "lung cancer":          ("363358000", "Malignant tumor of lung"),
    "colorectal cancer":    ("363346000", "Malignant neoplasm of colorectum"),
    "uti":                  ("68566005",  "Urinary tract infectious disease"),
    "pregnancy":            ("77386006",  "Pregnancy"),
    "anaphylaxis":          ("39579001",  "Anaphylaxis"),
}

ICD10_LOOKUP: dict[str, tuple[str, str]] = {
    # term → (icd10_code, description)
    "hypertension":          ("I10",   "Essential (primary) hypertension"),
    "type 2 diabetes":       ("E11",   "Type 2 diabetes mellitus"),
    "type 1 diabetes":       ("E10",   "Type 1 diabetes mellitus"),
    "heart failure":         ("I50",   "Heart failure"),
    "atrial fibrillation":   ("I48",   "Atrial fibrillation and flutter"),
    "myocardial infarction": ("I21",   "Acute myocardial infarction"),
    "stroke":                ("I63",   "Cerebral infarction"),
    "ckd":                   ("N18",   "Chronic kidney disease"),
    "asthma":                ("J45",   "Asthma"),
    "copd":                  ("J44",   "COPD"),
    "pneumonia":             ("J18",   "Pneumonia, unspecified"),
    "sepsis":                ("A41",   "Other sepsis"),
    "hypothyroidism":        ("E03",   "Other hypothyroidism"),
    "hyperthyroidism":       ("E05",   "Thyrotoxicosis"),
    "epilepsy":              ("G40",   "Epilepsy"),
    "depression":            ("F32",   "Depressive episode"),
    "anemia":                ("D64",   "Other anaemias"),
    "acute kidney injury":   ("N17",   "Acute kidney failure"),
    "liver cirrhosis":       ("K74",   "Fibrosis and cirrhosis of liver"),
    "gout":                  ("M10",   "Gout"),
    "rheumatoid arthritis":  ("M05",   "Seropositive rheumatoid arthritis"),
    "osteoporosis":          ("M81",   "Osteoporosis without pathological fracture"),
    "uti":                   ("N39.0", "Urinary tract infection"),
    "anaphylaxis":           ("T78.2", "Anaphylactic shock, unspecified"),
}

LOINC_LOOKUP: dict[str, tuple[str, str]] = {
    # lab term → (loinc_code, canonical_name)
    "creatinine":          ("2160-0",  "Creatinine [Mass/volume] in Serum or Plasma"),
    "egfr":                ("69405-9", "GFR/BSA predicted by Creatinine-based formula"),
    "potassium":           ("2823-3",  "Potassium [Moles/volume] in Serum or Plasma"),
    "sodium":              ("2951-2",  "Sodium [Moles/volume] in Serum or Plasma"),
    "haemoglobin":         ("718-7",   "Hemoglobin [Mass/volume] in Blood"),
    "hemoglobin":          ("718-7",   "Hemoglobin [Mass/volume] in Blood"),
    "hba1c":               ("4548-4",  "Hemoglobin A1c/Hemoglobin.total in Blood"),
    "tsh":                 ("3016-3",  "Thyrotropin [Units/volume] in Serum or Plasma"),
    "inr":                 ("6301-6",  "INR in Platelet poor plasma by Coagulation assay"),
    "aptt":                ("3173-2",  "aPTT in Platelet poor plasma by Coagulation assay"),
    "alt":                 ("1742-6",  "ALT [Enzymatic activity/volume] in Serum or Plasma"),
    "ast":                 ("1920-8",  "AST [Enzymatic activity/volume] in Serum or Plasma"),
    "bilirubin":           ("1975-2",  "Bilirubin.total [Mass/volume] in Serum or Plasma"),
    "albumin":             ("1751-7",  "Albumin [Mass/volume] in Serum or Plasma"),
    "cholesterol":         ("2093-3",  "Cholesterol [Mass/volume] in Serum or Plasma"),
    "ldl":                 ("13457-7", "Cholesterol in LDL [Mass/volume] in Serum or Plasma"),
    "hdl":                 ("2085-9",  "Cholesterol in HDL [Mass/volume] in Serum or Plasma"),
    "triglycerides":       ("2571-8",  "Triglyceride [Mass/volume] in Serum or Plasma"),
    "glucose":             ("2345-7",  "Glucose [Mass/volume] in Serum or Plasma"),
    "urea":                ("3091-6",  "Urea [Moles/volume] in Serum or Plasma"),
    "bun":                 ("3094-0",  "Urea nitrogen [Mass/volume] in Serum or Plasma"),
    "uric acid":           ("3084-1",  "Urate [Mass/volume] in Serum or Plasma"),
    "digoxin":             ("10535-3", "Digoxin [Mass/volume] in Serum or Plasma"),
    "lithium":             ("13376-9", "Lithium [Moles/volume] in Serum or Plasma"),
    "vancomycin":          ("4092-3",  "Vancomycin [Mass/volume] in Serum or Plasma"),
    "gentamicin":          ("3665-7",  "Gentamicin [Mass/volume] in Serum or Plasma"),
    "phenytoin":           ("3869-5",  "Phenytoin [Mass/volume] in Serum or Plasma"),
    "valproate":           ("4057-6",  "Valproate [Mass/volume] in Serum or Plasma"),
    "tacrolimus":          ("35548-5", "Tacrolimus [Mass/volume] in Blood"),
    "bnp":                 ("33762-6", "Natriuretic peptide.B prohormone N-Terminal"),
    "troponin":            ("10839-9", "Troponin I.cardiac [Mass/volume] in Serum or Plasma"),
    "d-dimer":             ("48065-7", "Fibrin D-dimer DDU [Mass/volume] in Platelet poor plasma"),
    "psa":                 ("2857-1",  "PSA [Mass/volume] in Serum or Plasma"),
    "ck":                  ("2157-6",  "Creatine kinase [Enzymatic activity/volume] in Serum"),
    "ldh":                 ("2532-0",  "Lactate dehydrogenase [Enzymatic activity/volume]"),
    "ferritin":            ("2276-4",  "Ferritin [Mass/volume] in Serum or Plasma"),
    "iron":                ("2498-4",  "Iron [Mass/volume] in Serum or Plasma"),
    "b12":                 ("2132-9",  "Cobalamin (B12) [Mass/volume] in Serum or Plasma"),
    "folate":              ("2284-8",  "Folate [Mass/volume] in Serum or Plasma"),
    "magnesium":           ("19123-9", "Magnesium [Mass/volume] in Serum or Plasma"),
    "phosphate":           ("2777-1",  "Phosphate [Mass/volume] in Serum or Plasma"),
    "calcium":             ("17861-6", "Calcium [Mass/volume] in Serum or Plasma"),
    "pth":                 ("2731-8",  "Parathyrin [Mass/volume] in Serum"),
    "vitamin d":           ("14635-7", "25-hydroxyvitamin D3 [Mass/volume] in Serum or Plasma"),
    "crp":                 ("1988-5",  "C reactive protein [Mass/volume] in Serum or Plasma"),
    "esr":                 ("4537-7",  "Erythrocyte sedimentation rate by Westergren method"),
    "procalcitonin":       ("33959-8", "Procalcitonin [Mass/volume] in Serum or Plasma"),
    "blood culture":       ("600-7",   "Bacteria identified in Blood by Culture"),
    "urine culture":       ("630-4",   "Bacteria identified in Urine by Culture"),
}

# RxCUI lookup for common drugs (production: full NLM API)
RXCUI_LOOKUP: dict[str, str] = {
    "paracetamol":    "161",
    "acetaminophen":  "161",
    "ibuprofen":      "5640",
    "amoxicillin":    "723",
    "metformin":      "6809",
    "atorvastatin":   "83367",
    "simvastatin":    "36567",
    "lisinopril":     "29046",
    "amlodipine":     "17767",
    "metoprolol":     "41493",
    "atenolol":       "1202",
    "warfarin":       "11289",
    "aspirin":        "1191",
    "omeprazole":     "7646",
    "salbutamol":     "2103",
    "albuterol":      "2103",
    "ciprofloxacin":  "2551",
    "metronidazole":  "6922",
    "diazepam":       "3322",
    "sertraline":     "36437",
    "carbamazepine":  "2002",
    "levothyroxine":  "10582",
    "furosemide":     "4603",
    "spironolactone": "9997",
    "enoxaparin":     "67108",
    "heparin":        "5224",
    "morphine":       "7052",
    "tramadol":       "41493",
    "prednisolone":   "8638",
    "dexamethasone":  "3264",
    "haloperidol":    "5134",
    "amitriptyline":  "704",
}


class OntologyNormalizer:
    """
    L2-1: Ontology normalizer for all clinical terms.
    
    Normalizes drug names, clinical conditions, diagnoses, and lab values
    to their canonical ontology codes.
    
    Production: uses monthly NLM API sync with RxNorm, SNOMED CT,
    ICD-10, LOINC web services. This implementation uses the
    curated lookup tables above, augmented by L2-15 drug name resolver.
    """

    def normalize_drug(self, drug_name: str) -> OntologyMapping:
        """Normalize a drug name to INN + RxCUI."""
        canonical, resolved = resolve_drug_name(drug_name)
        rxcui = RXCUI_LOOKUP.get(canonical.lower())

        return OntologyMapping(
            original_term=drug_name,
            canonical_term=canonical,
            rxcui=rxcui,
            confidence=1.0 if resolved else 0.7,
        )

    def normalize_condition(self, condition: str) -> OntologyMapping:
        """Normalize a clinical condition to SNOMED CT + ICD-10."""
        condition_lower = condition.strip().lower()

        # Direct lookup
        snomed = SNOMED_LOOKUP.get(condition_lower)
        icd10 = ICD10_LOOKUP.get(condition_lower)

        # Fuzzy match
        if not snomed and not icd10:
            for key in SNOMED_LOOKUP:
                if key in condition_lower or condition_lower in key:
                    snomed = SNOMED_LOOKUP[key]
                    icd10 = ICD10_LOOKUP.get(key)
                    break

        canonical = snomed[1] if snomed else condition

        return OntologyMapping(
            original_term=condition,
            canonical_term=canonical,
            snomed_code=snomed[0] if snomed else None,
            icd10_code=icd10[0] if icd10 else None,
            icd10_description=icd10[1] if icd10 else None,
            confidence=1.0 if snomed else 0.5,
        )

    def normalize_lab(self, lab_name: str) -> OntologyMapping:
        """Normalize a lab test name to LOINC code."""
        lab_lower = lab_name.strip().lower()

        loinc = LOINC_LOOKUP.get(lab_lower)

        if not loinc:
            for key in LOINC_LOOKUP:
                if key in lab_lower or lab_lower in key:
                    loinc = LOINC_LOOKUP[key]
                    break

        return OntologyMapping(
            original_term=lab_name,
            canonical_term=loinc[1] if loinc else lab_name,
            loinc_code=loinc[0] if loinc else None,
            confidence=1.0 if loinc else 0.5,
        )

    def expand_query_terms(self, query: str) -> list[str]:
        """
        Expand a query with ontology synonyms for better evidence retrieval.
        Replaces US drug names with INN + UK equivalents, etc.
        Returns list of expanded query terms.
        """
        expanded = [query]
        words = re.findall(r'\b[a-zA-Z]{4,}\b', query)

        for word in words:
            # Try drug name expansion
            canonical, resolved = resolve_drug_name(word)
            if resolved:
                synonyms = get_search_synonyms(canonical)
                for syn in synonyms:
                    if syn.lower() != word.lower():
                        expanded.append(query.replace(word, syn))

            # Try condition expansion
            mapping = self.normalize_condition(word)
            if mapping.snomed_code and mapping.canonical_term != word:
                expanded.append(query.replace(word, mapping.canonical_term))

        return list(dict.fromkeys(expanded))[:10]  # deduplicated, max 10
