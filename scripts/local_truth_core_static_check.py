"""No-dependency static smoke checks for environments without pydantic/fastapi."""
from __future__ import annotations

import ast
from pathlib import Path

root = Path(__file__).resolve().parents[1]
files = [p for p in (root / "curaniq").rglob("*.py")]
failures = []
for path in files:
    try:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except Exception as exc:
        failures.append((str(path.relative_to(root)), repr(exc)))

required_strings = {
    "curaniq/core/pipeline_components.py": [
        "fail_closed_no_live_evidence_seed_disabled",
        "Mock generation is disabled in clinician_prod mode",
        "seed_demo_only_bm25_like",
    ],
    "curaniq/core/pipeline.py": ["TruthCorePolicy.from_environment", "allow_seed_evidence"],
    "curaniq/api/main.py": ["Clinician role cannot be self-declared in clinician_prod", "CURANIQ_API_KEY"],
    "curaniq/layers/L1_evidence_ingestion/evidence_retriever.py": ["source_last_updated_at", "source_version"],
    "curaniq/models/schemas.py": ["NEGATIVE_TRIAL", "UNKNOWN", "SAFETY_WARNING", "INTL"],
}
for rel, needles in required_strings.items():
    text = (root / rel).read_text(encoding="utf-8")
    for needle in needles:
        if needle not in text:
            failures.append((rel, f"missing required string: {needle}"))

if failures:
    for rel, msg in failures:
        print(f"FAIL {rel}: {msg}")
    raise SystemExit(1)
print(f"STATIC_TRUTH_CORE_OK files={len(files)}")
