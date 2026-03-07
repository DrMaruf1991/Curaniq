"""
CURANIQ - Universal Input Normalizer (L8-12 + L8-13)
Any language in -> English-normalized for pipeline processing.

Copy this file to: curaniq/layers/L8_interface/universal_input.py

Architecture:
  1. Script detection via Unicode (ALL scripts, not a language list)
  2. If non-Latin: flag for translation
  3. Medical entity extraction via ontology (deterministic offline fallback)
  4. Full translation via LLM (production) — the LLM already speaks all languages
  5. Pipeline processes English. Response translates back.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional, Callable


# ─────────────────────────────────────────────────────────────────
# UNIVERSAL SCRIPT DETECTION
# No hardcoded language list. Detects writing system via Unicode.
# ─────────────────────────────────────────────────────────────────

_SCRIPT_MAP = {
    "CYRILLIC": "cyrillic", "ARABIC": "arabic", "HEBREW": "hebrew",
    "CJK": "cjk", "KANGXI": "cjk",
    "HANGUL": "hangul",
    "HIRAGANA": "japanese", "KATAKANA": "japanese",
    "DEVANAGARI": "devanagari", "BENGALI": "bengali",
    "TAMIL": "tamil", "TELUGU": "telugu",
    "GUJARATI": "gujarati", "KANNADA": "kannada",
    "MALAYALAM": "malayalam", "SINHALA": "sinhala",
    "THAI": "thai", "LAO": "lao", "KHMER": "khmer",
    "MYANMAR": "myanmar", "TIBETAN": "tibetan",
    "GEORGIAN": "georgian", "ARMENIAN": "armenian",
    "GREEK": "greek", "ETHIOPIC": "ethiopic",
}


def _char_script(ch: str) -> str:
    """Get script family for a single Unicode character."""
    try:
        name = unicodedata.name(ch, "").upper()
    except ValueError:
        return "latin"
    for key, script in _SCRIPT_MAP.items():
        if key in name:
            return script
    return "latin"


def detect_script(text: str) -> str:
    """
    Detect dominant script in text via Unicode character analysis.
    Returns: 'latin', 'cyrillic', 'arabic', 'cjk', 'hangul',
    'japanese', 'devanagari', 'thai', 'greek', 'georgian', etc.
    Works for ANY language. No hardcoded language list.
    """
    if not text or not text.strip():
        return "latin"

    counts: dict[str, int] = {}
    for ch in text:
        if not ch.isalpha():
            continue
        s = _char_script(ch)
        counts[s] = counts.get(s, 0) + 1

    if not counts:
        return "latin"
    return max(counts, key=counts.get)


def is_english_script(text: str) -> bool:
    """True if text is primarily Latin script."""
    return detect_script(text) == "latin"


# ─────────────────────────────────────────────────────────────────
# NORMALIZED QUERY
# ─────────────────────────────────────────────────────────────────

@dataclass
class NormalizedQuery:
    """A query normalized for pipeline processing."""
    original_text: str
    source_script: str
    needs_translation: bool
    english_text: str
    detected_drugs: list[str] = field(default_factory=list)
    detected_foods: list[str] = field(default_factory=list)
    translation_method: str = "none"


# ─────────────────────────────────────────────────────────────────
# UNIVERSAL INPUT NORMALIZER
# ─────────────────────────────────────────────────────────────────

class UniversalInputNormalizer:
    """
    L8-12 + L8-13: Universal input processing for any language.

    The LLM already understands all languages. This layer ensures
    the DETERMINISTIC parts (CQL, safety gates, triage) get clean
    English input regardless of source language.

    Production: pass llm_translate_fn for full translation.
    Dev/offline: deterministic medical entity extraction works for
    languages covered by the ontology (EN/RU/UZ + brands).
    """

    def __init__(self, llm_translate_fn: Optional[Callable] = None):
        self._llm_translate = llm_translate_fn

    def normalize(self, raw_text: str) -> NormalizedQuery:
        """Normalize any-language input for English pipeline."""
        script = detect_script(raw_text)
        needs_translation = script != "latin"

        drugs = self._extract_drugs_deterministic(raw_text)
        foods = self._extract_foods_deterministic(raw_text)

        if not needs_translation:
            english_text = raw_text
            method = "none"
        elif self._llm_translate:
            english_text = self._llm_translate(raw_text, script)
            method = "llm"
        else:
            english_text = self._build_english_fallback(raw_text, drugs, foods)
            method = "deterministic"

        return NormalizedQuery(
            original_text=raw_text,
            source_script=script,
            needs_translation=needs_translation,
            english_text=english_text,
            detected_drugs=drugs,
            detected_foods=foods,
            translation_method=method,
        )

    def _extract_drugs_deterministic(self, text: str) -> list[str]:
        """Extract drugs via ontology. Works in any language."""
        try:
            from curaniq.layers.L2_curation.ontology import (
                _REVERSE_DRUG_LOOKUP, resolve_drug_name,
            )
        except ImportError:
            return []

        found, seen = [], set()
        text_lower = text.lower()

        for variant, inn in _REVERSE_DRUG_LOOKUP.items():
            if len(variant) >= 3:
                pattern = r'\b' + re.escape(variant) + r'\b'
                if re.search(pattern, text_lower):
                    if inn not in seen:
                        found.append(inn)
                        seen.add(inn)

        for token in re.findall(r'\b[\w\u0400-\u04FF]{4,}\b', text):
            canonical, resolved = resolve_drug_name(token)
            if resolved and canonical not in seen:
                found.append(canonical)
                seen.add(canonical)

        return found

    def _extract_foods_deterministic(self, text: str) -> list[str]:
        """Extract food/herb terms. Works in any language."""
        try:
            from curaniq.layers.L3_safety_kernel.food_herb_resolver import (
                _REVERSE_FOOD_LOOKUP,
            )
            found, seen = [], set()
            text_lower = text.lower()
            for variant, canonical in _REVERSE_FOOD_LOOKUP.items():
                if len(variant) >= 3:
                    pattern = r'\b' + re.escape(variant) + r'\b'
                    if re.search(pattern, text_lower):
                        if canonical not in seen:
                            found.append(canonical)
                            seen.add(canonical)
            return found
        except ImportError:
            return self._extract_foods_basic(text)

    def _extract_foods_basic(self, text: str) -> list[str]:
        """English-only fallback if food_herb_resolver not installed."""
        terms = [
            "grapefruit", "dairy", "milk", "alcohol", "caffeine",
            "coffee", "st johns wort", "ginkgo", "ginseng", "kava",
            "tyramine", "vitamin k", "liquorice", "licorice",
        ]
        found = []
        tl = text.lower()
        for t in terms:
            if re.search(r'\b' + re.escape(t) + r'\b', tl):
                found.append(t)
        return found

    def _build_english_fallback(self, text, drugs, foods):
        """Build English summary from extracted entities when no LLM."""
        parts = []
        if drugs:
            parts.append("Drugs: " + ", ".join(drugs))
        if foods:
            parts.append("Food/herb: " + ", ".join(foods))
        nums = re.findall(r'\d+(?:\.\d+)?\s*(?:mg|mcg|ml|mmol|mEq|%|kg)', text)
        if nums:
            parts.append("Values: " + ", ".join(nums))
        return " | ".join(parts) if parts else text
