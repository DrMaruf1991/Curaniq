from __future__ import annotations
"""
CURANIQ - Audit Storage Backend (L9-1)
Pluggable, immutable, append-only audit storage.

Architecture:
  - Append-only: entries are NEVER modified or deleted (21 CFR Part 11)
  - Hash chain: each entry includes previous entry's hash
  - Pluggable: swap backend without changing ledger logic
  - Default: JSONL file (one JSON per line, append-only)
  - Production: swap to PostgreSQL/S3 backend

Storage path from env: CURANIQ_AUDIT_PATH (default: ./curaniq_audit.jsonl)
"""
import logging

import json
import os
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import UUID

logger = logging.getLogger(__name__)



class AuditStorageBackend(ABC):
    """Abstract interface for audit storage. All backends implement this."""

    @abstractmethod
    def append(self, entry_dict: dict) -> None:
        """Append an entry. Must be atomic and durable."""

    @abstractmethod
    def get_by_query_id(self, query_id: str) -> list[dict]:
        """Retrieve all entries for a given query_id."""

    @abstractmethod
    def get_all(self) -> list[dict]:
        """Retrieve all entries in order."""

    @abstractmethod
    def count(self) -> int:
        """Total number of stored entries."""

    @abstractmethod
    def get_last_hash(self) -> Optional[str]:
        """Get the hash of the most recent entry."""


class JSONLFileBackend(AuditStorageBackend):
    """
    Append-only JSONL file storage.
    One JSON object per line. Never modifies existing lines.
    Thread-safe via lock. Survives restarts.
    
    JSONL format chosen because:
    - Append-only by design (just add a line)
    - Human-readable for auditing
    - Streamable (don't need to load entire file)
    - Standard format (tools like jq, pandas support it)
    """

    def __init__(self, path: Optional[str] = None):
        self._path = path or os.environ.get(
            "CURANIQ_AUDIT_PATH", "./curaniq_audit.jsonl"
        )
        self._lock = threading.Lock()
        self._cache: list[dict] = []
        self._last_hash: Optional[str] = None

        # Load existing entries on startup
        self._load_existing()

    def _load_existing(self) -> None:
        """Load existing entries from file into memory cache."""
        if not os.path.exists(self._path):
            return

        with open(self._path, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    self._cache.append(entry)
                    self._last_hash = entry.get("entry_hash")
                except json.JSONDecodeError:
                    # Corrupted line — log but don't crash
                    pass

    def append(self, entry_dict: dict) -> None:
        """Append entry to file and cache. Atomic write."""
        with self._lock:
            line = json.dumps(entry_dict, ensure_ascii=False, default=str)

            # Ensure parent directory exists
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)

            # Append to file (atomic per line on most OS)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())  # Force to disk

            self._cache.append(entry_dict)
            self._last_hash = entry_dict.get("entry_hash")

    def get_by_query_id(self, query_id: str) -> list[dict]:
        """Retrieve entries for a specific query."""
        return [e for e in self._cache if e.get("query_id") == query_id]

    def get_all(self) -> list[dict]:
        """Return all entries in order."""
        return list(self._cache)

    def count(self) -> int:
        return len(self._cache)

    def get_last_hash(self) -> Optional[str]:
        return self._last_hash


class InMemoryBackend(AuditStorageBackend):
    """In-memory backend for testing. No persistence."""

    def __init__(self):
        self._entries: list[dict] = []

    def append(self, entry_dict: dict) -> None:
        self._entries.append(entry_dict)

    def get_by_query_id(self, query_id: str) -> list[dict]:
        return [e for e in self._entries if e.get("query_id") == query_id]

    def get_all(self) -> list[dict]:
        return list(self._entries)

    def count(self) -> int:
        return len(self._entries)

    def get_last_hash(self) -> Optional[str]:
        if self._entries:
            return self._entries[-1].get("entry_hash")
        return None


class PostgresBackend(AuditStorageBackend):
    """FIX-31: production audit ledger backed by curaniq.db.AuditRepository.

    Hash-chain semantics maintained inside the DB layer (per-tenant chain head).
    This adapter forwards the existing JSONL-style entry_dict into the new
    `audit_events` table, preserving the existing append-only contract.
    """

    def __init__(self):
        # Lazy-import so audit module doesn't pull DB stack when in JSONL mode.
        from curaniq.db import init_db
        init_db()  # Idempotent — safe to call repeatedly.

    def append(self, entry_dict: dict) -> None:
        from curaniq.db import get_session, AuditRepository, TenantRepository
        import uuid as _uuid
        with get_session() as s:
            TenantRepository(s).ensure_default()
            qid_raw = entry_dict.get("query_id")
            try:
                qid = _uuid.UUID(qid_raw) if qid_raw else None
            except Exception:
                qid = None
            AuditRepository(s).append(
                event_type=entry_dict.get("event_type", "query_answered"),
                payload=entry_dict,
                query_id=qid,
                pipeline_version=entry_dict.get("pipeline_version"),
            )

    def get_by_query_id(self, query_id: str) -> list[dict]:
        from curaniq.db import get_session, AuditRepository
        import uuid as _uuid
        try:
            qid = _uuid.UUID(query_id)
        except Exception:
            return []
        with get_session() as s:
            rows = AuditRepository(s).list_for_query(qid)
            return [
                {"sequence": r.sequence, "event_type": r.event_type,
                 "payload_json": r.payload_json, "chain_hash": r.chain_hash,
                 "created_at": r.created_at.isoformat()}
                for r in rows
            ]

    def get_all(self) -> list[dict]:
        from curaniq.db import get_session, AuditRepository
        with get_session() as s:
            return AuditRepository(s).export()

    def count(self) -> int:
        from curaniq.db import get_session, AuditRepository
        with get_session() as s:
            return AuditRepository(s).count()

    def get_last_hash(self) -> Optional[str]:
        from curaniq.db import get_session
        from curaniq.db.models import AuditChainHead, DEFAULT_TENANT_ID
        with get_session() as s:
            head = s.get(AuditChainHead, DEFAULT_TENANT_ID)
            return head.head_chain_hash if head else None

    def verify_chain(self) -> tuple[bool, Optional[str]]:
        """Cryptographic chain verification — DB-backed only."""
        from curaniq.db import get_session, AuditRepository
        with get_session() as s:
            return AuditRepository(s).verify_chain()


def get_storage_backend() -> AuditStorageBackend:
    """
    Factory: return the appropriate storage backend.
    Reads CURANIQ_AUDIT_BACKEND env var.
    
    Values:
      'jsonl' (default) — append-only JSONL file
      'memory' — in-memory only (for tests)
      'postgresql' — (future) PostgreSQL via SQLAlchemy
    """
    backend_type = os.environ.get("CURANIQ_AUDIT_BACKEND", "jsonl").lower()

    from curaniq.truth_core.config import is_clinician_prod

    if backend_type == "memory":
        if is_clinician_prod():
            raise RuntimeError(
                "clinician_prod forbids in-memory audit storage; set CURANIQ_AUDIT_BACKEND=postgresql."
            )
        return InMemoryBackend()
    elif backend_type == "jsonl":
        if is_clinician_prod():
            raise RuntimeError(
                "clinician_prod forbids JSONL audit storage; set CURANIQ_AUDIT_BACKEND=postgresql."
            )
        return JSONLFileBackend()
    elif backend_type == "postgresql":
        # Real Postgres audit backend wired to curaniq.db.AuditRepository.
        # In clinician_prod this must fail closed. Silent fallback would make the
        # clinical audit trail incomplete and is therefore forbidden.
        try:
            return PostgresBackend()
        except Exception as e:
            from curaniq.truth_core.config import is_clinician_prod
            logger.error("L9-1: PostgresBackend unavailable (%s).", e)
            if is_clinician_prod():
                raise RuntimeError(
                    "clinician_prod requires working PostgreSQL audit backend; "
                    "set CURANIQ_DATABASE_URL, run migrations, and set "
                    "CURANIQ_AUDIT_BACKEND=postgresql."
                ) from e
            logger.warning("Non-production mode: falling back to JSONL audit backend.")
            return JSONLFileBackend()
    else:
        from curaniq.truth_core.config import is_clinician_prod
        if is_clinician_prod():
            raise RuntimeError(
                "clinician_prod requires CURANIQ_AUDIT_BACKEND=postgresql; "
                f"got {backend_type!r}."
            )
        return JSONLFileBackend()
