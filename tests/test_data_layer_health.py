"""
FIX-34b — Data layer health monitoring test.

Reports on metadata schema completeness across all `curaniq/data/*.json`
files. Does NOT fail the build today (the 31 pre-FIX-33 files are
allowed to lack the new metadata fields), but pins the current state
and surfaces the gap in CI output for visibility.

When pre-FIX-33 files are backfilled with proper metadata:
  - This test's "expected baseline" auto-shifts (the test reads the
    actual report and compares to the EXPECTED_INCOMPLETE_BASELINE).
  - When `incomplete_metadata` count drops below baseline, update
    EXPECTED_INCOMPLETE_BASELINE to lock in the progress.
  - When it reaches 0, the architecture promise is delivered:
    every clinical-data file flows through validated provenance.
"""
from __future__ import annotations

import sys

import pytest


# Baseline at audit time (2026-04-25). When backfill happens, lower this.
EXPECTED_INCOMPLETE_BASELINE = 31


def test_data_layer_health_report_runs():
    """The health report itself must work — no exceptions, all files inventoried."""
    from curaniq.data_loader import get_data_health_report
    report = get_data_health_report()
    assert "total_files" in report
    assert report["total_files"] >= 1
    # Sum of all status counts equals total
    counted = (report.get("ok", 0) + report.get("incomplete_metadata", 0)
               + report.get("missing_metadata", 0) + report.get("not_a_dict", 0)
               + report.get("exempt", 0))
    assert counted == report["total_files"]


def test_no_files_with_completely_missing_metadata():
    """
    Stronger guarantee: no file should have ZERO metadata. They may have
    incomplete metadata (pre-FIX-33 schema), but the _metadata block must
    exist. The 31 legacy files all already have _metadata; this test
    fails the build if anyone adds a new clinical data file without it.
    """
    from curaniq.data_loader import get_data_health_report
    report = get_data_health_report()
    missing = report.get("missing_metadata", 0)
    assert missing == 0, (
        f"{missing} files have NO _metadata block at all. "
        f"Every clinical data file must have at least a _metadata block. "
        f"See curaniq/data_loader.py FIX-34b documentation."
    )


def test_incomplete_metadata_count_does_not_grow():
    """
    Soft gate: the number of files with incomplete metadata must not
    GROW. New clinical data files added to `curaniq/data/*.json` must
    have full FIX-33 metadata (snapshot_date_iso, snapshot_version,
    license_status, extraction_method, is_authoritative).

    If this test fails because someone added a non-conforming file:
        backfill the file's _metadata to FIX-33 schema.
    If this test fails because someone backfilled a file (count went DOWN):
        update EXPECTED_INCOMPLETE_BASELINE to lock in the progress.
    """
    from curaniq.data_loader import get_data_health_report
    report = get_data_health_report()
    incomplete = report.get("incomplete_metadata", 0)
    print(f"\nFIX-34b backfill progress: {incomplete}/{EXPECTED_INCOMPLETE_BASELINE} "
          f"files still need metadata backfill",
          file=sys.stderr)
    assert incomplete <= EXPECTED_INCOMPLETE_BASELINE, (
        f"Number of files with incomplete metadata grew from "
        f"{EXPECTED_INCOMPLETE_BASELINE} to {incomplete}. New clinical-data "
        f"JSON files MUST have full FIX-33 metadata. Files needing backfill: "
        f"{report.get('files_needing_backfill', [])}"
    )


def test_data_layer_consistent_with_knowledge_provider_artifacts():
    """
    The new FIX-33 vendored artifacts (dose_bounds.json, drug_synonyms.json,
    cis_drug_variants.json, fatal_dose_errors.json) must have FULL
    FIX-33 metadata. This is the canonical example for backfill.
    """
    import json
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    canonical_files = [
        repo_root / "curaniq" / "data" / "clinical" / "dose_bounds.json",
        repo_root / "curaniq" / "data" / "clinical" / "drug_synonyms.json",
        repo_root / "curaniq" / "data" / "clinical" / "cis_drug_variants.json",
        repo_root / "curaniq" / "data" / "rules" / "fatal_dose_errors.json",
    ]
    required = {"snapshot_date_iso", "snapshot_version", "license_status",
                "extraction_method", "is_authoritative"}
    for f in canonical_files:
        assert f.exists(), f"Canonical FIX-33 artifact missing: {f}"
        with f.open() as fh:
            doc = json.load(fh)
        md = doc.get("_metadata") or {}
        missing = required - set(md.keys())
        assert not missing, (
            f"{f.name} is missing required FIX-33 metadata fields: {missing}. "
            f"This is a CANONICAL EXAMPLE — it MUST conform."
        )
