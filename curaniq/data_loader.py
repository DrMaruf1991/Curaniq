"""
CURANIQ -- Clinical Data Loader (FIX-34b hardened).

All clinical rule databases loaded from versioned JSON files.
Code = LOGIC. Data files = KNOWLEDGE. Separated by design.

==========================================================================
FIX-34b — Metadata schema unification with curaniq.knowledge
==========================================================================

The `data_loader` pattern predates the FIX-33 `ClinicalKnowledgeProvider`
abstraction. They are now unified by a shared metadata contract:

Every JSON file in `curaniq/data/*.json` SHOULD have a `_metadata` block
with the standard fields used by `VendoredSnapshotProvider`:

    {
      "_metadata": {
        "snapshot_date_iso": "ISO 8601",
        "snapshot_version": "vendored-YYYY.MM.DD.N",
        "license_status": "public_domain|open|restricted|licensed|unknown",
        "extraction_method": "live_api|cached|manual_curation|automated_extraction",
        "is_authoritative": false,
        ...
      },
      "<content key>": [...]
    }

Files without these fields load with a warning. Files with malformed
metadata also load with a warning (not failure — backward compatibility).
Use `get_data_health_report()` to see which files still need backfill.

Environment: CURANIQ_DATA_DIR overrides default path.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_DATA_DIR = Path(__file__).parent / "data"

_REQUIRED_METADATA_FIELDS = {
    "snapshot_date_iso", "snapshot_version", "license_status",
    "extraction_method", "is_authoritative",
}

_METADATA_EXEMPT_FILES: set[str] = set()


def get_data_dir() -> Path:
    """Get clinical data directory. Env override: CURANIQ_DATA_DIR."""
    env_dir = os.environ.get("CURANIQ_DATA_DIR")
    if env_dir:
        return Path(env_dir)
    return _DEFAULT_DATA_DIR


def _validate_metadata(filename: str, data: Any) -> dict[str, Any]:
    """Check loaded data for proper FIX-33 provenance metadata. Never raises."""
    result: dict[str, Any] = {
        "status": "ok",
        "is_authoritative": None,
        "snapshot_date_iso": None,
        "snapshot_version": None,
    }
    if filename in _METADATA_EXEMPT_FILES:
        result["status"] = "exempt"
        return result
    if not isinstance(data, dict):
        result["status"] = "not_a_dict"
        return result
    md = data.get("_metadata")
    if md is None:
        result["status"] = "missing_metadata"
        logger.warning(
            "data_loader: %s has no _metadata block — provenance unknown.",
            filename,
        )
        return result
    if not isinstance(md, dict):
        result["status"] = "missing_metadata"
        logger.warning("data_loader: %s _metadata is not a dict", filename)
        return result
    missing = _REQUIRED_METADATA_FIELDS - set(md.keys())
    if missing:
        result["status"] = "incomplete_metadata"
        result["missing_fields"] = sorted(missing)
        logger.info(
            "data_loader: %s metadata incomplete (missing %s) — pre-FIX-33 file.",
            filename, sorted(missing),
        )
    result["is_authoritative"] = md.get("is_authoritative")
    result["snapshot_date_iso"] = md.get("snapshot_date_iso")
    result["snapshot_version"] = md.get("snapshot_version")
    return result


def load_json_data(filename: str) -> Any:
    """
    Load a clinical data file from the data directory.
    Returns empty dict/list on failure. FIX-34b: validates metadata schema.
    """
    filepath = get_data_dir() / filename
    if not filepath.exists():
        logger.warning("Clinical data file not found: %s", filepath)
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.error("Failed to parse %s: %s", filepath, e)
        return {}

    _validate_metadata(filename, data)
    logger.info("Loaded %s: %s entries", filename,
                 len(data) if isinstance(data, (list, dict)) else "?")
    return data


def get_data_manifest() -> dict[str, dict]:
    """List all data files with metadata, entry count, and provenance status."""
    data_dir = get_data_dir()
    manifest: dict[str, dict] = {}
    if not data_dir.exists():
        return manifest
    for filepath in sorted(data_dir.glob("*.json")):
        entry: dict[str, Any] = {
            "size_kb": round(filepath.stat().st_size / 1024, 1),
            "modified": filepath.stat().st_mtime,
        }
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            entry["entries"] = len(data) if isinstance(data, (list, dict)) else 0
            v = _validate_metadata(filepath.name, data)
            entry["metadata_status"] = v["status"]
            entry["is_authoritative"] = v["is_authoritative"]
            entry["snapshot_version"] = v["snapshot_version"]
            entry["snapshot_date_iso"] = v["snapshot_date_iso"]
            if v.get("missing_fields"):
                entry["missing_metadata_fields"] = v["missing_fields"]
        except Exception as exc:
            entry["entries"] = 0
            entry["error"] = str(exc)
        manifest[filepath.name] = entry
    return manifest


def get_data_health_report() -> dict[str, Any]:
    """
    Summarize metadata completeness across all data files.
    Used by tests/test_data_layer_health.py to track FIX-34b backfill progress.
    """
    manifest = get_data_manifest()
    counts: dict[str, int] = {"ok": 0, "incomplete_metadata": 0,
                               "missing_metadata": 0, "not_a_dict": 0, "exempt": 0}
    needing_backfill: list[str] = []
    for filename, entry in manifest.items():
        status = entry.get("metadata_status", "missing_metadata")
        counts[status] = counts.get(status, 0) + 1
        if status in ("incomplete_metadata", "missing_metadata"):
            needing_backfill.append(filename)
    return {
        "total_files": len(manifest),
        **counts,
        "files_needing_backfill": sorted(needing_backfill),
    }
