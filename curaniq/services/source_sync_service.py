"""Source sync/version service for the CURANIQ evidence backbone.

This is the production-facing coordinator for source synchronization. It does
not pretend to fully ingest licensed guideline bodies without credentials. It
provides the durable DB contract: run state, success/failure, version/hash hooks,
and fail-closed monitoring so clinical mode can prove sources are current enough.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from curaniq.db import get_session, SourceRepository, EvidenceRepository
from curaniq.db.models import SyncOutcomeEnum, SourceStatusEnum
from curaniq.truth_core.config import is_clinician_prod
from curaniq.models.schemas import EvidenceObject, EvidenceSourceType, EvidenceTier, Jurisdiction


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class SourceSyncResult:
    source_type: str
    outcome: str
    items_fetched: int = 0
    items_new: int = 0
    items_updated: int = 0
    items_superseded: int = 0
    error: str | None = None


class SourceSyncService:
    """DB-backed synchronization coordinator.

    Connector objects may expose one of:
      - fetch_evidence() -> list[EvidenceObject | dict]
      - sync() -> list[EvidenceObject | dict]

    Missing connector credentials should produce a failed sync run, not silent
    freshness success.
    """

    def __init__(self, connectors: dict[str, object] | None = None) -> None:
        self.connectors = connectors or {}

    def run_registered_sources(self, source_types: Iterable[str] | None = None) -> list[SourceSyncResult]:
        results: list[SourceSyncResult] = []
        with get_session() as s:
            repo = SourceRepository(s)
            sources = repo.list_all()
            if source_types:
                wanted = set(source_types)
                sources = [src for src in sources if src.source_type in wanted]
        for src in sources:
            if (src.status or "").lower() != SourceStatusEnum.ACTIVE.value:
                continue
            results.append(self.sync_one(src.source_type))
        return results

    def sync_one(self, source_type: str) -> SourceSyncResult:
        connector = self.connectors.get(source_type)
        if connector is None:
            return self._record_failure(source_type, "No connector configured for source")
        try:
            if hasattr(connector, "fetch_evidence"):
                raw_items = connector.fetch_evidence()
            elif hasattr(connector, "sync"):
                raw_items = connector.sync()
            else:
                return self._record_failure(source_type, "Connector has no fetch_evidence() or sync()")
            items = [self._coerce_item(source_type, item) for item in (raw_items or [])]
            with get_session() as s:
                srepo = SourceRepository(s)
                erepo = EvidenceRepository(s)
                src = srepo.by_type(source_type)
                if src is None:
                    src = srepo.upsert(source_type=source_type, display_name=source_type)
                new_count = 0
                updated_count = 0
                for ev in items:
                    db_ev, created = erepo.upsert_evidence(
                        source_id=src.id,
                        external_id=ev.source_id,
                        title=ev.title,
                        snippet=ev.snippet,
                        url=ev.url,
                        authors=ev.authors,
                        tier=ev.tier.value,
                        grade=ev.grade.value if ev.grade else None,
                        jurisdiction=ev.jurisdiction.value,
                        published_date=ev.published_date,
                        source_last_updated_at=ev.source_last_updated_at,
                        source_version=ev.source_version,
                        license_status=ev.license_status or "unknown",
                        staleness_ttl_hours=ev.staleness_ttl_hours,
                    )
                    if created:
                        new_count += 1
                    else:
                        updated_count += 1
                srepo.mark_synced(
                    src.id,
                    SyncOutcomeEnum.SUCCESS,
                    items_fetched=len(items),
                    items_new=new_count,
                    items_updated=updated_count,
                )
            return SourceSyncResult(source_type, SyncOutcomeEnum.SUCCESS.value, len(items), new_count, updated_count)
        except Exception as exc:
            return self._record_failure(source_type, f"{type(exc).__name__}: {exc}")

    def _record_failure(self, source_type: str, error: str) -> SourceSyncResult:
        """Record connector/source sync failure.

        In clinician production, inability to persist the failed sync is itself a
        readiness/safety failure. Silently swallowing the DB error would let the
        system pretend it has freshness observability when it does not.
        """
        try:
            with get_session() as s:
                repo = SourceRepository(s)
                src = repo.by_type(source_type) or repo.upsert(source_type=source_type, display_name=source_type)
                repo.mark_synced(src.id, SyncOutcomeEnum.FAILED, error_class="SourceSyncError", error_message=error)
        except Exception as exc:
            if is_clinician_prod():
                raise RuntimeError(
                    f"clinician_prod could not persist source sync failure for {source_type}: {exc}"
                ) from exc
        return SourceSyncResult(source_type, SyncOutcomeEnum.FAILED.value, error=error)

    def _coerce_item(self, source_type: str, item: EvidenceObject | dict) -> EvidenceObject:
        if isinstance(item, EvidenceObject):
            return item
        now = datetime.now(timezone.utc)
        try:
            stype = EvidenceSourceType(source_type)
        except Exception:
            stype = EvidenceSourceType.PUBMED
        return EvidenceObject(
            source_type=stype,
            source_id=str(item.get("source_id") or item.get("external_id") or sha256_text(str(item))[:16]),
            title=item.get("title", "Untitled evidence"),
            snippet=item.get("snippet", ""),
            url=item.get("url"),
            authors=item.get("authors", []),
            published_date=item.get("published_date"),
            source_last_updated_at=item.get("source_last_updated_at"),
            source_version=item.get("source_version"),
            retrieved_at=now,
            last_verified_at=now,
            tier=item.get("tier", EvidenceTier.UNKNOWN),
            jurisdiction=item.get("jurisdiction", Jurisdiction.INT),
            staleness_ttl_hours=item.get("staleness_ttl_hours", 24),
        )
