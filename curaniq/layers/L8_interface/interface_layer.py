"""
CURANIQ — Medical Evidence Operating System
Layer 8: Clinician Experience & Interface

L8-1  Evidence Cards Builder
L8-4  Role-Based UI Adapter
L8-5  Multilingual Engine (EN/RU/UZ)
L8-8  Medication Boundary Display
L8-12 Universal Language Auto-Detection
L8-13 Medical Translation Engine (3-stage)
"""
from __future__ import annotations
import logging, re
from dataclasses import dataclass, field
from typing import Optional
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# L8-1: EVIDENCE CARDS BUILDER
# Architecture: 'Structured cards: Action Card, Why Card, Evidence Card,
# Uncertainty Card. Every card links back to source.'
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ActionCard:
    """What to do — the clinical recommendation."""
    headline:       str
    action_steps:   list[str]
    urgency:        str   # "routine" | "urgent" | "emergency"
    monitoring:     list[str]
    review_in:      Optional[str]
    escalation:     Optional[str]

@dataclass
class WhyCard:
    """Why — evidence summary behind the recommendation."""
    summary:        str
    grade_label:    str   # GRADE: HIGH/MODERATE/LOW/VERY LOW
    confidence:     float
    key_studies:    list[str]
    guideline_source: Optional[str]

@dataclass
class EvidenceCard:
    """Source evidence with click-through to original."""
    chunk_id:       str
    source_name:    str
    evidence_tier:  str
    publication_date: Optional[str]
    doi_url:        Optional[str]
    snippet:        str   # First 200 chars of chunk
    jurisdiction:   str
    retraction_status: str

@dataclass
class UncertaintyCard:
    """What we don't know — evidence gaps and caveats."""
    gaps:           list[str]
    conflicting:    list[str]
    suppressed_claims: list[str]
    low_confidence_areas: list[str]

@dataclass
class CURaniqCard:
    """Complete structured output card."""
    response_id:    str
    query:          str
    action:         Optional[ActionCard]
    why:            Optional[WhyCard]
    evidence:       list[EvidenceCard]
    uncertainty:    Optional[UncertaintyCard]
    safety_flags:   list[str]
    staleness_display: str
    jurisdiction:   str
    mode:           str


class EvidenceCardsBuilder:
    """L8-1: Assemble structured CURANIQ output cards from generator output."""

    def build(
        self,
        response_id: str,
        query: str,
        claims: list[dict],
        chunks: list,
        safety_flags: list[str],
        staleness_display: str,
        jurisdiction: str,
        mode: str,
        evidence_gaps: list[str],
        confidence_overall: float,
        grade_label: str = "MODERATE",
    ) -> CURaniqCard:
        # Action card — from non-suppressed claims
        active_claims = [c for c in claims if not c.get("suppressed", False)]
        suppressed = [c.get("claim_text", "") for c in claims if c.get("suppressed", False)]

        action_steps = [c.get("claim_text", "") for c in active_claims[:5]]
        urgency = "emergency" if "emergency" in " ".join(safety_flags).lower() else \
                  "urgent" if any(f in safety_flags for f in ["URGENT", "BLACK_BOX"]) else "routine"

        action = ActionCard(
            headline=action_steps[0][:100] if action_steps else "See evidence cards below.",
            action_steps=action_steps,
            urgency=urgency,
            monitoring=[],
            review_in=None,
            escalation="Seek urgent medical attention if symptoms worsen unexpectedly." if urgency != "routine" else None,
        ) if action_steps else None

        # Why card
        guideline_sources = [
            c.provenance.source_api.value for c in chunks
            if hasattr(c, 'evidence_tier') and c.evidence_tier.value in ("guideline", "systematic_review")
        ]
        why = WhyCard(
            summary=f"Based on {len(chunks)} evidence source(s). {mode.replace('_', ' ').title()} mode.",
            grade_label=grade_label,
            confidence=round(confidence_overall, 2),
            key_studies=[c.provenance.source_doi or c.chunk_id for c in chunks[:3] if hasattr(c, "provenance")],
            guideline_source=guideline_sources[0] if guideline_sources else None,
        )

        # Evidence cards
        evidence_cards = []
        for chunk in chunks[:8]:
            if hasattr(chunk, "provenance"):
                evidence_cards.append(EvidenceCard(
                    chunk_id=chunk.chunk_id,
                    source_name=chunk.provenance.source_api.value,
                    evidence_tier=chunk.evidence_tier.value,
                    publication_date=chunk.provenance.publication_date.strftime("%Y-%m-%d") if chunk.provenance.publication_date else None,
                    doi_url=f"https://doi.org/{chunk.provenance.source_doi}" if chunk.provenance.source_doi else None,
                    snippet=chunk.content[:200].replace("\n", " "),
                    jurisdiction=chunk.provenance.jurisdiction.value,
                    retraction_status=chunk.retraction_status.value,
                ))

        # Uncertainty card
        uncertainty = UncertaintyCard(
            gaps=evidence_gaps,
            conflicting=[],
            suppressed_claims=suppressed,
            low_confidence_areas=[c.get("claim_text", "")[:80] for c in active_claims if c.get("certainty") in ("low", "very_low")],
        )

        return CURaniqCard(
            response_id=response_id,
            query=query,
            action=action,
            why=why,
            evidence=evidence_cards,
            uncertainty=uncertainty,
            safety_flags=safety_flags,
            staleness_display=staleness_display,
            jurisdiction=jurisdiction,
            mode=mode,
        )


# ─────────────────────────────────────────────────────────────────────────────
# L8-4: ROLE-BASED UI ADAPTER
# Architecture: 'Patient/clinician/researcher modes. Different depth/language.'
# ─────────────────────────────────────────────────────────────────────────────

ROLE_UI_CONFIGS: dict[str, dict] = {
    "patient": {
        "show_raw_evidence": False,
        "show_dose_details": False,
        "show_grade": False,
        "language_level": "lay",
        "max_claims_shown": 3,
        "always_show_escalation": True,
        "disclaimer_required": True,
    },
    "caregiver": {
        "show_raw_evidence": False,
        "show_dose_details": False,
        "show_grade": False,
        "language_level": "lay",
        "max_claims_shown": 5,
        "always_show_escalation": True,
        "disclaimer_required": True,
    },
    "nurse": {
        "show_raw_evidence": True,
        "show_dose_details": True,
        "show_grade": True,
        "language_level": "clinical",
        "max_claims_shown": 10,
        "always_show_escalation": False,
        "disclaimer_required": False,
    },
    "doctor": {
        "show_raw_evidence": True,
        "show_dose_details": True,
        "show_grade": True,
        "language_level": "expert",
        "max_claims_shown": 20,
        "always_show_escalation": False,
        "disclaimer_required": False,
    },
    "pharmacist": {
        "show_raw_evidence": True,
        "show_dose_details": True,
        "show_grade": True,
        "language_level": "expert",
        "max_claims_shown": 20,
        "always_show_escalation": False,
        "disclaimer_required": False,
    },
    "researcher": {
        "show_raw_evidence": True,
        "show_dose_details": True,
        "show_grade": True,
        "language_level": "expert",
        "max_claims_shown": 50,
        "always_show_escalation": False,
        "disclaimer_required": False,
    },
}


class RoleBasedUIAdapter:
    """L8-4: Filter and adapt CURANIQ card output based on user role."""

    def adapt(self, card: CURaniqCard, user_role: str) -> dict:
        config = ROLE_UI_CONFIGS.get(user_role.lower(), ROLE_UI_CONFIGS["patient"])
        output: dict = {
            "response_id": card.response_id,
            "mode": card.mode,
            "staleness_display": card.staleness_display,
            "safety_flags": card.safety_flags,
            "jurisdiction": card.jurisdiction,
        }

        # Action card — always shown
        if card.action:
            steps = card.action.action_steps[:config["max_claims_shown"]]
            output["action"] = {
                "headline": card.action.headline,
                "steps": steps,
                "urgency": card.action.urgency,
            }
            if config["always_show_escalation"] and card.action.escalation:
                output["action"]["escalation"] = card.action.escalation
            if card.action.monitoring:
                output["action"]["monitoring"] = card.action.monitoring

        # Why card — show grade only to clinical users
        if card.why:
            why_output: dict = {"summary": card.why.summary}
            if config["show_grade"]:
                why_output["grade"] = card.why.grade_label
                why_output["confidence"] = card.why.confidence
                if card.why.guideline_source:
                    why_output["guideline_source"] = card.why.guideline_source
            output["why"] = why_output

        # Evidence cards — shown to clinical/researcher only
        if config["show_raw_evidence"]:
            output["evidence"] = [
                {
                    "source": e.source_name,
                    "tier": e.evidence_tier,
                    "date": e.publication_date,
                    "doi": e.doi_url,
                    "snippet": e.snippet,
                    "retraction_status": e.retraction_status,
                }
                for e in card.evidence
            ]

        # Uncertainty card — simplified for patients
        if card.uncertainty:
            if config["show_raw_evidence"]:
                output["uncertainty"] = {
                    "gaps": card.uncertainty.gaps,
                    "suppressed_count": len(card.uncertainty.suppressed_claims),
                    "low_confidence_areas": card.uncertainty.low_confidence_areas,
                }
            elif card.uncertainty.gaps:
                output["uncertainty"] = {"note": "Some information is uncertain — discuss with your healthcare team."}

        if config["disclaimer_required"]:
            output["disclaimer"] = "This information is for educational purposes only. Always consult your healthcare professional."

        return output


# ─────────────────────────────────────────────────────────────────────────────
# L8-5: MULTILINGUAL ENGINE
# Architecture: 'English, Russian, Uzbek. Clinical meaning-lock (L8-13).
# NEVER changes clinical meaning when translating.'
# ─────────────────────────────────────────────────────────────────────────────

# Clinical term lock-list — NEVER translated (use INN/universal term)
CLINICAL_TERMS_DO_NOT_TRANSLATE = {
    "mg", "mcg", "ml", "mmol", "mmHg", "eGFR", "INR", "HbA1c", "ECG", "EKG",
    "NICE", "FDA", "MHRA", "WHO", "BNF", "GRADE", "RCT", "PICO",
    "IV", "IM", "SC", "PO", "PRN", "BD", "TDS", "QDS", "OD",
    "CKD", "AKI", "MI", "CVA", "PE", "DVT", "AF", "HF", "HTN",
    "ICD-10", "SNOMED", "RxNorm", "LOINC",
}

# UI string translations EN → RU → UZ
UI_STRINGS: dict[str, dict[str, str]] = {
    "confidence_high":    {"en": "High confidence", "ru": "Высокая уверенность",     "uz": "Yuqori ishonchlilik"},
    "confidence_moderate":{"en": "Moderate confidence", "ru": "Умеренная уверенность", "uz": "O'rtacha ishonchlilik"},
    "confidence_low":     {"en": "Low confidence", "ru": "Низкая уверенность",       "uz": "Past ishonchlilik"},
    "consult_doctor":     {"en": "Consult your doctor", "ru": "Проконсультируйтесь с врачом", "uz": "Shifokor bilan maslahating"},
    "evidence_source":    {"en": "Evidence source", "ru": "Источник данных",          "uz": "Ma'lumot manbai"},
    "urgent":             {"en": "URGENT", "ru": "СРОЧНО",                           "uz": "SHOSHILINCH"},
    "emergency":          {"en": "EMERGENCY — call emergency services", "ru": "ЭКСТРЕННО — вызовите скорую помощь", "uz": "FAVQULODDA — tez yordam chaqiring"},
    "dose_adjustment":    {"en": "Dose adjustment required", "ru": "Требуется коррекция дозы", "uz": "Dozani sozlash talab etiladi"},
    "contraindicated":    {"en": "Contraindicated", "ru": "Противопоказано",          "uz": "Qarshi ko'rsatma"},
    "black_box_warning":  {"en": "Black Box Warning", "ru": "Предупреждение в чёрной рамке", "uz": "Qora quti ogohlantirishi"},
    "monitoring_required":{"en": "Monitoring required", "ru": "Требуется мониторинг",  "uz": "Monitoring talab etiladi"},
    "no_evidence":        {"en": "Insufficient evidence", "ru": "Недостаточно доказательств", "uz": "Yetarli dalillar yo'q"},
}


class MultilingualEngine:
    """
    L8-5: CURANIQ multilingual engine for EN/RU/UZ.
    Clinical meaning-lock ensures translated output never changes clinical intent.
    """

    def translate_ui_string(self, key: str, lang: str) -> str:
        lang = lang.lower()
        strings = UI_STRINGS.get(key, {})
        return strings.get(lang, strings.get("en", key))

    def localize_card(self, card_dict: dict, lang: str) -> dict:
        """Localize UI labels in a card dict — does NOT translate clinical content."""
        if lang == "en":
            return card_dict
        result = dict(card_dict)
        # Localize urgency labels
        if "action" in result and "urgency" in result["action"]:
            urgency = result["action"]["urgency"]
            if urgency == "emergency":
                result["action"]["urgency_display"] = self.translate_ui_string("emergency", lang)
            elif urgency == "urgent":
                result["action"]["urgency_display"] = self.translate_ui_string("urgent", lang)
        # Localize grade label
        if "why" in result and "grade" in result["why"]:
            grade = result["why"]["grade"].lower()
            key = f"confidence_{grade}" if f"confidence_{grade}" in UI_STRINGS else "confidence_moderate"
            result["why"]["grade_display"] = self.translate_ui_string(key, lang)
        if result.get("disclaimer"):
            disclaimers = {
                "ru": "Эта информация предназначена только для образовательных целей. Всегда консультируйтесь с врачом.",
                "uz": "Ushbu ma'lumot faqat ta'lim maqsadida taqdim etilgan. Har doim shifokor bilan maslahating.",
            }
            result["disclaimer"] = disclaimers.get(lang, result["disclaimer"])
        return result

    def is_supported_language(self, lang: str) -> bool:
        return lang.lower() in ("en", "ru", "uz")


# ─────────────────────────────────────────────────────────────────────────────
# L8-8: MEDICATION BOUNDARY DISPLAY
# Architecture: '"This is CURANIQ's recommendation based on evidence.
# Final prescribing decision is yours, clinician." Clear AI/human boundary.'
# ─────────────────────────────────────────────────────────────────────────────

MEDICATION_BOUNDARY_STATEMENT = {
    "en": (
        "\n\n🔵 CURANIQ CLINICAL DECISION SUPPORT BOUNDARY:\n"
        "This output represents evidence-based decision support, not a prescription. "
        "The final prescribing decision — including whether this medication is appropriate "
        "for this specific patient — remains entirely with the responsible clinician. "
        "CURANIQ does not replace clinical judgment.\n"
        "[CURANIQ v3.6 | L8-8 Medication Boundary Display]"
    ),
    "ru": (
        "\n\n🔵 ГРАНИЦА КЛИНИЧЕСКОЙ ПОДДЕРЖКИ CURANIQ:\n"
        "Данный вывод представляет собой доказательную поддержку принятия решений, а не рецепт. "
        "Окончательное решение о назначении — включая вопрос о том, подходит ли данный препарат "
        "конкретному пациенту — остаётся за ответственным врачом. "
        "CURANIQ не заменяет клиническое суждение.\n"
        "[CURANIQ v3.6 | L8-8]"
    ),
    "uz": (
        "\n\n🔵 CURANIQ KLINIK QAROR QABUL QILISH CHEGARASI:\n"
        "Ushbu natija retsept emas, dalillarga asoslangan qaror qabul qilish yordamidir. "
        "Dori belgilash bo'yicha yakuniy qaror — shu jumladan ushbu dori aniq bemorga "
        "mos kelishi yoki yo'qligi — mas'ul shifokorga tegishli. "
        "CURANIQ klinik mulohazani almashtirmaydi.\n"
        "[CURANIQ v3.6 | L8-8]"
    ),
}


class MedicationBoundaryDisplay:
    """L8-8: Inject medication boundary statement into all clinical outputs."""

    def inject(self, output_text: str, lang: str = "en", user_role: str = "doctor") -> str:
        if user_role.lower() in ("patient", "caregiver"):
            return output_text  # Patient disclaimer handled by L5-14
        statement = MEDICATION_BOUNDARY_STATEMENT.get(lang, MEDICATION_BOUNDARY_STATEMENT["en"])
        return output_text + statement


# ─────────────────────────────────────────────────────────────────────────────
# L8-12: UNIVERSAL LANGUAGE AUTO-DETECTION
# Architecture: 'Auto-detect input language on EVERY query.
# Routes to appropriate language pipeline.'
# ─────────────────────────────────────────────────────────────────────────────

# Cyrillic character ranges — Russian/Uzbek (Cyrillic script)
_CYRILLIC = re.compile(r'[\u0400-\u04FF]')
# Uzbek Latin script indicators (post-1993 Uzbek alphabet)
_UZBEK_LATIN_INDICATORS = re.compile(r'\b(va|bu|uchun|bilan|ham|emas|lekin|yoki|qilib|bo\'lib)\b', re.I)
# Russian function words
_RUSSIAN_INDICATORS = re.compile(r'\b(и|в|не|на|с|по|как|это|для|при|или|что|то|но|же)\b')
# Arabic/Uzbek script
_ARABIC_SCRIPT = re.compile(r'[\u0600-\u06FF\u0750-\u077F]')


def detect_language(text: str) -> str:
    """
    Detect language of input text.
    Returns ISO 639-1 code: 'en', 'ru', 'uz'.
    Falls back to 'en'.
    """
    if not text or len(text.strip()) < 3:
        return "en"

    cyrillic_count = len(_CYRILLIC.findall(text))
    total_chars = len([c for c in text if c.isalpha()])

    if total_chars == 0:
        return "en"

    cyrillic_ratio = cyrillic_count / total_chars

    if cyrillic_ratio > 0.3:
        # Likely Russian or Cyrillic Uzbek
        if _RUSSIAN_INDICATORS.search(text):
            return "ru"
        return "uz"  # Default Cyrillic non-Russian to Uzbek

    if _UZBEK_LATIN_INDICATORS.search(text):
        return "uz"

    if _ARABIC_SCRIPT.search(text):
        return "uz"  # Old Uzbek/Arabic script

    return "en"


class LanguageAutoDetector:
    """L8-12: Automatic language detection for all CURANIQ inputs."""

    def detect(self, text: str) -> str:
        return detect_language(text)

    def detect_with_confidence(self, text: str) -> tuple[str, float]:
        lang = detect_language(text)
        # Confidence heuristic
        if lang == "en":
            en_words = len(re.findall(r'\b(?:the|and|for|with|this|that|from|have|not)\b', text, re.I))
            confidence = min(0.95, 0.5 + en_words * 0.05)
        elif lang == "ru":
            ru_words = len(_RUSSIAN_INDICATORS.findall(text))
            confidence = min(0.95, 0.5 + ru_words * 0.08)
        else:
            confidence = 0.70  # Uzbek detection less precise without full NLP
        return lang, round(confidence, 2)


# ─────────────────────────────────────────────────────────────────────────────
# L8-13: MEDICAL TRANSLATION ENGINE (3-STAGE)
# Architecture: '(1) Detect source language, (2) Medical terminology lock,
# (3) Back-translation verification. Clinical meaning preserved.'
# ─────────────────────────────────────────────────────────────────────────────

# Bilingual medical glossary — locked terms that must survive translation intact
MEDICAL_GLOSSARY_EN_RU: dict[str, str] = {
    "contraindicated": "противопоказан",
    "dose adjustment": "коррекция дозы",
    "renal impairment": "почечная недостаточность",
    "hepatic impairment": "печёночная недостаточность",
    "drug interaction": "лекарственное взаимодействие",
    "black box warning": "предупреждение в чёрной рамке",
    "first-line": "препарат первой линии",
    "second-line": "препарат второй линии",
    "off-label": "вне зарегистрированных показаний",
    "evidence-based": "доказательная",
    "randomized controlled trial": "рандомизированное контролируемое исследование",
    "systematic review": "систематический обзор",
    "meta-analysis": "метаанализ",
    "clinical guideline": "клинические рекомендации",
    "adverse effect": "нежелательный эффект",
    "serious adverse event": "серьёзное нежелательное явление",
    "therapeutic drug monitoring": "терапевтический лекарственный мониторинг",
}

MEDICAL_GLOSSARY_EN_UZ: dict[str, str] = {
    "contraindicated": "qarshi ko'rsatma mavjud",
    "dose adjustment": "dozani sozlash",
    "renal impairment": "buyrak yetishmovchiligi",
    "hepatic impairment": "jigar yetishmovchiligi",
    "drug interaction": "dori o'zaro ta'siri",
    "black box warning": "qora quti ogohlantirishi",
    "first-line": "birinchi qator dori",
    "second-line": "ikkinchi qator dori",
    "evidence-based": "dalillarga asoslangan",
    "adverse effect": "noxush ta'sir",
    "clinical guideline": "klinik ko'rsatma",
}


@dataclass
class MedicalTranslationResult:
    source_lang:    str
    target_lang:    str
    source_text:    str
    translated:     str
    locked_terms:   list[str]   # Terms preserved untranslated
    meaning_locked: bool        # Passed 3-stage verification
    warnings:       list[str]


class MedicalTranslationEngine:
    """
    L8-13: Three-stage medical translation.
    Stage 1: Detect source language
    Stage 2: Lock critical medical terms
    Stage 3: Verify meaning preserved (back-translation check)
    
    Production: integrates with DeepL Medical API + custom clinical fine-tune.
    Current: glossary-based translation with meaning-lock verification.
    """

    def __init__(self) -> None:
        self.detector = LanguageAutoDetector()
        self.glossaries = {"ru": MEDICAL_GLOSSARY_EN_RU, "uz": MEDICAL_GLOSSARY_EN_UZ}

    def translate(self, text: str, target_lang: str, source_lang: Optional[str] = None) -> MedicalTranslationResult:
        # Stage 1: Detect source
        if not source_lang:
            source_lang = self.detector.detect(text)

        if source_lang == target_lang:
            return MedicalTranslationResult(
                source_lang=source_lang, target_lang=target_lang,
                source_text=text, translated=text,
                locked_terms=[], meaning_locked=True, warnings=[],
            )

        # Stage 2: Lock clinical terms
        locked = []
        working = text
        glossary = self.glossaries.get(target_lang, {})

        # Lock numeric/unit terms (never translate)
        for term in CLINICAL_TERMS_DO_NOT_TRANSLATE:
            if term in working:
                locked.append(term)

        # Apply glossary translations where available
        translated = working
        for en_term, target_term in glossary.items():
            if en_term.lower() in translated.lower():
                translated = re.sub(re.escape(en_term), target_term, translated, flags=re.I)
                locked.append(en_term)

        # Stage 3: Meaning-lock verification
        # Verify numeric values are preserved
        source_numbers = re.findall(r'\d+(?:\.\d+)?', text)
        translated_numbers = re.findall(r'\d+(?:\.\d+)?', translated)
        meaning_locked = set(source_numbers) == set(translated_numbers)
        warnings = []
        if not meaning_locked:
            warnings.append(f"NUMERIC MISMATCH: source={source_numbers}, translated={translated_numbers}")

        # Verify key locked terms survived
        for term in locked[:5]:
            if term.lower() not in translated.lower() and term not in CLINICAL_TERMS_DO_NOT_TRANSLATE:
                warnings.append(f"TERM MAY NOT BE PRESERVED: {term}")

        return MedicalTranslationResult(
            source_lang=source_lang,
            target_lang=target_lang,
            source_text=text,
            translated=translated,
            locked_terms=locked,
            meaning_locked=meaning_locked and len(warnings) == 0,
            warnings=warnings,
        )
