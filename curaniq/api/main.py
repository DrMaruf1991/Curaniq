"""
CURANIQ — FastAPI Application
All API routes for the Medical Evidence Operating System.
"""
from __future__ import annotations
import asyncio
import logging
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from curaniq.core.pipeline import CURANIQPipeline
from curaniq.models.schemas import (
    ClinicalQuery,
    CURANIQResponse,
    Jurisdiction,
    InteractionMode,
    PatientContext,
    RenalFunction,
    UserRole,
)

app = FastAPI(
    title="CURANIQ — Medical Evidence Operating System",
    description=(
        "Evidence-locked, fail-closed medical AI. "
        "Every claim has a citation. Every citation is verified. "
        "Every number is deterministic or verbatim-quoted."
    ),
    version="1.0.0",
)

# CORS: Environment-driven. No hardcoded origins.
# Set CURANIQ_CORS_ORIGINS="https://curaniq.com,https://app.curaniq.com"
# Default: localhost only (secure by default)
_cors_env = os.environ.get("CURANIQ_CORS_ORIGINS", "")
_cors_origins = (
    [o.strip() for o in _cors_env.split(",") if o.strip()]
    if _cors_env
    else ["http://localhost:3000", "http://localhost:8080"]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Authorization"],
)

_pipeline: Optional[CURANIQPipeline] = None

@app.on_event("startup")
async def startup_event():
    global _pipeline
    _pipeline = CURANIQPipeline()

def get_pipeline() -> CURANIQPipeline:
    if _pipeline is None:
        raise HTTPException(status_code=503, detail="Pipeline not initialized")
    return _pipeline

class RenalFunctionRequest(BaseModel):
    egfr_ml_min: Optional[float] = None
    crcl_ml_min: Optional[float] = None
    on_dialysis: bool = False
    dialysis_type: Optional[str] = None

class PatientContextRequest(BaseModel):
    age_years: Optional[int] = None
    weight_kg: Optional[float] = None
    sex_at_birth: Optional[str] = None
    is_pregnant: bool = False
    is_breastfeeding: bool = False
    renal: Optional[RenalFunctionRequest] = None
    active_medications: list[str] = Field(default_factory=list)
    allergies: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    jurisdiction: str = "INT"

class ClinicalQueryRequest(BaseModel):
    query: str = Field(..., min_length=3, max_length=4000)
    user_role: str = Field(default="clinician")
    mode: Optional[str] = None
    patient_context: Optional[PatientContextRequest] = None
    jurisdiction: str = Field(default="INT")
    session_id: Optional[str] = None

class EvidenceCardResponse(BaseModel):
    claim_text: str
    claim_type: str
    confidence_level: str
    confidence_score: float
    grade: Optional[str]
    sources: list[dict]
    safety_flags: list[str]
    uncertainty_marker: Optional[str]
    caveat: Optional[str]
    numeric_verified: bool

class SafetyGateResponse(BaseModel):
    gate_id: str
    gate_name: str
    passed: bool
    message: Optional[str]
    severity: str

class FreshnessStampResponse(BaseModel):
    source_type: str
    display_text: str
    is_stale: bool

class CURANIQAPIResponse(BaseModel):
    query_id: str
    mode: str
    user_role: str
    evidence_cards: list[EvidenceCardResponse]
    summary_text: Optional[str]
    safe_next_steps: list[str]
    monitoring_required: list[str]
    escalation_thresholds: list[str]
    follow_up_interval: Optional[str]
    triage_result: str
    triage_message: Optional[str]
    claim_contract_enforced: bool
    safety_gates: list[SafetyGateResponse]
    safety_passed: bool
    hard_blocked: bool
    freshness_stamps: list[FreshnessStampResponse]
    sources_used: int
    refused: bool
    refusal_reason: Optional[str]
    audit_ledger_id: Optional[str]
    processing_time_ms: Optional[float]
    generated_at: str

def _serialize_response(resp: CURANIQResponse) -> CURANIQAPIResponse:
    return CURANIQAPIResponse(
        query_id=str(resp.query_id),
        mode=resp.mode.value,
        user_role=resp.user_role.value,
        evidence_cards=[
            EvidenceCardResponse(
                claim_text=c.claim_text,
                claim_type=c.claim_type.value,
                confidence_level=c.confidence_level.value,
                confidence_score=c.confidence_score,
                grade=c.grade.value if c.grade else None,
                sources=c.sources,
                safety_flags=[f.value for f in c.safety_flags],
                uncertainty_marker=c.uncertainty_marker,
                caveat=c.caveat,
                numeric_verified=c.numeric_verified,
            )
            for c in resp.evidence_cards
        ],
        summary_text=resp.summary_text,
        safe_next_steps=resp.safe_next_steps,
        monitoring_required=resp.monitoring_required,
        escalation_thresholds=resp.escalation_thresholds,
        follow_up_interval=resp.follow_up_interval,
        triage_result=resp.triage.result.value,
        triage_message=resp.triage.escalation_message,
        claim_contract_enforced=resp.claim_contract_enforced,
        safety_gates=[
            SafetyGateResponse(
                gate_id=g.gate_id,
                gate_name=g.gate_name,
                passed=g.passed,
                message=g.message,
                severity=g.severity,
            )
            for g in resp.safety_suite.gates
        ],
        safety_passed=resp.safety_suite.overall_passed,
        hard_blocked=resp.safety_suite.hard_block,
        freshness_stamps=[
            FreshnessStampResponse(
                source_type=f.source_type.value,
                display_text=f.display_text,
                is_stale=f.is_stale,
            )
            for f in resp.freshness_stamps
        ],
        sources_used=resp.sources_used,
        refused=resp.refused,
        refusal_reason=resp.refusal_reason,
        audit_ledger_id=str(resp.audit_ledger_id) if resp.audit_ledger_id else None,
        processing_time_ms=resp.processing_time_ms,
        generated_at=resp.generated_at.isoformat(),
    )

def _build_query(req: ClinicalQueryRequest) -> ClinicalQuery:
    patient_ctx: Optional[PatientContext] = None
    if req.patient_context:
        pc = req.patient_context
        renal = None
        if pc.renal:
            renal = RenalFunction(
                egfr_ml_min=pc.renal.egfr_ml_min,
                crcl_ml_min=pc.renal.crcl_ml_min,
                on_dialysis=pc.renal.on_dialysis,
                dialysis_type=pc.renal.dialysis_type,
            )
        patient_ctx = PatientContext(
            age_years=pc.age_years,
            weight_kg=pc.weight_kg,
            sex_at_birth=pc.sex_at_birth,
            is_pregnant=pc.is_pregnant,
            is_breastfeeding=pc.is_breastfeeding,
            renal=renal,
            active_medications=pc.active_medications,
            allergies=pc.allergies,
            conditions=pc.conditions,
            jurisdiction=Jurisdiction(pc.jurisdiction),
        )
    mode = None
    if req.mode:
        try:
            mode = InteractionMode(req.mode)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid mode '{req.mode}'")
    try:
        role = UserRole(req.user_role)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid role '{req.user_role}'")
    try:
        jurisdiction = Jurisdiction(req.jurisdiction)
    except ValueError:
        jurisdiction = Jurisdiction.INT
    return ClinicalQuery(
        raw_text=req.query,
        user_role=role,
        mode=mode,
        patient_context=patient_ctx,
        jurisdiction=jurisdiction,
        session_id=UUID(req.session_id) if req.session_id else None,
    )

@app.get("/health", tags=["System"])
async def health_check():
    return {
        "status": "operational",
        "system": "CURANIQ Medical Evidence Operating System",
        "version": "1.0.0",
        "pipeline_ready": _pipeline is not None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

@app.get("/info", tags=["System"])
async def system_info():
    return {
        "product": "CURANIQ",
        "tagline": "Medical Evidence Operating System",
        "thesis": "This system will never tell you a clinical claim without showing you the evidence, the certainty, and when it was last checked.",
        "architecture": {"version": "3.6", "modules": 181, "layers": 15},
        "interaction_modes": {
            "quick_answer": "Fast clinical answer (~5s)",
            "evidence_deep_dive": "Multi-source synthesis (~60s)",
            "living_dossier": "Persistent topic tracker",
            "decision_session": "What-if toggles, Second Opinion",
            "document_processing": "Upload guidelines/protocols",
        },
    }

@app.post("/query", response_model=CURANIQAPIResponse, tags=["Clinical Query"])
async def process_clinical_query(request: ClinicalQueryRequest):
    pipeline = get_pipeline()
    query = _build_query(request)
    try:
        # pipeline.process() is sync and takes 2-30s. Run in thread to avoid
        # blocking the event loop (which would starve ALL concurrent requests).
        response = await asyncio.to_thread(pipeline.process, query)
        return _serialize_response(response)
    except Exception as e:
        logging.getLogger(__name__).error("Pipeline error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal pipeline error. Check server logs.")

@app.post("/query/quick", response_model=CURANIQAPIResponse, tags=["Clinical Query"])
async def quick_answer(request: ClinicalQueryRequest):
    request.mode = "quick_answer"
    return await process_clinical_query(request)

@app.post("/query/deep", response_model=CURANIQAPIResponse, tags=["Clinical Query"])
async def evidence_deep_dive(request: ClinicalQueryRequest):
    request.mode = "evidence_deep_dive"
    return await process_clinical_query(request)

@app.get("/audit/{query_id}", tags=["Audit"])
async def get_audit_trail(query_id: str):
    pipeline = get_pipeline()
    try:
        qid = UUID(query_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid query_id")
    entries = pipeline.audit_ledger.get_query_audit_trail(qid)
    if not entries:
        raise HTTPException(status_code=404, detail="No audit entries found")
    return {"query_id": query_id, "entries": [
        {
            "entry_id": str(e.entry_id),
            "user_role": e.user_role.value,
            "triage_result": e.triage_result.value,
            "safety_passed": e.safety_suite_passed,
            "refused": e.refused,
            "entry_hash": e.entry_hash,
            "created_at": e.created_at.isoformat(),
        } for e in entries
    ]}

@app.get("/audit/integrity/verify", tags=["Audit"])
async def verify_audit_integrity():
    return get_pipeline().audit_ledger.verify_integrity()

@app.get("/triage/test", tags=["Safety"])
async def test_triage(query: str):
    pipeline = get_pipeline()
    triage = pipeline.triage_gate.assess(query)
    return {
        "query": query,
        "triage_result": triage.result.value,
        "triggered_criteria": triage.triggered_criteria,
        "escalation_message": triage.escalation_message,
        "pipeline_halts": triage.result.value == "emergency",
    }

@app.get("/cql/renal", tags=["CQL"])
async def cql_renal(drug: str, crcl: float):
    from curaniq.core.cql_kernel import get_renal_dose_adjustment
    result = get_renal_dose_adjustment(drug, crcl)
    if not result:
        return {"drug": drug, "crcl": crcl, "result": "Not in CQL database"}
    rule, log = result
    return {"drug": drug, "crcl": crcl, "action": rule["action"], "dose": rule["dose"],
            "computation_id": log.computation_id}

@app.get("/cql/allergy", tags=["CQL"])
async def cql_allergy(allergy: str, proposed_drug: str):
    from curaniq.core.cql_kernel import check_allergy_cross_reactivity
    risk, log = check_allergy_cross_reactivity(allergy, proposed_drug)
    return {"allergy": allergy, "proposed_drug": proposed_drug,
            "risk_found": risk is not None, "risk": risk,
            "computation_id": log.computation_id}

@app.get("/cql/qt_risk", tags=["CQL"])
async def cql_qt(drugs: str, qtc_ms: Optional[float] = None,
                  serum_k_meq: Optional[float] = None,
                  age: Optional[int] = None, sex: Optional[str] = None):
    from curaniq.core.cql_kernel import compute_tisdale_qt_score
    drug_list = [d.strip() for d in drugs.split(",")]
    score, risk, log = compute_tisdale_qt_score(drug_list, qtc_ms, serum_k_meq, age, sex)
    return {"drugs": drug_list, "tisdale_score": score, "risk": risk,
            "computation_id": log.computation_id}

@app.get("/cql/egfr", tags=["CQL"])
async def cql_egfr(age: int, creatinine_umol_l: float, sex: str,
                    weight_kg: Optional[float] = None):
    from curaniq.core.cql_kernel import compute_ckd_epi, compute_cockcroft_gault
    egfr, log = compute_ckd_epi(age, creatinine_umol_l, sex)
    result: dict = {"egfr": egfr, "unit": "mL/min/1.73m2", "computation_id": log.computation_id}
    if weight_kg:
        crcl, log2 = compute_cockcroft_gault(age, weight_kg, creatinine_umol_l, sex)
        result["crcl"] = crcl
        result["crcl_computation_id"] = log2.computation_id
    return result

@app.get("/cql/pregnancy", tags=["CQL"])
async def cql_pregnancy(drug: str):
    from curaniq.core.cql_kernel import get_pregnancy_risk
    entry, log = get_pregnancy_risk(drug)
    if not entry:
        return {"drug": drug, "result": "Not in database"}
    return {"drug": drug, "category": entry.get("category"),
            "risk": entry.get("risk"), "note": entry.get("note")}

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={
        "error": "Internal pipeline error",
        "detail": str(exc),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
