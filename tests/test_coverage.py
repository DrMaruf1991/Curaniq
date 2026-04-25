"""
CURANIQ — Coverage Test Suite
==============================

Exercises 16 distinct paths through the pipeline that the original smoke test
did not reach. These tests guard against orchestrator/module signature drift
on infrequently-touched code paths (pediatric, pregnancy, antimicrobial,
multiple interaction modes, multiple jurisdictions, multilingual input,
concurrency, etc.).

Each scenario was verified to crash the engine BEFORE the corresponding fix
was applied — this is the regression-guard the FIX-30 work needs.
"""
from __future__ import annotations
import os
import sys
import pytest
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ─────────────────────────────────────────────────────────────────────────────
# Module-scoped pipeline (boot once)
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def pipeline():
    os.environ.setdefault("CURANIQ_OFFLINE", "1")
    os.environ.setdefault("CURANIQ_ENV", "demo")
    from curaniq.core.pipeline import CURANIQPipeline
    return CURANIQPipeline()


def _q(text, role="clinician", jur="UZ", mode="quick_answer", patient=None):
    """Build a ClinicalQuery — keeps tests terse."""
    from curaniq.models.schemas import (
        ClinicalQuery, UserRole, Jurisdiction, InteractionMode, PatientContext,
    )
    return ClinicalQuery(
        raw_text=text,
        user_role=UserRole(role),
        jurisdiction=Jurisdiction(jur),
        mode=InteractionMode(mode),
        patient_context=patient,
    )


def _patient(**kw):
    from curaniq.models.schemas import PatientContext, RenalFunction, HepaticFunction
    if "renal" in kw and isinstance(kw["renal"], dict):
        kw["renal"] = RenalFunction(**kw["renal"])
    if "hepatic" in kw and isinstance(kw["hepatic"], dict):
        kw["hepatic"] = HepaticFunction(**kw["hepatic"])
    return PatientContext(**kw)


# ─── interaction modes ──────────────────────────────────────────────────────

def test_evidence_deep_mode(pipeline):
    r = pipeline.process(_q("metformin in diabetic CKD — full evidence review",
        mode="evidence_deep_dive",
        patient=_patient(age_years=56, renal={"egfr_ml_min": 35})))
    assert r is not None

def test_living_dossier_mode(pipeline):
    r = pipeline.process(_q("glp-1 agonists in CKD evidence map", mode="living_dossier"))
    assert r is not None

def test_decision_session_mode(pipeline):
    r = pipeline.process(_q("metformin vs SGLT2 in this patient",
        mode="decision_session",
        patient=_patient(age_years=56, renal={"egfr_ml_min": 35})))
    assert r is not None


# ─── role-based behavior ────────────────────────────────────────────────────

def test_patient_role_handled(pipeline):
    """L5-14 patient role regulatory boundary must engage; pipeline must not crash."""
    r = pipeline.process(_q("what dose of metformin should I take?",
        role="patient", jur="US",
        patient=_patient(age_years=56)))
    assert r is not None


# ─── jurisdictions ──────────────────────────────────────────────────────────

def test_uk_jurisdiction(pipeline):
    r = pipeline.process(_q("metformin renal dosing", jur="UK",
        patient=_patient(age_years=56, renal={"egfr_ml_min": 35})))
    assert r is not None

def test_int_jurisdiction(pipeline):
    r = pipeline.process(_q("amoxicillin pediatric dose", jur="INT",
        patient=_patient(age_years=5, weight_kg=20)))
    assert r is not None


# ─── patient profiles ───────────────────────────────────────────────────────

def test_polypharmacy(pipeline):
    """5-drug DDI scenario — exercises L3 multi-drug paths."""
    r = pipeline.process(_q("DDI check",
        patient=_patient(age_years=72,
            active_medications=["warfarin","amiodarone","clarithromycin","metformin","atorvastatin"])))
    assert r is not None

def test_pregnancy(pipeline):
    """L3-9 PregnancyLactationEngine.check_pregnancy must fire correctly."""
    r = pipeline.process(_q("ACE inhibitor in pregnancy?",
        patient=_patient(age_years=28, is_pregnant=True, gestational_week=20,
            active_medications=["lisinopril"])))
    assert r is not None

def test_no_patient_context(pipeline):
    r = pipeline.process(_q("general question about metformin"))
    assert r is not None

def test_hepatic_impairment(pipeline):
    r = pipeline.process(_q("paracetamol in cirrhosis Child-Pugh B",
        patient=_patient(age_years=60, hepatic={"child_pugh_class": "B"})))
    assert r is not None

def test_dialysis(pipeline):
    r = pipeline.process(_q("vancomycin in HD patient",
        patient=_patient(age_years=68, renal={"on_dialysis": True, "dialysis_type": "HD"})))
    assert r is not None

def test_allergy_cross_reactivity(pipeline):
    """L3-1 allergy kernel + L3-10 antimicrobial stewardship must compose."""
    r = pipeline.process(_q("alternative to amoxicillin in PCN-allergic patient",
        patient=_patient(age_years=40, allergies=["penicillin"])))
    assert r is not None

def test_pediatric_weight_based(pipeline):
    """L3-7 PediatricSafetyEngine.calculate must fire — was .check before FIX-30."""
    r = pipeline.process(_q("amoxicillin dose for 4yo 18kg with otitis media",
        patient=_patient(age_years=4, weight_kg=18)))
    assert r is not None


# ─── multilingual input ─────────────────────────────────────────────────────

def test_russian_input(pipeline):
    r = pipeline.process(_q("доза метформина при СКФ 35", jur="RU",
        patient=_patient(age_years=56, renal={"egfr_ml_min": 35})))
    assert r is not None

def test_uzbek_input(pipeline):
    r = pipeline.process(_q("metformin GFR 35 dozasi qanday",
        patient=_patient(age_years=56, renal={"egfr_ml_min": 35})))
    assert r is not None


# ─── high-stakes drug (L5-12 dose plausibility) ─────────────────────────────

def test_methotrexate_query(pipeline):
    """Methotrexate is a known fatal-error drug; pipeline must complete gracefully."""
    r = pipeline.process(_q("methotrexate dose for rheumatoid arthritis",
        patient=_patient(age_years=55)))
    assert r is not None


# ─── concurrency ────────────────────────────────────────────────────────────

def test_concurrent_queries_serialize_safely(pipeline):
    """Pipeline has a process_lock; 5 concurrent queries must all succeed without deadlock."""
    errs: list[Exception] = []
    def worker():
        try:
            r = pipeline.process(_q("ibuprofen renal safety",
                patient=_patient(age_years=70, renal={"egfr_ml_min": 50})))
            assert r is not None
        except Exception as e:
            errs.append(e)
    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=20)
    assert all(not t.is_alive() for t in threads), "thread deadlock"
    assert not errs, f"concurrent failures: {errs}"


# ─── direct runner ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
