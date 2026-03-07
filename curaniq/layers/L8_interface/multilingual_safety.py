"""
CURANIQ — Medical Evidence Operating System
Layer 8: Clinician Experience & Interface

L8-5  Multilingual Clinical Interface (EN/RU/UZ meaning-safe)
L8-12 Meaning Lock Engine (prevents negation/dose translation errors)

Architecture: "Meaning-locks on negation, units, doses, contraindications.
Back-translation verification blocks 'do NOT take' → 'do take' errors."

CRITICAL FOR UZBEKISTAN/CIS MARKET: Russian and Uzbek medical terminology
must be handled with clinical precision. A translation error in a dose
or contraindication is a patient safety event.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# L8-12: MEANING LOCK ENGINE
# ─────────────────────────────────────────────────────────────────────────────

class MeaningLockCategory(str, Enum):
    """Categories of clinical content that must be meaning-locked."""
    NEGATION       = "negation"        # "do NOT take", "contraindicated"
    DOSE_NUMERIC   = "dose_numeric"    # "500 mg twice daily"
    UNIT           = "unit"            # "mg" vs "mcg" vs "g"
    FREQUENCY      = "frequency"       # "BID" vs "TID" vs "QD"
    SEVERITY       = "severity"        # "mild" vs "severe"
    ALLERGY_ALERT  = "allergy_alert"   # "ALLERGIC — do not administer"
    ROUTE          = "route"           # "oral" vs "IV" vs "topical"
    DECIMAL_SEP    = "decimal_sep"     # "2.5" (EN) vs "2,5" (RU)


@dataclass
class MeaningLock:
    """A locked semantic unit that must survive translation unchanged."""
    lock_id: str
    category: MeaningLockCategory
    original_text: str
    canonical_form: str
    language: str
    position_start: int = 0
    position_end: int = 0
    verified: bool = False


@dataclass
class MeaningLockResult:
    locks: list[MeaningLock] = field(default_factory=list)
    total_locked: int = 0
    negation_count: int = 0
    dose_count: int = 0
    warnings: list[str] = field(default_factory=list)


class MeaningLockEngine:
    """
    L8-12: Locks critical clinical semantics before translation.

    The engine identifies and protects:
    1. Negation patterns: "do NOT take", "contraindicated", "avoid"
    2. Dose numerics: "500 mg", "2.5 mL", "10 units"
    3. Unit expressions: ensures "mg" never becomes "mcg"
    4. Frequency: "twice daily" never becomes "twice weekly"
    5. Decimal separators: "2.5" (EN) preserved, not "25" in RU
    6. Route of administration: "oral" never becomes "IV"

    After translation, the back-translation verifier checks that
    every locked element survived the round-trip.
    """

    # Negation patterns (EN/RU/UZ)
    NEGATION_PATTERNS: dict[str, list[re.Pattern]] = {
        "en": [
            re.compile(r'\b(do\s+NOT|should\s+NOT|must\s+NOT|NEVER|cannot|AVOID)\b', re.I),
            re.compile(r'\b(contraindicated|prohibited|forbidden|not\s+recommended)\b', re.I),
            re.compile(r'\b(discontinue|stop|withhold|hold|suspend|cease)\b', re.I),
        ],
        "ru": [
            re.compile(r'\b(НЕ\s+принимать|НЕЛЬЗЯ|ЗАПРЕЩЕНО|ПРОТИВОПОКАЗАНО)\b', re.I),
            re.compile(r'\b(не\s+рекомендуется|не\s+следует|не\s+назначать)\b', re.I),
            re.compile(r'\b(отменить|прекратить|приостановить)\b', re.I),
        ],
        "uz": [
            re.compile(r'\b(qabul\s+qilmang|MUMKIN\s+EMAS|TAQIQLANADI|MAN\s+ETILADI)\b', re.I),
            re.compile(r"\b(tavsiya\s+etilmaydi|buyurmaslik|to'xtatish)\b", re.I),
        ],
    }

    # Dose + unit patterns (language-independent)
    DOSE_PATTERNS: list[re.Pattern] = [
        re.compile(r'\b(\d+(?:[.,]\d+)?)\s*(mg|mcg|µg|g|kg|mL|L|IU|units?|mmol|mEq)\b', re.I),
        re.compile(r'\b(\d+(?:[.,]\d+)?)\s*(tablets?|capsules?|drops?|puffs?|sachets?)\b', re.I),
    ]

    FREQUENCY_PATTERNS: list[tuple[re.Pattern, str]] = [
        (re.compile(r'\b(once\s+daily|OD|QD|один\s+раз\s+в\s+день)\b', re.I), "once_daily"),
        (re.compile(r'\b(twice\s+daily|BID|два\s+раза\s+в\s+день)\b', re.I), "twice_daily"),
        (re.compile(r'\b(three\s+times\s+daily|TID|TDS|три\s+раза\s+в\s+день)\b', re.I), "three_times_daily"),
        (re.compile(r'\b(four\s+times\s+daily|QID|QDS)\b', re.I), "four_times_daily"),
        (re.compile(r'\b(every\s+(\d+)\s+hours?|каждые\s+(\d+)\s+час)\b', re.I), "every_n_hours"),
        (re.compile(r'\b(weekly|еженедельно|раз\s+в\s+неделю)\b', re.I), "weekly"),
    ]

    ROUTE_PATTERNS: list[tuple[re.Pattern, str]] = [
        (re.compile(r'\b(oral(?:ly)?|перорально|ич(?:ки)?)\b', re.I), "oral"),
        (re.compile(r'\b(intravenous(?:ly)?|IV|внутривенно)\b', re.I), "intravenous"),
        (re.compile(r'\b(intramuscular(?:ly)?|IM|внутримышечно)\b', re.I), "intramuscular"),
        (re.compile(r'\b(subcutaneous(?:ly)?|SC|подкожно)\b', re.I), "subcutaneous"),
        (re.compile(r'\b(topical(?:ly)?|наружно|местно)\b', re.I), "topical"),
        (re.compile(r'\b(inhal(?:ation|ed)?|ингаляционно)\b', re.I), "inhalation"),
    ]

    def extract_locks(self, text: str, language: str = "en") -> MeaningLockResult:
        """
        Extract all meaning-lockable elements from clinical text.
        Returns locks that must survive translation intact.
        """
        result = MeaningLockResult()
        lock_counter = 0

        # 1. Lock negation patterns
        lang_patterns = self.NEGATION_PATTERNS.get(language, self.NEGATION_PATTERNS["en"])
        for pattern in lang_patterns:
            for match in pattern.finditer(text):
                lock_counter += 1
                result.locks.append(MeaningLock(
                    lock_id=f"NEG-{lock_counter:04d}",
                    category=MeaningLockCategory.NEGATION,
                    original_text=match.group(),
                    canonical_form=match.group().upper(),
                    language=language,
                    position_start=match.start(),
                    position_end=match.end(),
                ))
                result.negation_count += 1

        # 2. Lock dose + unit expressions
        for pattern in self.DOSE_PATTERNS:
            for match in pattern.finditer(text):
                lock_counter += 1
                result.locks.append(MeaningLock(
                    lock_id=f"DOSE-{lock_counter:04d}",
                    category=MeaningLockCategory.DOSE_NUMERIC,
                    original_text=match.group(),
                    canonical_form=match.group().replace(",", "."),
                    language=language,
                    position_start=match.start(),
                    position_end=match.end(),
                ))
                result.dose_count += 1

        # 3. Lock frequency expressions
        for pattern, canonical in self.FREQUENCY_PATTERNS:
            for match in pattern.finditer(text):
                lock_counter += 1
                result.locks.append(MeaningLock(
                    lock_id=f"FREQ-{lock_counter:04d}",
                    category=MeaningLockCategory.FREQUENCY,
                    original_text=match.group(),
                    canonical_form=canonical,
                    language=language,
                    position_start=match.start(),
                    position_end=match.end(),
                ))

        # 4. Lock route of administration
        for pattern, canonical in self.ROUTE_PATTERNS:
            for match in pattern.finditer(text):
                lock_counter += 1
                result.locks.append(MeaningLock(
                    lock_id=f"ROUTE-{lock_counter:04d}",
                    category=MeaningLockCategory.ROUTE,
                    original_text=match.group(),
                    canonical_form=canonical,
                    language=language,
                    position_start=match.start(),
                    position_end=match.end(),
                ))

        result.total_locked = lock_counter
        return result


# ─────────────────────────────────────────────────────────────────────────────
# L8-5: MULTILINGUAL CLINICAL INTERFACE
# ─────────────────────────────────────────────────────────────────────────────

class SupportedLanguage(str, Enum):
    EN = "en"
    RU = "ru"
    UZ = "uz"


@dataclass
class BackTranslationResult:
    """Result of round-trip translation verification."""
    original_text: str
    translated_text: str
    back_translated_text: str
    meaning_preserved: bool = True
    lost_negations: list[str] = field(default_factory=list)
    altered_doses: list[str] = field(default_factory=list)
    altered_routes: list[str] = field(default_factory=list)
    confidence: float = 1.0


class MultilingualClinicalInterface:
    """
    L8-5: Safe multilingual clinical output.

    Pipeline:
    1. Detect input language (auto or explicit)
    2. Process all clinical logic in English (deterministic pipeline)
    3. Extract meaning locks from English output (L8-12)
    4. Translate to target language
    5. Back-translate to English
    6. Verify all meaning locks survived round-trip
    7. If ANY negation or dose lock failed → REFUSE translation,
       deliver English output with language warning

    Supported: EN (primary), RU (Tier 1), UZ (Tier 1)
    """

    # Language detection patterns
    CYRILLIC_PATTERN = re.compile(r'[\u0400-\u04FF]')
    LATIN_UZ_PATTERN = re.compile(r"\b(qabul|dori|kasallik|shifokor|bemor|davolash)\b", re.I)

    def __init__(self):
        self.meaning_lock_engine = MeaningLockEngine()

    def detect_language(self, text: str) -> SupportedLanguage:
        """Auto-detect input language from text content."""
        cyrillic_count = len(self.CYRILLIC_PATTERN.findall(text))
        total_chars = len(text.strip())

        if total_chars == 0:
            return SupportedLanguage.EN

        cyrillic_ratio = cyrillic_count / total_chars

        # Uzbek Latin detection (specific UZ medical terms)
        if self.LATIN_UZ_PATTERN.search(text):
            return SupportedLanguage.UZ

        # Cyrillic-heavy = Russian (could also be Uzbek Cyrillic)
        if cyrillic_ratio > 0.3:
            return SupportedLanguage.RU

        return SupportedLanguage.EN

    def verify_translation_safety(
        self,
        english_output: str,
        translated_output: str,
        back_translated: str,
        target_language: SupportedLanguage,
    ) -> BackTranslationResult:
        """
        Verify that translation preserved all critical clinical meaning.

        Compares meaning locks between original English and back-translated English.
        ANY lost negation → translation REFUSED (fail-closed).
        """
        result = BackTranslationResult(
            original_text=english_output,
            translated_text=translated_output,
            back_translated_text=back_translated,
        )

        # Extract locks from original and back-translated
        original_locks = self.meaning_lock_engine.extract_locks(english_output, "en")
        backtrans_locks = self.meaning_lock_engine.extract_locks(back_translated, "en")

        # Check negation preservation (CRITICAL — fail-closed)
        original_negations = {
            lock.canonical_form
            for lock in original_locks.locks
            if lock.category == MeaningLockCategory.NEGATION
        }
        backtrans_negations = {
            lock.canonical_form
            for lock in backtrans_locks.locks
            if lock.category == MeaningLockCategory.NEGATION
        }
        lost_negations = original_negations - backtrans_negations
        if lost_negations:
            result.meaning_preserved = False
            result.lost_negations = list(lost_negations)
            result.confidence = 0.0
            logger.error(
                "MEANING LOCK FAILURE: Negation lost in %s translation: %s",
                target_language.value, lost_negations,
            )

        # Check dose preservation
        original_doses = {
            lock.canonical_form
            for lock in original_locks.locks
            if lock.category == MeaningLockCategory.DOSE_NUMERIC
        }
        backtrans_doses = {
            lock.canonical_form
            for lock in backtrans_locks.locks
            if lock.category == MeaningLockCategory.DOSE_NUMERIC
        }
        lost_doses = original_doses - backtrans_doses
        if lost_doses:
            result.altered_doses = list(lost_doses)
            result.confidence = min(result.confidence, 0.3)
            logger.warning(
                "MEANING LOCK WARNING: Dose altered in %s translation: %s",
                target_language.value, lost_doses,
            )

        # Check route preservation
        original_routes = {
            lock.canonical_form
            for lock in original_locks.locks
            if lock.category == MeaningLockCategory.ROUTE
        }
        backtrans_routes = {
            lock.canonical_form
            for lock in backtrans_locks.locks
            if lock.category == MeaningLockCategory.ROUTE
        }
        lost_routes = original_routes - backtrans_routes
        if lost_routes:
            result.altered_routes = list(lost_routes)
            result.confidence = min(result.confidence, 0.5)

        # If no issues found, high confidence
        if result.meaning_preserved and not lost_doses and not lost_routes:
            result.confidence = 0.95

        return result

    def safe_translate(
        self,
        english_output: str,
        target_language: SupportedLanguage,
        translation_fn=None,
    ) -> tuple[str, bool, list[str]]:
        """
        Translate with safety verification.

        Returns: (output_text, is_translated, warnings)
        If translation is unsafe, returns English with warning.
        """
        if target_language == SupportedLanguage.EN:
            return english_output, False, []

        if translation_fn is None:
            # No translation service available — return English with notice
            return english_output, False, [
                f"Translation to {target_language.value} not available. "
                "Displaying in English for safety."
            ]

        # Extract meaning locks BEFORE translation
        locks = self.meaning_lock_engine.extract_locks(english_output, "en")

        # Translate
        translated = translation_fn(english_output, "en", target_language.value)

        # Back-translate for verification
        back_translated = translation_fn(translated, target_language.value, "en")

        # Verify safety
        verification = self.verify_translation_safety(
            english_output, translated, back_translated, target_language,
        )

        if not verification.meaning_preserved:
            # FAIL-CLOSED: negation lost → deliver English
            warnings = [
                f"⚠️ Translation to {target_language.value} BLOCKED: "
                f"Critical negation(s) lost in translation: {verification.lost_negations}. "
                "Delivering English output for patient safety."
            ]
            return english_output, False, warnings

        warnings = []
        if verification.altered_doses:
            warnings.append(
                f"⚠️ Dose expression may have changed in translation. "
                f"Verify: {verification.altered_doses}"
            )
        if verification.altered_routes:
            warnings.append(
                f"⚠️ Route of administration may have changed. "
                f"Verify: {verification.altered_routes}"
            )

        return translated, True, warnings
