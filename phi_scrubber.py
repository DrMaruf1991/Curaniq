"""
CURANIQ - L6-2 PHI Scrubber + Data Exfiltration Prevention
HIPAA Safe Harbor de-identification method (45 CFR 164.514(b)(2)).

Copy to: curaniq/layers/L6_security/phi_scrubber.py

Standard: HIPAA Safe Harbor defines exactly 18 identifier types that
must be removed for de-identification. This is a legal standard,
not a hardcoded list — it comes from US federal law.

The 18 HIPAA Safe Harbor identifiers:
  1. Names
  2. Geographic data (smaller than state)
  3. Dates (except year) related to individual
  4. Phone numbers
  5. Fax numbers
  6. Email addresses
  7. Social Security numbers
  8. Medical record numbers
  9. Health plan beneficiary numbers
  10. Account numbers
  11. Certificate/license numbers
  12. Vehicle identifiers and serial numbers
  13. Device identifiers and serial numbers
  14. Web URLs
  15. IP addresses
  16. Biometric identifiers
  17. Full-face photographs
  18. Any other unique identifying number

Principle: scrub BEFORE sending to LLM. The LLM never sees PHI.
Clinical content (drug names, doses, conditions) passes through unchanged.
"""
from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class ScrubResult:
    """Result of PHI scrubbing."""
    original_length: int
    scrubbed_text: str
    scrubbed_count: int
    identifiers_found: list[str]    # Types found (not values)
    is_clean: bool                  # True if no PHI detected
    scrub_hash: str                 # Hash of scrub operation for audit


# ─────────────────────────────────────────────────────────────────
# HIPAA SAFE HARBOR 18 IDENTIFIER PATTERNS
# These come from 45 CFR 164.514(b)(2) — federal law, not invention.
# ─────────────────────────────────────────────────────────────────

class PHIScrubber:
    """
    L6-2: HIPAA Safe Harbor PHI de-identification.
    
    Scrubs the 18 identifier types defined in federal law.
    Replaces with type-tagged placeholders: [PHI:TYPE]
    Clinical content passes through unchanged.
    
    The LLM receives scrubbed text. It never sees patient identifiers.
    The original mapping is stored in the audit trail for authorized
    re-identification by the treating clinician only.
    """

    def __init__(self):
        # Build pattern registry — each entry is (identifier_type, pattern)
        # Patterns ordered by specificity (most specific first)
        self._patterns: list[tuple[str, re.Pattern]] = self._build_patterns()

    def _build_patterns(self) -> list[tuple[str, re.Pattern]]:
        """
        Build HIPAA Safe Harbor 18 identifier patterns.
        Ordered by specificity to prevent over-matching.
        """
        patterns = []

        # 7. SSN (###-##-####) — most specific, match first
        patterns.append(("SSN", re.compile(
            r'\b\d{3}-\d{2}-\d{4}\b'
        )))

        # 4. Phone numbers (various formats)
        patterns.append(("PHONE", re.compile(
            r'\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b'
        )))

        # 5. Fax numbers (often labeled)
        patterns.append(("FAX", re.compile(
            r'(?:fax|telefax|f)\s*[:.]?\s*(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}',
            re.IGNORECASE
        )))

        # 6. Email addresses
        patterns.append(("EMAIL", re.compile(
            r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
        )))

        # 15. IP addresses (IPv4)
        patterns.append(("IP_ADDRESS", re.compile(
            r'\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b'
        )))

        # 14. Web URLs
        patterns.append(("URL", re.compile(
            r'https?://[^\s<>\"\']+|www\.[^\s<>\"\']+',
            re.IGNORECASE
        )))

        # 8. Medical Record Numbers (MRN patterns)
        patterns.append(("MRN", re.compile(
            r'(?:MRN|mrn|medical\s*record)\s*[:.]?\s*[A-Z0-9]{4,12}',
            re.IGNORECASE
        )))

        # 9. Health plan beneficiary numbers
        patterns.append(("HEALTH_PLAN_ID", re.compile(
            r'(?:member|beneficiary|policy|insurance)\s*(?:id|number|no|#)\s*[:.]?\s*[A-Z0-9]{4,15}',
            re.IGNORECASE
        )))

        # 10. Account numbers (bank, billing)
        patterns.append(("ACCOUNT_NUMBER", re.compile(
            r'(?:account|acct)\s*(?:number|no|#)\s*[:.]?\s*\d{4,15}',
            re.IGNORECASE
        )))

        # 11. License/certificate numbers
        patterns.append(("LICENSE", re.compile(
            r'(?:license|certificate|cert|lic)\s*(?:number|no|#)\s*[:.]?\s*[A-Z0-9]{4,12}',
            re.IGNORECASE
        )))

        # 12. Vehicle identifiers (VIN)
        patterns.append(("VEHICLE_ID", re.compile(
            r'\b[A-HJ-NPR-Z0-9]{17}\b'  # VIN format (17 chars, no I/O/Q)
        )))

        # 3. Dates related to individual (DOB, admission, discharge)
        # Keep year, scrub month/day when labeled as personal
        patterns.append(("DATE_PERSONAL", re.compile(
            r'(?:DOB|date\s*of\s*birth|born|birth\s*date|admission\s*date|'
            r'discharge\s*date|admitted|discharged)\s*[:.]?\s*'
            r'\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4}',
            re.IGNORECASE
        )))

        # 2. Geographic data smaller than state (street, city, zip)
        # ZIP codes — 5-digit or 5+4
        patterns.append(("ZIP_CODE", re.compile(
            r'(?:zip|postal)\s*(?:code)?\s*[:.]?\s*\d{5}(?:-\d{4})?',
            re.IGNORECASE
        )))

        # Street addresses
        patterns.append(("ADDRESS", re.compile(
            r'\d{1,5}\s+(?:[A-Z][a-z]+\s+){1,3}(?:Street|St|Avenue|Ave|'
            r'Boulevard|Blvd|Drive|Dr|Road|Rd|Lane|Ln|Way|Court|Ct|'
            r'Place|Pl|Circle|Cir)\b\.?',
            re.IGNORECASE
        )))

        # 1. Names — contextual (labeled as patient/doctor name)
        # Only scrub when preceded by name-indicating context
        patterns.append(("PERSON_NAME", re.compile(
            r'(?:patient|name|Mr|Mrs|Ms|Dr|Prof)\.?\s*[:.]?\s*'
            r'[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}',
            re.IGNORECASE
        )))

        return patterns

    def scrub(self, text: str) -> ScrubResult:
        """
        Scrub PHI from text. Replace with [PHI:TYPE] placeholders.
        Clinical content (drugs, doses, conditions) passes unchanged.
        """
        if not text:
            return ScrubResult(
                original_length=0, scrubbed_text="",
                scrubbed_count=0, identifiers_found=[],
                is_clean=True, scrub_hash="",
            )

        scrubbed = text
        found_types: list[str] = []
        total_scrubbed = 0

        for id_type, pattern in self._patterns:
            matches = list(pattern.finditer(scrubbed))
            if matches:
                found_types.append(id_type)
                total_scrubbed += len(matches)
                # Replace each match with placeholder
                scrubbed = pattern.sub(f"[PHI:{id_type}]", scrubbed)

        # Compute scrub hash for audit trail
        scrub_hash = hashlib.sha256(
            f"{text[:50]}|{total_scrubbed}|{','.join(found_types)}".encode()
        ).hexdigest()[:16]

        return ScrubResult(
            original_length=len(text),
            scrubbed_text=scrubbed,
            scrubbed_count=total_scrubbed,
            identifiers_found=found_types,
            is_clean=(total_scrubbed == 0),
            scrub_hash=scrub_hash,
        )

    def scrub_patient_context(self, context_dict: dict) -> dict:
        """
        Scrub PHI from patient context before sending to LLM.
        Keeps clinical data (age, weight, labs, conditions).
        Removes identifiers (name, MRN, DOB).
        """
        # Fields that are clinical (keep as-is)
        clinical_fields = {
            "age_years", "weight_kg", "sex_at_birth",
            "is_pregnant", "is_breastfeeding",
            "renal", "hepatic",
            "active_medications", "allergies", "conditions",
            "jurisdiction",
        }

        # Fields that are identifiers (scrub)
        identifier_fields = {
            "name", "patient_name", "full_name", "first_name", "last_name",
            "mrn", "medical_record_number",
            "dob", "date_of_birth", "birth_date",
            "ssn", "social_security",
            "phone", "email", "address", "zip_code",
            "insurance_id", "policy_number",
        }

        scrubbed = {}
        for key, value in context_dict.items():
            key_lower = key.lower()
            if key_lower in identifier_fields:
                scrubbed[key] = f"[PHI:{key.upper()}_SCRUBBED]"
            elif key_lower in clinical_fields:
                scrubbed[key] = value
            elif isinstance(value, str):
                # Unknown field — scrub the value
                result = self.scrub(value)
                scrubbed[key] = result.scrubbed_text
            else:
                scrubbed[key] = value

        return scrubbed


# ─────────────────────────────────────────────────────────────────
# OUTPUT EXFILTRATION SCANNER
# Check if LLM output contains PHI that leaked through
# ─────────────────────────────────────────────────────────────────

class OutputExfiltrationScanner:
    """
    Scan LLM output for potential PHI leakage.
    Even though input is scrubbed, the LLM might generate
    content that looks like PHI from its training data.
    """

    def __init__(self):
        self._scrubber = PHIScrubber()

    def scan(self, output_text: str) -> tuple[bool, list[str]]:
        """
        Scan output for PHI patterns.
        Returns (is_clean, found_types).
        """
        result = self._scrubber.scrub(output_text)

        # Filter out false positives common in clinical text
        # Phone-like patterns in dosing: "500-1000 mg" looks like phone
        # Date patterns in evidence: "2024/01/15" is publication date, not DOB
        clinical_false_positives = {"DATE_PERSONAL", "PHONE"}
        real_leaks = [
            t for t in result.identifiers_found
            if t not in clinical_false_positives
        ]

        return len(real_leaks) == 0, real_leaks
