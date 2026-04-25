"""Production safety configuration for CURANIQ Truth Core.

The central rule: clinician production mode must fail closed. Demo conveniences
(seed evidence, mock LLM answers, stale high-risk evidence) are blocked.
"""
from __future__ import annotations

import os
from enum import Enum
from dataclasses import dataclass


class CuraniqEnvironment(str, Enum):
    DEMO = "demo"
    RESEARCH = "research"
    CLINICIAN_PROD = "clinician_prod"


TRUE_VALUES = {"1", "true", "yes", "on"}


def get_environment() -> CuraniqEnvironment:
    raw = os.getenv("CURANIQ_ENV", CuraniqEnvironment.DEMO.value).strip().lower()
    try:
        return CuraniqEnvironment(raw)
    except ValueError:
        # Unknown environment must be safe, not permissive.
        return CuraniqEnvironment.CLINICIAN_PROD


def is_clinician_prod() -> bool:
    return get_environment() == CuraniqEnvironment.CLINICIAN_PROD


def allow_seed_evidence() -> bool:
    if is_clinician_prod():
        return False
    return os.getenv("CURANIQ_ALLOW_SEED_EVIDENCE", "true").strip().lower() in TRUE_VALUES


def allow_mock_llm() -> bool:
    if is_clinician_prod():
        return False
    return os.getenv("CURANIQ_ALLOW_MOCK_LLM", "true").strip().lower() in TRUE_VALUES


def allow_stale_high_risk_evidence() -> bool:
    if is_clinician_prod():
        return False
    return os.getenv("CURANIQ_ALLOW_STALE_HIGH_RISK", "false").strip().lower() in TRUE_VALUES


@dataclass(frozen=True)
class TruthCorePolicy:
    environment: CuraniqEnvironment
    allow_seed_evidence: bool
    allow_mock_llm: bool
    allow_stale_high_risk_evidence: bool

    @classmethod
    def from_environment(cls) -> "TruthCorePolicy":
        return cls(
            environment=get_environment(),
            allow_seed_evidence=allow_seed_evidence(),
            allow_mock_llm=allow_mock_llm(),
            allow_stale_high_risk_evidence=allow_stale_high_risk_evidence(),
        )
