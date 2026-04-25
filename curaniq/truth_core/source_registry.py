"""Approved source registry with optional database enforcement.

Doctor-facing clinical answers must use governed sources rather than arbitrary
web pages. In clinician production the registry is DB-backed and fail-closed:
source status, license status, TTL, jurisdiction, and allowed claim types are
loaded from the `sources` table and enforced in memory after boot.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from curaniq.models.schemas import ClaimType, EvidenceSourceType, Jurisdiction
from curaniq.truth_core.config import is_clinician_prod


@dataclass(frozen=True)
class SourcePolicy:
    source_type: EvidenceSourceType
    authority_level: int  # 1 highest, 5 lowest
    jurisdiction: Jurisdiction
    ttl_hours: int
    fail_closed: bool
    allowed_claim_types: set[ClaimType] = field(default_factory=set)
    requires_license: bool = False
    description: str = ""
    license_status: str = "open"
    status: str = "active"
    last_successful_sync_at: datetime | None = None


DEFAULT_SOURCE_POLICIES: dict[EvidenceSourceType, SourcePolicy] = {
    EvidenceSourceType.DAILYMED: SourcePolicy(
        EvidenceSourceType.DAILYMED, 1, Jurisdiction.US, 24, True,
        {ClaimType.DOSING, ClaimType.CONTRAINDICATION, ClaimType.DRUG_INTERACTION, ClaimType.SAFETY_SIGNAL, ClaimType.SAFETY_WARNING, ClaimType.MONITORING},
        False, "Official FDA-submitted drug labels via DailyMed/SPL.",
    ),
    EvidenceSourceType.OPENFDA: SourcePolicy(
        EvidenceSourceType.OPENFDA, 1, Jurisdiction.US, 24, True,
        {ClaimType.DOSING, ClaimType.CONTRAINDICATION, ClaimType.DRUG_INTERACTION, ClaimType.SAFETY_SIGNAL, ClaimType.SAFETY_WARNING, ClaimType.MONITORING},
        False, "FDA/openFDA labels, safety, recalls and device/drug endpoints.",
    ),
    EvidenceSourceType.NICE: SourcePolicy(
        EvidenceSourceType.NICE, 1, Jurisdiction.UK, 24 * 7, True,
        {ClaimType.DIAGNOSTIC, ClaimType.EFFICACY, ClaimType.MONITORING, ClaimType.DOSING, ClaimType.CONTRAINDICATION},
        False, "NICE guideline source.",
    ),
    EvidenceSourceType.WHO: SourcePolicy(
        EvidenceSourceType.WHO, 1, Jurisdiction.WHO, 24 * 7, True,
        {ClaimType.DIAGNOSTIC, ClaimType.EFFICACY, ClaimType.MONITORING, ClaimType.SAFETY_SIGNAL, ClaimType.SAFETY_WARNING},
        False, "WHO global guidance.",
    ),
    EvidenceSourceType.PUBMED: SourcePolicy(
        EvidenceSourceType.PUBMED, 2, Jurisdiction.INT, 24 * 7, False,
        {ClaimType.DIAGNOSTIC, ClaimType.EFFICACY, ClaimType.MONITORING, ClaimType.PROGNOSIS, ClaimType.GENERAL, ClaimType.SAFETY_SIGNAL},
        False, "Biomedical literature. Must not be sole source for high-risk dosing.",
    ),
    EvidenceSourceType.CLINICALTRIALS: SourcePolicy(
        EvidenceSourceType.CLINICALTRIALS, 3, Jurisdiction.INT, 24, False,
        {ClaimType.EFFICACY, ClaimType.SAFETY_SIGNAL, ClaimType.PROGNOSIS},
        False, "Emerging trial status; not a recommendation source by itself.",
    ),
    EvidenceSourceType.LACTMED: SourcePolicy(
        EvidenceSourceType.LACTMED, 1, Jurisdiction.US, 24 * 7, True,
        {ClaimType.CONTRAINDICATION, ClaimType.SAFETY_SIGNAL, ClaimType.SAFETY_WARNING, ClaimType.MONITORING},
        False, "Lactation-specific medication safety.",
    ),
    EvidenceSourceType.UZ_MOH: SourcePolicy(
        EvidenceSourceType.UZ_MOH, 1, Jurisdiction.UZ, 24 * 7, True,
        {ClaimType.DIAGNOSTIC, ClaimType.EFFICACY, ClaimType.MONITORING, ClaimType.DOSING, ClaimType.CONTRAINDICATION},
        False, "Uzbekistan Ministry/local protocols.",
    ),
}


class SourceRegistry:
    """Approved source registry.

    Demo/research: static defaults are allowed; DB can be enabled optionally.
    Clinician production: DB-backed registry is mandatory and failures raise.
    """

    def __init__(self, policies: Iterable[SourcePolicy] | None = None, use_db: bool | None = None) -> None:
        self._policies = dict(DEFAULT_SOURCE_POLICIES)
        if policies:
            for policy in policies:
                self._policies[policy.source_type] = policy
        if use_db is None:
            use_db = (
                is_clinician_prod()
                or os.environ.get("CURANIQ_SOURCE_REGISTRY_DB", "").lower() in ("1", "true", "yes")
            )
        self._use_db = bool(use_db)
        self._db_synced = False
        if self._use_db:
            try:
                self._sync_with_db()
            except Exception:
                if is_clinician_prod():
                    raise
                # Demo/research may continue with static defaults.

    def _sync_with_db(self) -> None:
        """Upsert defaults, then hydrate active DB policies back into memory.

        DB admin changes override code defaults. Disabled/degraded/license-expired
        sources are not considered approved by the runtime registry.
        """
        from curaniq.db import get_session, SourceRepository
        from curaniq.db.models import SourceStatusEnum

        with get_session() as s:
            repo = SourceRepository(s)
            for policy in self._policies.values():
                repo.upsert(
                    source_type=policy.source_type.value,
                    display_name=policy.description or policy.source_type.value,
                    authority_level=policy.authority_level,
                    jurisdictions=[policy.jurisdiction.value],
                    ttl_seconds=policy.ttl_hours * 3600,
                    license_status=policy.license_status,
                    fail_closed_high_risk=policy.fail_closed,
                    allowed_claim_types=[ct.value for ct in policy.allowed_claim_types],
                )
            db_sources = repo.list_all()

        hydrated: dict[EvidenceSourceType, SourcePolicy] = {}
        now = datetime.now(timezone.utc)
        for src in db_sources:
            try:
                source_type = EvidenceSourceType(src.source_type)
            except Exception:
                continue
            status = (src.status or "").lower()
            license_status = (src.license_status or "open").lower()
            license_expired = src.license_expires_at is not None and src.license_expires_at <= now
            if status != SourceStatusEnum.ACTIVE.value:
                continue
            if license_status in {"expired", "license_expired", "revoked"} or license_expired:
                continue
            jurisdictions = [j for j in (src.jurisdictions or "INT").split(",") if j]
            jurisdiction_raw = jurisdictions[0] if jurisdictions else "INT"
            try:
                jurisdiction = Jurisdiction(jurisdiction_raw)
            except Exception:
                jurisdiction = Jurisdiction.INT
            allowed: set[ClaimType] = set()
            for raw in (src.allowed_claim_types or "").split(","):
                if not raw:
                    continue
                try:
                    allowed.add(ClaimType(raw))
                except Exception:
                    continue
            hydrated[source_type] = SourcePolicy(
                source_type=source_type,
                authority_level=int(src.authority_level or 5),
                jurisdiction=jurisdiction,
                ttl_hours=max(1, int((src.ttl_seconds or 86400) / 3600)),
                fail_closed=bool(src.fail_closed_high_risk),
                allowed_claim_types=allowed,
                requires_license=license_status not in {"open", "public"},
                description=src.display_name or source_type.value,
                license_status=license_status,
                status=status,
                last_successful_sync_at=src.last_successful_sync_at,
            )
        if not hydrated and is_clinician_prod():
            raise RuntimeError("clinician_prod source registry has no active approved DB sources.")
        if hydrated:
            self._policies = hydrated
        self._db_synced = True

    def refresh(self) -> None:
        if self._use_db:
            self._sync_with_db()

    def get(self, source_type: EvidenceSourceType) -> SourcePolicy | None:
        return self._policies.get(source_type)

    def is_approved(self, source_type: EvidenceSourceType) -> bool:
        policy = self.get(source_type)
        return bool(policy and policy.status == "active" and policy.license_status not in {"expired", "license_expired", "revoked"})

    def ttl_hours_for(self, source_type: EvidenceSourceType, default: int = 24) -> int:
        policy = self.get(source_type)
        return policy.ttl_hours if policy else default

    def allows_claim(self, source_type: EvidenceSourceType, claim_type: ClaimType) -> bool:
        policy = self.get(source_type)
        if not policy:
            return False
        return not policy.allowed_claim_types or claim_type in policy.allowed_claim_types

    @property
    def is_db_backed(self) -> bool:
        return self._use_db and self._db_synced
