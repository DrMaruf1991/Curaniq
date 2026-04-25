"""
CURANIQ — Live API End-to-End Test
===================================

Exercises the FastAPI surface with FastAPI TestClient (no real HTTP).
Pins the request/response contracts so any future change to the API
schema or pipeline state breaks loudly.

Run:
    pytest tests/test_api_e2e.py -v

These tests assume CURANIQ_ENV=demo so seed evidence is available.
clinician_prod-mode tests live in test_truth_core_static.py.
"""
from __future__ import annotations
import os
import sys
import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def client():
    """Start the FastAPI app with lifespan events so the pipeline initializes."""
    os.environ.setdefault("CURANIQ_OFFLINE", "1")
    os.environ.setdefault("CURANIQ_ENV", "demo")
    from fastapi.testclient import TestClient
    from curaniq.api.main import app
    with TestClient(app) as c:
        yield c


# ─── system endpoints ────────────────────────────────────────────────────────

def test_health_returns_pipeline_ready(client):
    """/health must report pipeline_ready=True after startup."""
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "operational"
    assert body["pipeline_ready"] is True
    assert "version" in body


def test_info_endpoint_works(client):
    """/info should return 200 — content shape is implementation detail."""
    r = client.get("/info")
    assert r.status_code == 200


# ─── CQL deterministic endpoints ─────────────────────────────────────────────

def test_cql_renal_returns_metformin_action_at_low_egfr(client):
    """L3-2 metformin renal dosing must reduce dose at CrCl 35."""
    r = client.get("/cql/renal", params={"drug": "metformin", "egfr": 35, "crcl": 35})
    assert r.status_code == 200
    body = r.json()
    assert body["drug"] == "metformin"
    # Must indicate dose reduction (action key may be reduce_50pct or similar)
    assert "reduce" in body.get("action", "").lower() or \
           "reduce" in body.get("dose", "").lower()
    assert body.get("computation_id"), "Must return a computation_id for audit"


def test_cql_qt_risk_computes_tisdale_score(client):
    """L3-12 QT risk engine must compute Tisdale score and risk class."""
    r = client.get("/cql/qt_risk", params={"drugs": "azithromycin,fluoxetine,methadone"})
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body.get("tisdale_score"), int)
    assert body.get("tisdale_score") >= 0
    assert body.get("risk") in ("LOW", "MODERATE", "HIGH")
    assert isinstance(body.get("drugs"), list)
    assert len(body["drugs"]) == 3


# ─── /query endpoint — the main API surface ──────────────────────────────────

def test_query_returns_200_for_clinician_query(client):
    """POST /query must return 200 with a structured CURANIQAPIResponse."""
    payload = {
        "query": "56yo on metformin with eGFR 35 mL/min — what's the safe dose?",
        "user_role": "clinician",
        "jurisdiction": "UZ",
        "mode": "quick_answer",
        "patient_context": {
            "age_years": 56,
            "renal": {"egfr_ml_min": 35},
            "active_medications": ["metformin"],
            "conditions": ["type_2_diabetes"],
        },
    }
    r = client.post("/query", json=payload)
    assert r.status_code == 200, f"Body: {r.text[:300]}"
    body = r.json()
    # Must have at minimum: a refusal flag, processing time, mode, response_id
    assert "refused" in body
    assert "processing_time_ms" in body
    assert "mode" in body
    # safety_gates must be a list (gate suite must run)
    assert isinstance(body.get("safety_gates"), list)


def test_query_safety_gate_pipeline_executes(client):
    """L5 safety gate suite must run and report results to the API."""
    payload = {
        "query": "metformin dose at eGFR 35?",
        "user_role": "clinician",
        "jurisdiction": "UZ",
        "patient_context": {"age_years": 56, "renal": {"egfr_ml_min": 35}},
    }
    r = client.post("/query", json=payload)
    assert r.status_code == 200
    gates = r.json().get("safety_gates", [])
    assert len(gates) >= 5, f"Expected the safety suite to run >=5 gates, got {len(gates)}"


def test_query_quick_mode_endpoint_works(client):
    """The /query/quick shortcut must accept minimal payload."""
    r = client.post("/query/quick", json={"query": "metformin renal dose"})
    assert r.status_code == 200
    body = r.json()
    assert "refused" in body


def test_query_validation_rejects_short_input(client):
    """Pydantic validation must reject queries below min_length."""
    r = client.post("/query", json={"query": "x"})
    assert r.status_code == 422


# ─── Audit endpoint ──────────────────────────────────────────────────────────

def test_audit_integrity_verify(client):
    """L9-1 immutable audit ledger integrity check must respond."""
    r = client.get("/audit/integrity/verify")
    # Either 200 with intact ledger, or structured error — never 500
    assert r.status_code in (200, 404, 503)


# ─── Direct runner ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
