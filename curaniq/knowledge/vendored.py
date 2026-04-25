"""
CURANIQ Clinical Knowledge — VendoredSnapshotProvider.

Loads versioned, fully-provenance-tagged clinical-data snapshots from
`curaniq/data/clinical/*.json` and rule artifacts from
`curaniq/data/rules/*.json`.

Strict policy:
- Refuses to instantiate in `clinician_prod`. The boot-time tripwire
  catches any clinician_prod boot that would have served vendored facts.
- Validates provenance metadata on every file. A file without a valid
  Provenance block fails fast at load time — never silently used.
- Returned facts carry the file's provenance, propagated to L9-1 audit
  via the L4-3 claim contract.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterator, Iterable

from curaniq.knowledge.exceptions import (
    KnowledgeUnavailableError,
    ProvenanceMissingError,
    VendoredDataRefusedError,
)
from curaniq.knowledge.types import (
    DoseBounds,
    FatalErrorRule,
    Provenance,
    compile_pattern,
)
from curaniq.truth_core.config import is_clinician_prod

logger = logging.getLogger(__name__)

DATA_ROOT = Path(__file__).parent.parent / "data"
CLINICAL_DIR = DATA_ROOT / "clinical"
RULES_DIR = DATA_ROOT / "rules"


def _load_snapshot(path: Path) -> dict:
    """Load a JSON snapshot, validate metadata, return parsed dict."""
    if not path.exists():
        raise KnowledgeUnavailableError(
            fact=path.stem, reason=f"snapshot file missing: {path}"
        )
    with path.open("r", encoding="utf-8") as f:
        try:
            doc = json.load(f)
        except json.JSONDecodeError as exc:
            raise KnowledgeUnavailableError(
                fact=path.stem, reason=f"snapshot {path.name} is not valid JSON: {exc}"
            ) from exc
    if "_metadata" not in doc:
        raise ProvenanceMissingError(
            f"snapshot {path.name} has no _metadata block — "
            "vendored data without provenance is forbidden"
        )
    md = doc["_metadata"]
    required = {"snapshot_date_iso", "snapshot_version", "license_status",
                "extraction_method", "is_authoritative"}
    missing = required - set(md.keys())
    if missing:
        raise ProvenanceMissingError(
            f"snapshot {path.name} _metadata missing keys: {sorted(missing)}"
        )
    return doc


def _provenance_from_metadata(md: dict, source: str, source_url: str) -> Provenance:
    """Build a Provenance record from a snapshot's _metadata block."""
    return Provenance(
        source=source,
        source_url=source_url,
        snapshot_date_iso=md["snapshot_date_iso"],
        evidence_version=md["snapshot_version"],
        license_status=md["license_status"],
        extraction_method=md["extraction_method"],
        is_authoritative=bool(md["is_authoritative"]),
    )


class VendoredSnapshotProvider:
    """
    Serves clinical knowledge from vendored JSON snapshots.

    Refuses to instantiate when `is_clinician_prod()` is True. This is a
    boot-time tripwire — by the time clinical engines need knowledge,
    they already have a non-vendored provider or the boot has failed.

    Public properties:
        name: 'vendored'
        is_authoritative: False  (always — by definition)

    Loaded artifacts:
        clinical/dose_bounds.json   — DoseBounds, non-authoritative
        rules/fatal_dose_errors.json — FatalErrorRule, authoritative (rules,
                                       not data; the patterns ARE the rule)
    """

    name = "vendored"
    is_authoritative = False

    def __init__(self, *, allow_in_prod: bool = False) -> None:
        if is_clinician_prod() and not allow_in_prod:
            raise VendoredDataRefusedError(
                "VendoredSnapshotProvider cannot be instantiated in clinician_prod. "
                "Use LiveEvidenceProvider or RouterProvider with a live backend."
            )
        self._dose_bounds: dict[str, DoseBounds] = {}
        self._fatal_rules: list[FatalErrorRule] = []
        self._loaded = False

    # ─── EAGER LOADING ────────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._load_dose_bounds()
        self._load_fatal_error_rules()
        self._loaded = True
        logger.info(
            "VendoredSnapshotProvider loaded: %d dose bounds, %d fatal-error rules",
            len(self._dose_bounds), len(self._fatal_rules),
        )

    def _load_dose_bounds(self) -> None:
        path = CLINICAL_DIR / "dose_bounds.json"
        doc = _load_snapshot(path)
        md = doc["_metadata"]
        # Source for these specific bounds is DailyMed
        prov = _provenance_from_metadata(
            md, source="DAILYMED",
            source_url="https://dailymed.nlm.nih.gov/dailymed/",
        )
        seen = set()
        for entry in doc.get("bounds", []):
            drug = entry["drug"].lower().strip()
            if drug in seen:
                logger.warning("dose_bounds.json: duplicate drug %r; first wins", drug)
                continue
            seen.add(drug)
            self._dose_bounds[drug] = DoseBounds(
                drug=drug,
                min_single_dose_mg=float(entry["min_single_dose_mg"]),
                max_single_dose_mg=float(entry["max_single_dose_mg"]),
                route_context=entry["route_context"],
                tolerance_factor=float(entry.get("tolerance_factor", 5.0)),
                provenance=prov,
            )

    def _load_fatal_error_rules(self) -> None:
        path = RULES_DIR / "fatal_dose_errors.json"
        doc = _load_snapshot(path)
        md = doc["_metadata"]
        prov = _provenance_from_metadata(
            md, source=md.get("source", "ISMP_SENTINEL_LIST"),
            source_url=md.get("source_url",
                              "https://www.ismp.org/recommendations/high-alert-medications-acute-list"),
        )
        for entry in doc.get("rules", []):
            self._fatal_rules.append(FatalErrorRule(
                drug=entry["drug"].lower().strip(),
                error_class=entry["error_class"],
                danger_pattern=compile_pattern(entry["danger_pattern"]),  # type: ignore[arg-type]
                safe_pattern=compile_pattern(entry.get("safe_pattern")),
                message=entry["message"],
                severity=entry["severity"],
                provenance=prov,
                extras=entry.get("extras", {}),
            ))

    # ─── PROVIDER PROTOCOL ────────────────────────────────────────────────

    def get_dose_bounds(self, drug: str, jurisdiction: str = "US") -> DoseBounds | None:
        self._ensure_loaded()
        return self._dose_bounds.get(drug.lower().strip())

    def iter_fatal_error_rules(self) -> Iterator[FatalErrorRule]:
        self._ensure_loaded()
        yield from self._fatal_rules

    # ─── INTROSPECTION ────────────────────────────────────────────────────

    def known_drugs_with_bounds(self) -> Iterable[str]:
        """Drug names for which this provider has dose bounds. For diagnostics."""
        self._ensure_loaded()
        return sorted(self._dose_bounds.keys())

    def fatal_rule_count(self) -> int:
        """For diagnostics."""
        self._ensure_loaded()
        return len(self._fatal_rules)
