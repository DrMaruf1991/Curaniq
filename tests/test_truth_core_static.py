"""Truth Core regression tests.

These tests are designed to run without external APIs. They verify that the
production safety contract is encoded in the codebase.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from curaniq.models.schemas import (
    ClaimType,
    ClinicalQuery,
    EvidenceObject,
    EvidencePack,
    EvidenceSourceType,
    EvidenceTier,
    Jurisdiction,
)
from curaniq.truth_core.claim_requirements import infer_claim_type_from_query
from curaniq.truth_core.config import TruthCorePolicy, CuraniqEnvironment
from curaniq.truth_core.freshness import FreshnessEnforcementService


def make_evidence(source_type=EvidenceSourceType.PUBMED, tier=EvidenceTier.COHORT, hours_old=1):
    now = datetime.now(timezone.utc)
    return EvidenceObject(
        source_type=source_type,
        source_id="TEST-SOURCE",
        title="Test evidence",
        snippet="Metformin is contraindicated when eGFR is below 30 mL/min/1.73m2.",
        published_date=now - timedelta(days=100),
        source_last_updated_at=now - timedelta(days=10),
        retrieved_at=now,
        last_verified_at=now - timedelta(hours=hours_old),
        tier=tier,
        jurisdiction=Jurisdiction.US,
        staleness_ttl_hours=24,
    )


def test_dosing_query_infers_dosing_claim_type():
    assert infer_claim_type_from_query("What dose of apixaban in renal failure?") == ClaimType.DOSING


def test_pubmed_alone_is_not_sufficient_for_dosing():
    pack = EvidencePack(query_id=uuid4(), objects=[make_evidence()])
    service = FreshnessEnforcementService(
        policy=TruthCorePolicy(CuraniqEnvironment.CLINICIAN_PROD, False, False, False)
    )
    result = service.validate_pack_for_claim(pack, ClaimType.DOSING)
    assert not result.passed
    assert any("not sufficient" in r for r in result.reasons)


def test_openfda_guideline_source_can_support_dosing_when_fresh():
    pack = EvidencePack(query_id=uuid4(), objects=[make_evidence(EvidenceSourceType.OPENFDA, EvidenceTier.GUIDELINE)])
    service = FreshnessEnforcementService(
        policy=TruthCorePolicy(CuraniqEnvironment.CLINICIAN_PROD, False, False, False)
    )
    result = service.validate_pack_for_claim(pack, ClaimType.DOSING)
    assert result.passed


def test_stale_high_risk_evidence_fails_closed():
    pack = EvidencePack(query_id=uuid4(), objects=[make_evidence(EvidenceSourceType.OPENFDA, EvidenceTier.GUIDELINE, hours_old=72)])
    service = FreshnessEnforcementService(
        policy=TruthCorePolicy(CuraniqEnvironment.CLINICIAN_PROD, False, False, False)
    )
    result = service.validate_pack_for_claim(pack, ClaimType.DOSING)
    assert not result.passed
    assert any("Stale evidence" in r for r in result.reasons)


def test_seed_and_mock_are_disabled_in_clinician_prod(monkeypatch):
    monkeypatch.setenv("CURANIQ_ENV", "clinician_prod")
    from curaniq.truth_core.config import allow_seed_evidence, allow_mock_llm
    assert not allow_seed_evidence()
    assert not allow_mock_llm()
