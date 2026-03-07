"""
CURANIQ -- Clinical Data Loader
All clinical rule databases loaded from versioned JSON files.
Code = LOGIC. Data files = KNOWLEDGE. Separated by design.

Data files live in curaniq/data/*.json and are:
- Version-controlled (git-tracked, diff-reviewable)
- Clinician-editable (JSON, no Python knowledge needed)
- Independently updatable (no code deploy for data changes)
- Source-cited (every entry has a source field)

Environment: CURANIQ_DATA_DIR overrides default path.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Default data directory: curaniq/data/ relative to this file
_DEFAULT_DATA_DIR = Path(__file__).parent / "data"


def get_data_dir() -> Path:
    """Get clinical data directory. Env override: CURANIQ_DATA_DIR."""
    env_dir = os.environ.get("CURANIQ_DATA_DIR")
    if env_dir:
        return Path(env_dir)
    return _DEFAULT_DATA_DIR


def load_json_data(filename: str) -> Any:
    """
    Load a clinical data file from the data directory.
    Returns empty dict/list on failure (fail-safe, not fail-silent — logs warning).
    """
    filepath = get_data_dir() / filename
    if not filepath.exists():
        logger.warning("Clinical data file not found: %s", filepath)
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info("Loaded %s: %s entries", filename,
                     len(data) if isinstance(data, (list, dict)) else "?")
        return data
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.error("Failed to parse %s: %s", filepath, e)
        return {}


def get_data_manifest() -> dict[str, dict]:
    """List all data files with metadata (size, entry count, last modified)."""
    data_dir = get_data_dir()
    manifest = {}
    if not data_dir.exists():
        return manifest
    for filepath in sorted(data_dir.glob("*.json")):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            entry_count = len(data) if isinstance(data, (list, dict)) else 0
            manifest[filepath.name] = {
                "entries": entry_count,
                "size_kb": round(filepath.stat().st_size / 1024, 1),
                "modified": filepath.stat().st_mtime,
            }
        except Exception:
            manifest[filepath.name] = {"entries": 0, "error": True}
    return manifest
