"""
CURANIQ Clinical Knowledge — typed records.

Every clinical fact returned by a `ClinicalKnowledgeProvider` carries
explicit provenance. A fact without provenance cannot be cited; it is
not an evidence-grounded fact. This file defines the typed records
that flow across the provider boundary.

Design constraints:
- Every record carries `source` (URL or registered governed source name),
  `snapshot_date_iso`, `evidence_version`, `license_status`, and
  `is_authoritative` (False = vendored / staged, True = live).
- Providers MUST set these correctly. Callers MAY refuse facts that
  fail their authority threshold (e.g., `clinician_prod` requires
  `is_authoritative=True`).
- Records are frozen dataclasses to prevent caller mutation after
  retrieval (a downstream bug source if any code rewrote a dose
  bound after retrieval).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Pattern


# ─── PROVENANCE ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Provenance:
    """
    Origin of a clinical fact. Every fact must carry one.

    Attributes:
        source: Registered governed-source identifier (e.g., "DAILYMED",
                "OPENFDA", "RXNORM", "CREDIBLEMEDS", "ISMP_SENTINEL_LIST",
                "WHO_EML_2023"). Must match an entry in the source registry.
        source_url: Canonical URL of the original document/record.
                    For vendored snapshots, URL of the artifact that was
                    snapshotted.
        snapshot_date_iso: When this fact was last verified against source.
                           ISO 8601 with timezone.
        evidence_version: Source-defined version (e.g., NDC for DailyMed,
                          DOI for PubMed, schedule version for ATC).
        license_status: One of "public_domain", "open", "restricted",
                        "licensed", "unknown". Affects whether the
                        provider may serve this fact in a given env.
        extraction_method: How the fact was derived from source —
                          "live_api", "cached", "manual_curation",
                          "automated_extraction". For audit.
        is_authoritative: True iff this fact came from a live or
                         recently-cached governed source. False for
                         vendored snapshots and manual curation.
                         `clinician_prod` callers refuse non-authoritative
                         facts.
    """
    source: str
    source_url: str
    snapshot_date_iso: str
    evidence_version: str
    license_status: str
    extraction_method: str
    is_authoritative: bool

    def __post_init__(self) -> None:
        # Validate ISO date — fail fast if file is malformed
        try:
            datetime.fromisoformat(self.snapshot_date_iso.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f"Provenance.snapshot_date_iso must be ISO 8601, got {self.snapshot_date_iso!r}"
            ) from exc
        valid_licenses = {"public_domain", "open", "restricted", "licensed", "unknown"}
        if self.license_status not in valid_licenses:
            raise ValueError(
                f"Provenance.license_status must be one of {valid_licenses}, got {self.license_status!r}"
            )
        valid_methods = {"live_api", "cached", "manual_curation", "automated_extraction"}
        if self.extraction_method not in valid_methods:
            raise ValueError(
                f"Provenance.extraction_method must be one of {valid_methods}, got {self.extraction_method!r}"
            )


# ─── DOSE BOUNDS ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DoseBounds:
    """
    Single-dose plausibility bounds for a drug, in normalized mg.

    These bounds answer ONE question: is the dose in the output a plausible
    single dose for this drug, or is it order-of-magnitude wrong?

    Bounds are NOT clinical recommendations. They are an outer envelope
    derived from the drug's FDA-approved label (Dosage and Administration
    section) — the most-permissive single dose seen across approved
    indications. Doses outside these bounds are presumptively unsafe
    and require human review.

    Attributes:
        drug: Canonical lowercase drug name (RxNorm-preferred when available).
        min_single_dose_mg: Smallest plausible single dose in mg.
                            Below this → "implausibly low" warning.
        max_single_dose_mg: Largest plausible single dose in mg.
                            Above this × tolerance → "implausibly high" block.
        route_context: Free-text route/population qualifier
                       (e.g., "oral adult", "IV pediatric weight-based").
                       Used only for the warning message — NOT for logic.
        tolerance_factor: Multiplier on max for the "implausibly high"
                          threshold. Default 5.0 = an order of magnitude
                          beyond max single dose triggers a block.
        provenance: Where the bounds came from.
    """
    drug: str
    min_single_dose_mg: float
    max_single_dose_mg: float
    route_context: str
    provenance: Provenance
    tolerance_factor: float = 5.0

    def __post_init__(self) -> None:
        if not self.drug or self.drug != self.drug.lower():
            raise ValueError(f"DoseBounds.drug must be lowercase non-empty, got {self.drug!r}")
        if self.min_single_dose_mg <= 0:
            raise ValueError(f"DoseBounds.min_single_dose_mg must be > 0")
        if self.max_single_dose_mg < self.min_single_dose_mg:
            raise ValueError(f"DoseBounds.max < min for {self.drug}")
        if self.tolerance_factor < 1.0:
            raise ValueError(f"DoseBounds.tolerance_factor must be >= 1.0")


# ─── FATAL ERROR RULE ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class FatalErrorRule:
    """
    A single sentinel rule from the ISMP-derived fatal-medication-error list.

    These are NOT clinical data — they are SAFETY LOGIC. The rule encodes
    a known historical fatal error pattern (e.g., "methotrexate + daily" →
    fatal bone marrow suppression) and the regex/condition that detects it
    in output text.

    Rules are versioned and refreshed against the ISMP High-Alert
    Medication List + Sentinel Event publications. They are loaded from
    a versioned config artifact, never embedded in code paths.

    Attributes:
        drug: Canonical drug name (lowercase).
        error_class: ISMP-style category name
                     (e.g., "frequency_confusion", "route_confusion",
                      "unit_confusion", "decimal_place").
        danger_pattern: Compiled regex that, if matched against output
                       text, indicates the fatal error pattern is present.
        safe_pattern: Optional compiled regex; if matched, suppresses the
                      danger flag (e.g., methotrexate + "weekly" present →
                      safe).
        message: Human-readable explanation injected into the output.
        severity: "warn" (warn only), "block" (hard block), "emergency"
                  (hard block + clinical escalation).
        provenance: Source of the rule (ISMP version, publication date).
    """
    drug: str
    error_class: str
    danger_pattern: Pattern[str]
    message: str
    severity: str
    provenance: Provenance
    safe_pattern: Pattern[str] | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.severity not in ("warn", "block", "emergency"):
            raise ValueError(
                f"FatalErrorRule.severity must be warn|block|emergency, got {self.severity!r}"
            )

    def evaluate(self, text: str) -> tuple[bool, str | None]:
        """
        Return (is_violated, reason).

        is_violated=True iff danger_pattern matches AND safe_pattern does
        not match (or there is no safe_pattern).
        """
        if self.drug.lower() not in text.lower():
            return False, None
        if self.danger_pattern.search(text) is None:
            return False, None
        if self.safe_pattern is not None and self.safe_pattern.search(text):
            return False, None
        return True, self.message


# ─── HELPERS ────────────────────────────────────────────────────────────────

def compile_pattern(s: str | None) -> Pattern[str] | None:
    """Compile a regex string with re.I, or return None if input is None."""
    if s is None:
        return None
    return re.compile(s, re.IGNORECASE)
