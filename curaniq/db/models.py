"""
SQLAlchemy ORM models for CURANIQ production database.

Tables:
    tenants                   — Hospitals / institutions (multi-tenant isolation)
    users                     — Clinicians / admins (linked to tenant)
    sources                   — Approved evidence sources (NCBI, NICE, WHO, etc.)
    source_versions           — Version history per source (license renewals, content updates)
    source_sync_runs          — Each scheduled sync attempt, success/failure
    evidence_objects          — Retrieved/curated evidence (immutable per version)
    evidence_versions         — Version history per evidence object
    audit_events              — Append-only audit log with hash-chain
    audit_chain_heads         — Per-tenant chain head pointer for tamper detection

All timestamps in UTC. All IDs UUIDs. All evidence rows hash-locked.
Designed for PostgreSQL; SQLite-compatible for tests via Generic types.
"""
from __future__ import annotations
import enum
import uuid
from datetime import datetime, timezone
from typing import Optional, List

from sqlalchemy import (
    Column, String, Integer, Float, Boolean, DateTime,
    ForeignKey, Text, Index, UniqueConstraint, CheckConstraint,
    LargeBinary, Enum as SAEnum,
)
from sqlalchemy.orm import declarative_base, relationship, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator, CHAR

Base = declarative_base()


# ─────────────────────────────────────────────────────────────────────────────
# Cross-DB UUID type (Postgres has native UUID, SQLite needs CHAR(36))
# ─────────────────────────────────────────────────────────────────────────────

class UUIDType(TypeDecorator):
    """UUID stored as native UUID on Postgres, as CHAR(36) on SQLite."""
    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import UUID
            return dialect.type_descriptor(UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value if dialect.name == "postgresql" else str(value)
        # accept str
        if dialect.name == "postgresql":
            return uuid.UUID(value)
        return str(uuid.UUID(value))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, uuid.UUID):
            return value
        return uuid.UUID(value)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# Enum types (str-backed, portable)
# ─────────────────────────────────────────────────────────────────────────────

class SourceStatusEnum(str, enum.Enum):
    ACTIVE = "active"
    DEGRADED = "degraded"
    SUSPENDED = "suspended"
    LICENSE_EXPIRED = "license_expired"
    DECOMMISSIONED = "decommissioned"


class SyncOutcomeEnum(str, enum.Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


class AuditEventTypeEnum(str, enum.Enum):
    QUERY_RECEIVED = "query_received"
    QUERY_REFUSED = "query_refused"
    QUERY_ANSWERED = "query_answered"
    EVIDENCE_RETRIEVED = "evidence_retrieved"
    CLAIM_VERIFIED = "claim_verified"
    CLAIM_SUPPRESSED = "claim_suppressed"
    SAFETY_BLOCK = "safety_block"
    SOURCE_SYNCED = "source_synced"
    SOURCE_FAILED = "source_failed"
    USER_AUTH = "user_auth"
    ADMIN_ACTION = "admin_action"


class UserRoleEnum(str, enum.Enum):
    PATIENT = "patient"
    CLINICIAN = "clinician"
    PHARMACIST = "pharmacist"
    RESEARCHER = "researcher"
    SAFETY_OFFICER = "safety_officer"
    ADMIN = "admin"


# ─────────────────────────────────────────────────────────────────────────────
# Tenants & Users
# ─────────────────────────────────────────────────────────────────────────────

class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID]    = mapped_column(UUIDType(), primary_key=True, default=uuid.uuid4)
    name: Mapped[str]        = mapped_column(String(200), nullable=False, unique=True)
    slug: Mapped[str]        = mapped_column(String(100), nullable=False, unique=True)
    jurisdiction: Mapped[str] = mapped_column(String(10), nullable=False, default="INT")
    is_active: Mapped[bool]  = mapped_column(Boolean, nullable=False, default=True)
    runtime_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="demo")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    users: Mapped[List["User"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        Index("ix_users_tenant_email", "tenant_id", "email", unique=True),
    )

    id: Mapped[uuid.UUID]      = mapped_column(UUIDType(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUIDType(), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    email: Mapped[str]         = mapped_column(String(255), nullable=False)
    full_name: Mapped[Optional[str]] = mapped_column(String(255))
    role: Mapped[str]          = mapped_column(String(40), nullable=False, default=UserRoleEnum.CLINICIAN.value)
    license_number: Mapped[Optional[str]] = mapped_column(String(100))
    license_jurisdiction: Mapped[Optional[str]] = mapped_column(String(10))
    license_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool]    = mapped_column(Boolean, nullable=False, default=True)
    password_hash: Mapped[Optional[str]] = mapped_column(String(255))
    mfa_enabled: Mapped[bool]  = mapped_column(Boolean, nullable=False, default=False)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    tenant: Mapped["Tenant"] = relationship(back_populates="users")


# ─────────────────────────────────────────────────────────────────────────────
# Sources & versioning
# ─────────────────────────────────────────────────────────────────────────────

class Source(Base):
    """An approved evidence source (NCBI PubMed, FDA DailyMed, NICE, WHO, ...)."""
    __tablename__ = "sources"
    __table_args__ = (
        Index("ix_sources_status", "status"),
        CheckConstraint("ttl_seconds > 0", name="ck_source_ttl_positive"),
    )

    id: Mapped[uuid.UUID]      = mapped_column(UUIDType(), primary_key=True, default=uuid.uuid4)
    source_type: Mapped[str]   = mapped_column(String(50), nullable=False, unique=True)
    display_name: Mapped[str]  = mapped_column(String(200), nullable=False)
    authority_level: Mapped[int] = mapped_column(Integer, nullable=False, default=5)  # 1=highest
    jurisdictions: Mapped[str] = mapped_column(String(200), nullable=False, default="INT")  # CSV
    base_url: Mapped[Optional[str]] = mapped_column(String(500))
    license_status: Mapped[str] = mapped_column(String(40), nullable=False, default="open")
    license_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ttl_seconds: Mapped[int]   = mapped_column(Integer, nullable=False, default=86400)
    fail_closed_high_risk: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allowed_claim_types: Mapped[str] = mapped_column(String(500), nullable=False, default="")  # CSV
    status: Mapped[str]        = mapped_column(String(40), nullable=False, default=SourceStatusEnum.ACTIVE.value)
    last_successful_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_attempted_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    versions: Mapped[List["SourceVersion"]] = relationship(back_populates="source", cascade="all, delete-orphan")
    sync_runs: Mapped[List["SourceSyncRun"]] = relationship(back_populates="source", cascade="all, delete-orphan")


class SourceVersion(Base):
    """Version history per source — license changes, schema updates, etc."""
    __tablename__ = "source_versions"
    __table_args__ = (
        Index("ix_source_versions_source_active", "source_id", "is_current"),
        UniqueConstraint("source_id", "version", name="uq_source_version"),
    )

    id: Mapped[uuid.UUID]      = mapped_column(UUIDType(), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(UUIDType(), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[str]       = mapped_column(String(50), nullable=False)
    is_current: Mapped[bool]   = mapped_column(Boolean, nullable=False, default=True)
    license_terms_hash: Mapped[Optional[str]] = mapped_column(String(64))
    config_json: Mapped[Optional[str]] = mapped_column(Text)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    deactivated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    source: Mapped["Source"]   = relationship(back_populates="versions")


class SourceSyncRun(Base):
    """Each scheduled sync attempt — success/failure for monitoring."""
    __tablename__ = "source_sync_runs"
    __table_args__ = (
        Index("ix_sync_runs_source_started", "source_id", "started_at"),
        Index("ix_sync_runs_outcome", "outcome"),
    )

    id: Mapped[uuid.UUID]      = mapped_column(UUIDType(), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(UUIDType(), ForeignKey("sources.id", ondelete="CASCADE"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str]       = mapped_column(String(20), nullable=False, default=SyncOutcomeEnum.SUCCESS.value)
    items_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_new: Mapped[int]     = mapped_column(Integer, nullable=False, default=0)
    items_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_superseded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_class: Mapped[Optional[str]] = mapped_column(String(100))
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    source: Mapped["Source"]   = relationship(back_populates="sync_runs")


# ─────────────────────────────────────────────────────────────────────────────
# Evidence objects + versioning
# ─────────────────────────────────────────────────────────────────────────────

class EvidenceObjectDB(Base):
    """Curated evidence snippets retrieved from approved sources.

    `_DB` suffix avoids name collision with curaniq.models.evidence.EvidenceObject
    (the in-memory Pydantic model). Repository layer converts between them.
    """
    __tablename__ = "evidence_objects"
    __table_args__ = (
        Index("ix_evidence_source_external", "source_id", "external_id"),
        Index("ix_evidence_jurisdiction", "jurisdiction"),
        Index("ix_evidence_published", "published_date"),
        Index("ix_evidence_retracted", "is_retracted"),
        UniqueConstraint("source_id", "external_id", "version", name="uq_evidence_source_extid_ver"),
    )

    id: Mapped[uuid.UUID]      = mapped_column(UUIDType(), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(UUIDType(), ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False)
    external_id: Mapped[str]   = mapped_column(String(200), nullable=False)
    title: Mapped[Optional[str]] = mapped_column(Text)
    snippet: Mapped[str]       = mapped_column(Text, nullable=False)
    snippet_hash: Mapped[str]  = mapped_column(String(64), nullable=False)  # SHA-256
    snippet_byte_offset: Mapped[Optional[int]] = mapped_column(Integer)
    url: Mapped[Optional[str]] = mapped_column(String(1000))
    doi: Mapped[Optional[str]] = mapped_column(String(200))
    authors: Mapped[Optional[str]] = mapped_column(Text)  # CSV
    tier: Mapped[str]          = mapped_column(String(40), nullable=False)
    grade: Mapped[Optional[str]] = mapped_column(String(10))
    jurisdiction: Mapped[str]  = mapped_column(String(10), nullable=False, default="INT")
    published_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    source_last_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    source_version: Mapped[Optional[str]] = mapped_column(String(50))
    license_status: Mapped[str] = mapped_column(String(40), nullable=False, default="open")
    is_retracted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    retraction_notice: Mapped[Optional[str]] = mapped_column(Text)
    is_superseded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    superseded_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUIDType(), ForeignKey("evidence_objects.id"))
    is_current: Mapped[bool]   = mapped_column(Boolean, nullable=False, default=True)
    version: Mapped[int]       = mapped_column(Integer, nullable=False, default=1)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    last_verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    staleness_ttl_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    source: Mapped["Source"]   = relationship(foreign_keys=[source_id])
    evidence_versions: Mapped[List["EvidenceVersion"]] = relationship(
        back_populates="evidence_object",
        cascade="all, delete-orphan",
        foreign_keys="EvidenceVersion.evidence_object_id",
    )


class EvidenceVersion(Base):
    """Append-only version history for evidence objects.

    On any change (retraction, supersession, content update), the prior row's
    is_current goes False and a new EvidenceObjectDB row is inserted; the diff
    is recorded here. Enables 'what was the system citing on Date X' replay.
    """
    __tablename__ = "evidence_versions"
    __table_args__ = (
        Index("ix_ev_versions_obj_recorded", "evidence_object_id", "recorded_at"),
    )

    id: Mapped[uuid.UUID]      = mapped_column(UUIDType(), primary_key=True, default=uuid.uuid4)
    evidence_object_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(), ForeignKey("evidence_objects.id", ondelete="CASCADE"), nullable=False,
    )
    version: Mapped[int]       = mapped_column(Integer, nullable=False)
    change_type: Mapped[str]   = mapped_column(String(40), nullable=False)  # retraction, content_update, supersession
    previous_snippet_hash: Mapped[Optional[str]] = mapped_column(String(64))
    new_snippet_hash: Mapped[Optional[str]] = mapped_column(String(64))
    diff_summary: Mapped[Optional[str]] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    evidence_object: Mapped["EvidenceObjectDB"] = relationship(
        back_populates="evidence_versions",
        foreign_keys=[evidence_object_id],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Audit ledger with hash chain
# ─────────────────────────────────────────────────────────────────────────────

class AuditEvent(Base):
    """Append-only audit event with cryptographic hash chain.

    Each row's `chain_hash` = SHA-256(prev_chain_hash || event_payload_hash).
    Tampering with any historical row breaks the chain on verify.
    """
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_tenant_created", "tenant_id", "created_at"),
        Index("ix_audit_query", "query_id"),
        Index("ix_audit_event_type", "event_type"),
        # FIX-32: defense-in-depth — sequences MUST be unique per tenant.
        # Combined with row-level lock on AuditChainHead in repositories.py,
        # this guarantees no two events ever share a (tenant, sequence) tuple
        # even under concurrent writers on Postgres.
        UniqueConstraint("tenant_id", "sequence", name="uq_audit_tenant_sequence"),
    )

    id: Mapped[uuid.UUID]      = mapped_column(UUIDType(), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUIDType(), ForeignKey("tenants.id"))
    user_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUIDType(), ForeignKey("users.id"))
    query_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUIDType())
    event_type: Mapped[str]    = mapped_column(String(40), nullable=False)
    sequence: Mapped[int]      = mapped_column(Integer, nullable=False)
    payload_json: Mapped[str]  = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str]  = mapped_column(String(64), nullable=False)
    prev_chain_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    chain_hash: Mapped[str]    = mapped_column(String(64), nullable=False)
    model_id: Mapped[Optional[str]] = mapped_column(String(100))
    model_version: Mapped[Optional[str]] = mapped_column(String(50))
    pipeline_version: Mapped[Optional[str]] = mapped_column(String(50))
    ip_address: Mapped[Optional[str]] = mapped_column(String(45))
    user_agent: Mapped[Optional[str]] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class AuditChainHead(Base):
    """Fast lookup of current chain head per tenant for next-write hashing.

    Maintained transactionally with audit_events insert. Allows hash-chain
    growth without scanning the full audit_events table on every write.
    """
    __tablename__ = "audit_chain_heads"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType(), ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True,
    )
    head_event_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUIDType())
    head_chain_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="0" * 64)
    head_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


# Singleton tenant ID for installations without multi-tenancy yet.
DEFAULT_TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
