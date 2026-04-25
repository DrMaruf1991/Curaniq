"""Freshness and source-class enforcement before generation."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from curaniq.models.schemas import ClaimType, EvidencePack, EvidenceObject, EvidenceTier
from curaniq.truth_core.config import TruthCorePolicy
from curaniq.truth_core.source_registry import SourceRegistry
from curaniq.truth_core.claim_requirements import CLAIM_REQUIREMENTS, ClaimRequirement


@dataclass
class EvidenceValidationResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    source_count: int = 0
    claim_type: ClaimType = ClaimType.GENERAL


class FreshnessEnforcementService:
    def __init__(self, registry: SourceRegistry | None = None, policy: TruthCorePolicy | None = None) -> None:
        self.registry = registry or SourceRegistry()
        self.policy = policy or TruthCorePolicy.from_environment()

    def validate_pack_for_claim(self, pack: EvidencePack, claim_type: ClaimType) -> EvidenceValidationResult:
        req = CLAIM_REQUIREMENTS.get(claim_type, CLAIM_REQUIREMENTS[ClaimType.UNKNOWN])
        reasons: list[str] = []
        usable: list[EvidenceObject] = []
        now = datetime.now(timezone.utc)

        if not pack.objects:
            return EvidenceValidationResult(False, ["No evidence objects were retrieved; clinician mode must refuse."], 0, claim_type)

        for ev in pack.objects:
            if not self.registry.is_approved(ev.source_type):
                reasons.append(f"Unapproved source type: {ev.source_type.value} ({ev.source_id}).")
                continue
            if ev.is_retracted:
                reasons.append(f"Retracted source blocked: {ev.source_id}.")
                continue
            if ev.superseded_by or ev.guideline_status in {"superseded", "withdrawn"}:
                reasons.append(f"Superseded/withdrawn source blocked: {ev.source_id}.")
                continue
            if req.allowed_tiers and ev.tier not in req.allowed_tiers:
                reasons.append(f"Tier {ev.tier.value} is not sufficient for {claim_type.value}: {ev.source_id}.")
                continue
            if req.required_source_types_any and ev.source_type not in req.required_source_types_any:
                reasons.append(f"Source {ev.source_type.value} is not sufficient for {claim_type.value}: {ev.source_id}.")
                continue
            ttl = ev.staleness_ttl_hours or self.registry.ttl_hours_for(ev.source_type)
            age_hours = (now - ev.last_verified_at).total_seconds() / 3600
            if age_hours > ttl and (req.fail_closed or not self.policy.allow_stale_high_risk_evidence):
                reasons.append(f"Stale evidence blocked: {ev.source_id}, {age_hours:.1f}h old, TTL {ttl}h.")
                continue
            usable.append(ev)

        if len({ev.source_id for ev in usable}) < req.min_sources:
            reasons.append(f"Insufficient source count for {claim_type.value}: required {req.min_sources}, usable {len({ev.source_id for ev in usable})}.")
            return EvidenceValidationResult(False, reasons, len({ev.source_id for ev in usable}), claim_type)

        return EvidenceValidationResult(True, reasons, len({ev.source_id for ev in usable}), claim_type)

    def fail_closed_message(self, result: EvidenceValidationResult) -> str:
        return "Clinician production safety refusal: current governed evidence is insufficient. " + " | ".join(result.reasons[:6])
