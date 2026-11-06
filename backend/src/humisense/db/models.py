"""Database models.

Every tenant-owned table carries ``organization_id``. PostgreSQL Row-Level Security
policies reference that column, so isolation holds even if an application-level filter
is forgotten. See :mod:`humisense.db.rls` for how tenant context is established.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from humisense.db.base import Base, Timestamped, UUIDPrimaryKey


def _enum_values(enum_class: type[enum.Enum]) -> list[str]:
    """Persist enum *values* rather than member names.

    SQLAlchemy defaults to the member name, which would store ``OWNER`` while the API
    and the migration use ``owner``. Returning the values keeps the database, the
    migration, and the wire format identical.
    """
    return [member.value for member in enum_class]


class Role(str, enum.Enum):
    """Membership role. Ordered from least to most privileged."""

    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"
    OWNER = "owner"


class ConnectionStatus(str, enum.Enum):
    PENDING = "pending"
    """Created but never successfully tested."""

    ACTIVE = "active"
    """Last test or sync succeeded."""

    ERROR = "error"
    """Last test or sync failed."""

    DISABLED = "disabled"
    """Administratively disabled; will not run."""


class SyncStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PARTIAL = "partial"
    """Completed, but some records failed validation or delivery."""


class ReceiptStatus(str, enum.Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    FAILED = "failed"


class ActorType(str, enum.Enum):
    USER = "user"
    SYSTEM = "system"
    WORKER = "worker"


class Organization(UUIDPrimaryKey, Timestamped, Base):
    __tablename__ = "organizations"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class User(UUIDPrimaryKey, Timestamped, Base):
    """Identity keyed by the verified token's issuer and subject.

    Email and display name are informational copies from the identity provider and are
    never used for authorization.
    """

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("issuer", "subject", name="uq_users_issuer_subject"),)

    issuer: Mapped[str] = mapped_column(String(500), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320))
    display_name: Mapped[str | None] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    memberships: Mapped[list[Membership]] = relationship(back_populates="user")


class Membership(UUIDPrimaryKey, Timestamped, Base):
    """Binds a user to an organization with a role. The only source of authorization."""

    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "organization_id", name="uq_memberships_user_org"),
        Index("ix_memberships_organization_id", "organization_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[Role] = mapped_column(
        Enum(
            Role, name="role", native_enum=True, validate_strings=True, values_callable=_enum_values
        ),
        nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    user: Mapped[User] = relationship(back_populates="memberships")
    organization: Mapped[Organization] = relationship()


class Session(UUIDPrimaryKey, Base):
    """Server-side session. The cookie holds a random id; only its hash is stored."""

    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_token_hash", "token_hash", unique=True),
        Index("ix_sessions_user_id", "user_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    auth_method: Mapped[str] = mapped_column(String(20), nullable=False, default="dev")
    client_fingerprint: Mapped[str | None] = mapped_column(String(64))


class Connection(UUIDPrimaryKey, Timestamped, Base):
    """One organization's configured source instance.

    ``config`` holds only non-secret configuration. Credentials live behind
    ``secret_ref`` and are resolved by the secrets provider at execution time.
    """

    __tablename__ = "connections"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_connections_organization_id_name"),
        Index("ix_connections_organization_id", "organization_id"),
        CheckConstraint("char_length(name) >= 1", name="name_not_empty"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    connector_type: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    secret_ref: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[ConnectionStatus] = mapped_column(
        Enum(
            ConnectionStatus,
            name="connection_status",
            native_enum=True,
            values_callable=_enum_values,
        ),
        nullable=False,
        default=ConnectionStatus.PENDING,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    sync_jobs: Mapped[list[SyncJob]] = relationship(
        back_populates="connection", cascade="all, delete-orphan"
    )


class SyncJob(UUIDPrimaryKey, Base):
    """A bounded unit of capture work."""

    __tablename__ = "sync_jobs"
    __table_args__ = (
        Index("ix_sync_jobs_organization_id", "organization_id"),
        Index("ix_sync_jobs_connection_id", "connection_id"),
        Index("ix_sync_jobs_status", "status"),
        CheckConstraint("attempt >= 0", name="attempt_non_negative"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("connections.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[SyncStatus] = mapped_column(
        Enum(SyncStatus, name="sync_status", native_enum=True, values_callable=_enum_values),
        nullable=False,
        default=SyncStatus.QUEUED,
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    cursor_before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    cursor_after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    records_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_persisted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_delivered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_message: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[float | None] = mapped_column(Float)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    connection: Mapped[Connection] = relationship(back_populates="sync_jobs")


class SourceRecord(UUIDPrimaryKey, Base):
    """A captured record in the standard envelope.

    The uniqueness constraint is the idempotency mechanism: re-capturing the same source
    record with the same content is a no-op rather than a duplicate.
    """

    __tablename__ = "source_records"
    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "source_record_id",
            "content_hash",
            name="uq_source_records_identity",
        ),
        Index("ix_source_records_organization_id", "organization_id"),
        Index("ix_source_records_connection_id_captured_at", "connection_id", "captured_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("connections.id", ondelete="CASCADE"), nullable=False
    )
    sync_job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sync_jobs.id", ondelete="SET NULL")
    )
    connector_type: Mapped[str] = mapped_column(String(80), nullable=False)
    source_record_id: Mapped[str] = mapped_column(String(512), nullable=False)
    record_type: Mapped[str] = mapped_column(String(120), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(20), nullable=False)
    source_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # Named ``record_metadata`` rather than ``metadata``: ``metadata`` is reserved on a
    # declarative class, and a column of that name silently resolves to the MetaData
    # object in an ORM insert.
    record_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)


class IngestionReceipt(UUIDPrimaryKey, Base):
    """Layer 2 acceptance record for one source record."""

    __tablename__ = "ingestion_receipts"
    __table_args__ = (
        UniqueConstraint("source_record_id", name="uq_ingestion_receipts_source_record_id"),
        Index("ix_ingestion_receipts_organization_id", "organization_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    source_record_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("source_records.id", ondelete="CASCADE"),
        nullable=False,
    )
    sync_job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("sync_jobs.id", ondelete="SET NULL")
    )
    status: Mapped[ReceiptStatus] = mapped_column(
        Enum(ReceiptStatus, name="receipt_status", native_enum=True, values_callable=_enum_values),
        nullable=False,
        default=ReceiptStatus.PENDING,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(80))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SecretRecord(UUIDPrimaryKey, Timestamped, Base):
    """An authenticated-encrypted credential blob.

    The plaintext is never stored and never returned after write. ``key_version`` allows
    a master-key rotation to proceed without decrypting every row at once.
    """

    __tablename__ = "secrets"
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_secrets_organization_id_name"),
        Index("ix_secrets_organization_id", "organization_id"),
    )

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    algorithm: Mapped[str] = mapped_column(String(40), nullable=False, default="AES-256-GCM")
    key_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    nonce: Mapped[bytes] = mapped_column(LargeBinary(12), nullable=False)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class AuditEvent(UUIDPrimaryKey, Base):
    """Append-only audit trail.

    A database trigger rejects UPDATE and DELETE on this table, so the application role
    cannot rewrite history even if its credentials are used directly.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_organization_id_created_at", "organization_id", "created_at"),
        Index("ix_audit_events_action", "action"),
        Index("ix_audit_events_correlation_id", "correlation_id"),
    )

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="SET NULL")
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    actor_type: Mapped[ActorType] = mapped_column(
        Enum(ActorType, name="actor_type", native_enum=True, values_callable=_enum_values),
        nullable=False,
        default=ActorType.USER,
    )
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(60))
    target_id: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64))
    # ``metadata`` is reserved on a declarative class; see the note on SourceRecord.
    event_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# =====================================================================================
# Layer 2 — Data Ingestion & Normalization Engine
#
# Every table below is tenant-owned (NOT NULL organization_id) and is covered by Row-Level
# Security, applied in the Layer 2 migration. Payloads are JSONB because they are genuinely
# source-specific; identity, timestamps, and state are strongly typed so they can be indexed
# and constrained.
# =====================================================================================


class IngestBatchStatus(str, enum.Enum):
    RECEIVED = "received"
    PROCESSING = "processing"
    COMPLETED = "completed"
    PARTIAL = "partial"
    """Completed, but at least one record failed or was quarantined."""
    FAILED = "failed"


class RecordState(str, enum.Enum):
    """Mirrors ``humisense.layer2.states.ProcessingState`` for persistence."""

    RECEIVED = "received"
    VALIDATING = "validating"
    VALIDATED = "validated"
    NORMALIZING = "normalizing"
    NORMALIZED = "normalized"
    STORED = "stored"
    PUBLISHED = "published"
    RETRY_PENDING = "retry_pending"
    FAILED = "failed"
    QUARANTINED = "quarantined"
    DUPLICATE = "duplicate"


class QuarantineStatus(str, enum.Enum):
    OPEN = "open"
    REPLAYED = "replayed"
    DISCARDED = "discarded"


class OutboxStatus(str, enum.Enum):
    PENDING = "pending"
    PUBLISHED = "published"
    FAILED = "failed"


class IngestBatch(UUIDPrimaryKey, Base):
    """One batch received from Layer 1.

    Carries per-batch counters so an operator can see the fate of a delivery without
    scanning every record. ``batch_id`` is the producer's identifier and is unique per
    tenant, which makes a redelivered batch idempotent at the batch level too.
    """

    __tablename__ = "ingest_batches"
    __table_args__ = (
        UniqueConstraint("organization_id", "batch_id", name="uq_ingest_batches_org_batch"),
        Index("ix_ingest_batches_organization_id", "organization_id"),
        Index("ix_ingest_batches_organization_created", "organization_id", "created_at"),
        CheckConstraint("records_received >= 0", name="records_received_non_negative"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    producer: Mapped[str | None] = mapped_column(String(80))
    """The Layer 1 connector type or service that produced the batch."""
    status: Mapped[IngestBatchStatus] = mapped_column(
        Enum(
            IngestBatchStatus,
            name="ingest_batch_status",
            native_enum=True,
            values_callable=_enum_values,
        ),
        nullable=False,
        default=IngestBatchStatus.RECEIVED,
    )
    records_received: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_accepted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_duplicate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_quarantined: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RawRecord(UUIDPrimaryKey, Base):
    """Immutable preservation of the original source payload.

    This row is the source of truth for replay. A database trigger rejects ``UPDATE``, so
    the payload cannot be rewritten by application code even if a query is wrong. The
    normalized representation lives in a separate table (``canonical_records``), so the two
    are never conflated.

    Uniqueness is the deduplication mechanism and is tenant-scoped: it is impossible to
    deduplicate across tenants because ``organization_id`` is part of the key. ``content_hash``
    alone is never the identity — two legitimate records may share content — so the source
    record id is part of the key too.

    The key uses ``source_record_key`` (the producer's identifier string), not the optional
    ``source_record_id`` foreign key. Over HTTP delivery the producer's ``source_records``
    row may not exist in this database, so the foreign key can be NULL; and PostgreSQL treats
    NULLs as distinct in a unique index, which would defeat deduplication entirely.
    """

    __tablename__ = "raw_records"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "connection_id",
            "source_record_key",
            "content_hash",
            name="uq_raw_records_identity",
        ),
        Index("ix_raw_records_organization_id", "organization_id"),
        Index("ix_raw_records_org_connection", "organization_id", "connection_id"),
        Index("ix_raw_records_org_source_ts", "organization_id", "source_timestamp"),
        Index("ix_raw_records_batch", "batch_row_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("connections.id", ondelete="CASCADE"), nullable=False
    )
    source_record_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("source_records.id", ondelete="SET NULL")
    )
    """The Layer 1 ``source_records`` row this envelope came from, when delivered over HTTP."""
    batch_row_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("ingest_batches.id", ondelete="SET NULL")
    )
    connector_type: Mapped[str] = mapped_column(String(80), nullable=False)
    source_record_key: Mapped[str] = mapped_column(String(512), nullable=False)
    """The producer's ``source_record_id`` string, preserved verbatim."""
    record_type: Mapped[str] = mapped_column(String(120), nullable=False)
    source_schema_version: Mapped[str] = mapped_column(String(20), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    source_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    record_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    untrusted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class CanonicalRecord(UUIDPrimaryKey, Base):
    """The normalized, deduplicated canonical record.

    One canonical record per (tenant, idempotency key) — the same identity as the raw record
    it derives from. State and processing version are mutable; the payload columns are written
    once when the record reaches ``NORMALIZED``.
    """

    __tablename__ = "canonical_records"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "idempotency_key",
            name="uq_canonical_records_identity",
        ),
        Index("ix_canonical_records_organization_id", "organization_id"),
        Index("ix_canonical_records_org_record_type", "organization_id", "record_type"),
        Index("ix_canonical_records_org_source_ts", "organization_id", "source_timestamp"),
        Index("ix_canonical_records_org_state", "organization_id", "processing_state"),
        Index("ix_canonical_records_raw", "raw_record_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    raw_record_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("raw_records.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("connections.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    source_record_key: Mapped[str] = mapped_column(String(512), nullable=False)
    record_type: Mapped[str] = mapped_column(String(120), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(120))
    event_type: Mapped[str | None] = mapped_column(String(120))
    data_family: Mapped[str] = mapped_column(String(20), nullable=False)

    source_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    event_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observed_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    source_timezone: Mapped[str | None] = mapped_column(String(64))
    source_time_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )

    normalized_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    extensions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    record_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    unit_conversions: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    quality_code: Mapped[str | None] = mapped_column(String(40))

    schema_version: Mapped[str] = mapped_column(String(20), nullable=False)
    source_schema_version: Mapped[str] = mapped_column(String(20), nullable=False)
    processing_version: Mapped[str] = mapped_column(String(40), nullable=False)
    normalizer: Mapped[str | None] = mapped_column(String(120))
    normalizer_version: Mapped[str | None] = mapped_column(String(40))

    processing_state: Mapped[RecordState] = mapped_column(
        Enum(RecordState, name="record_state", native_enum=True, values_callable=_enum_values),
        nullable=False,
        default=RecordState.RECEIVED,
    )
    attempt: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    last_error_detail: Mapped[str | None] = mapped_column(Text)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("canonical_records.id", ondelete="SET NULL")
    )
    """Set when a newer content hash supersedes this record under the same source id."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class RecordQuarantine(UUIDPrimaryKey, Base):
    """A record that failed a stage and could not proceed.

    Holds a *bounded* error detail and a small payload preview, never the whole payload, so
    the quarantine table cannot be used as bulk storage or to exfiltrate source data through
    a broad query.
    """

    __tablename__ = "record_quarantine"
    __table_args__ = (
        Index("ix_record_quarantine_organization_id", "organization_id"),
        Index("ix_record_quarantine_org_status", "organization_id", "status"),
        Index("ix_record_quarantine_org_created", "organization_id", "created_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    raw_record_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("raw_records.id", ondelete="CASCADE")
    )
    canonical_record_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("canonical_records.id", ondelete="CASCADE")
    )
    batch_row_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("ingest_batches.id", ondelete="SET NULL")
    )
    connection_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("connections.id", ondelete="CASCADE")
    )
    source_record_key: Mapped[str | None] = mapped_column(String(512))
    stage: Mapped[str] = mapped_column(String(30), nullable=False)
    error_code: Mapped[str] = mapped_column(String(80), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    payload_preview: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    """A truncated preview (bounded byte budget), never the full payload."""
    status: Mapped[QuarantineStatus] = mapped_column(
        Enum(
            QuarantineStatus,
            name="quarantine_status",
            native_enum=True,
            values_callable=_enum_values,
        ),
        nullable=False,
        default=QuarantineStatus.OPEN,
    )
    replay_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class OutboxEvent(UUIDPrimaryKey, Base):
    """Transactional outbox row for downstream publication.

    Inserted in the same transaction that writes the canonical record, so an event exists
    if and only if the record exists. The publisher then moves ``pending -> published`` only
    after the downstream sink confirms, which is what lets Layer 2 avoid claiming a record
    was published before publication was durable.
    """

    __tablename__ = "outbox_events"
    __table_args__ = (
        UniqueConstraint("canonical_record_id", name="uq_outbox_events_canonical_record"),
        Index("ix_outbox_events_organization_id", "organization_id"),
        Index("ix_outbox_events_org_status", "organization_id", "status"),
        Index("ix_outbox_events_status_created", "status", "created_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    canonical_record_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("canonical_records.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[OutboxStatus] = mapped_column(
        Enum(OutboxStatus, name="outbox_status", native_enum=True, values_callable=_enum_values),
        nullable=False,
        default=OutboxStatus.PENDING,
    )
    attempt: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RecordProcessingEvent(UUIDPrimaryKey, Base):
    """Append-only ledger of a record's processing-state transitions.

    The database rejects UPDATE and DELETE, so the history of how a record was processed is
    immutable. This is the record-level counterpart to the audit trail.
    """

    __tablename__ = "record_processing_events"
    __table_args__ = (
        Index("ix_record_processing_events_organization_id", "organization_id"),
        Index("ix_record_processing_events_canonical", "canonical_record_id", "created_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    canonical_record_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("canonical_records.id", ondelete="CASCADE")
    )
    from_state: Mapped[str | None] = mapped_column(String(30))
    to_state: Mapped[str] = mapped_column(String(30), nullable=False)
    stage: Mapped[str] = mapped_column(String(30), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80))
    detail: Mapped[str | None] = mapped_column(Text)
    attempt: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# --- Layer 3: Analytics, Intelligence & Detection ---------------------------------
#
# Every table below is tenant-owned and carries a NOT NULL ``organization_id`` so the same
# Row-Level Security treatment as Layers 1 and 2 applies without exception. The Layer 3
# migration applies ``ENABLE`` + ``FORCE`` RLS to each of these tables.
#
# JSONB is used only where the shape is genuinely variable (rule condition, detection
# evidence, quality issues, counters). Identity, classification, versions, and provenance
# are typed columns, so they can be indexed and constrained.


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


class DetectionStatus(str, enum.Enum):
    """Mirrors ``humisense.layer3.contract.DetectionLifecycle`` for persistence."""

    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    INVESTIGATING = "investigating"
    RESOLVED = "resolved"
    SUPPRESSED = "suppressed"
    RECOVERED = "recovered"


class ModelStatus(str, enum.Enum):
    REGISTERED = "registered"
    EVALUATED = "evaluated"
    ACTIVE = "active"
    INACTIVE = "inactive"
    ROLLED_BACK = "rolled_back"


class AnalyticsJob(UUIDPrimaryKey, Base):
    """One unit of analytical execution, keyed for idempotency.

    ``job_key`` is a deterministic string (job type + entity + metric + window) so a
    re-run of the same window upserts rather than creating a second job, which is what
    makes scheduled and backfill execution idempotent.
    """

    __tablename__ = "analytics_jobs"
    __table_args__ = (
        UniqueConstraint("organization_id", "job_key", name="uq_analytics_jobs_org_key"),
        Index("ix_analytics_jobs_organization_id", "organization_id"),
        Index("ix_analytics_jobs_org_status", "organization_id", "status"),
        Index("ix_analytics_jobs_org_created", "organization_id", "created_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    job_key: Mapped[str] = mapped_column(String(200), nullable=False)
    job_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(200))
    metric: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[JobStatus] = mapped_column(
        Enum(
            JobStatus,
            name="analytics_job_status",
            native_enum=True,
            values_callable=_enum_values,
        ),
        nullable=False,
        default=JobStatus.QUEUED,
    )
    watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    """The newest event time processed, so the next run resumes incrementally."""
    detections: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(80))
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AnalyticsExecutionLog(UUIDPrimaryKey, Base):
    """Append-only per-run stage log. Rejects UPDATE and DELETE at the database."""

    __tablename__ = "analytics_execution_logs"
    __table_args__ = (
        Index("ix_analytics_execution_logs_organization_id", "organization_id"),
        Index("ix_analytics_execution_logs_job", "job_id", "created_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("analytics_jobs.id", ondelete="CASCADE")
    )
    stage: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    counters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DataQualityResultRow(UUIDPrimaryKey, Base):
    """Persisted data-quality assessment for one entity/metric window."""

    __tablename__ = "data_quality_results"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "entity_id",
            "metric",
            "window_end",
            name="uq_data_quality_results_window",
        ),
        Index("ix_data_quality_results_organization_id", "organization_id"),
        Index("ix_data_quality_results_org_entity", "organization_id", "entity_id", "metric"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    entity_id: Mapped[str] = mapped_column(String(200), nullable=False)
    metric: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    evaluated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    missing_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    invalid_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    issues: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DataReadinessResultRow(UUIDPrimaryKey, Base):
    """Persisted readiness evaluation for one method/entity/metric window."""

    __tablename__ = "data_readiness_results"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "entity_id",
            "metric",
            "method",
            "window_end",
            name="uq_data_readiness_results_window",
        ),
        Index("ix_data_readiness_results_organization_id", "organization_id"),
        Index("ix_data_readiness_results_org_entity", "organization_id", "entity_id", "metric"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    entity_id: Mapped[str] = mapped_column(String(200), nullable=False)
    metric: Mapped[str] = mapped_column(String(120), nullable=False)
    method: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    reasons: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    min_required: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FeatureDefinition(UUIDPrimaryKey, Base):
    """A registered, versioned feature definition."""

    __tablename__ = "feature_definitions"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "name", "version", name="uq_feature_definitions_name_version"
        ),
        Index("ix_feature_definitions_organization_id", "organization_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    method: Mapped[str] = mapped_column(String(120), nullable=False)
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unit: Mapped[str | None] = mapped_column(String(40))
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FeatureValueRow(UUIDPrimaryKey, Base):
    """A computed feature observation, idempotent on its natural key."""

    __tablename__ = "feature_values"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "entity_id",
            "metric",
            "name",
            "version",
            "at",
            name="uq_feature_values_observation",
        ),
        Index("ix_feature_values_organization_id", "organization_id"),
        Index("ix_feature_values_org_entity", "organization_id", "entity_id", "metric", "at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    entity_id: Mapped[str] = mapped_column(String(200), nullable=False)
    metric: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str | None] = mapped_column(String(40))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    method: Mapped[str] = mapped_column(String(120), nullable=False)
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quality_status: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    sources: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class BaselineDefinition(UUIDPrimaryKey, Base):
    """A baseline's stable identity and configuration; versions live separately."""

    __tablename__ = "baseline_definitions"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "entity_id",
            "metric",
            "baseline_type",
            name="uq_baseline_definitions_identity",
        ),
        Index("ix_baseline_definitions_organization_id", "organization_id"),
        Index("ix_baseline_definitions_org_entity", "organization_id", "entity_id", "metric"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    entity_id: Mapped[str] = mapped_column(String(200), nullable=False)
    metric: Mapped[str] = mapped_column(String(120), nullable=False)
    baseline_type: Mapped[str] = mapped_column(String(30), nullable=False)
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unit: Mapped[str | None] = mapped_column(String(40))
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    """0 means no version has been built yet."""
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class BaselineVersionRow(UUIDPrimaryKey, Base):
    """An immutable baseline version. Never updated in place; a new row is added."""

    __tablename__ = "baseline_versions"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "baseline_id", "version", name="uq_baseline_versions_version"
        ),
        Index("ix_baseline_versions_organization_id", "organization_id"),
        Index("ix_baseline_versions_baseline", "baseline_id", "version"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    baseline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("baseline_definitions.id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    mean: Mapped[float | None] = mapped_column(Float)
    median: Mapped[float | None] = mapped_column(Float)
    stddev: Mapped[float | None] = mapped_column(Float)
    minimum: Mapped[float | None] = mapped_column(Float)
    maximum: Mapped[float | None] = mapped_column(Float)
    p10: Mapped[float | None] = mapped_column(Float)
    p25: Mapped[float | None] = mapped_column(Float)
    p75: Mapped[float | None] = mapped_column(Float)
    p90: Mapped[float | None] = mapped_column(Float)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    quality_status: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    dataset_ref: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AnalyticsRule(UUIDPrimaryKey, Base):
    """A rule's stable identity; versions live separately."""

    __tablename__ = "analytics_rules"
    __table_args__ = (
        UniqueConstraint("rule_id", name="uq_analytics_rules_rule_id"),
        Index("ix_analytics_rules_organization_id", "organization_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    rule_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    domain: Mapped[str] = mapped_column(String(30), nullable=False, default="generic")
    scope: Mapped[str] = mapped_column(String(40), nullable=False, default="entity")
    entity_id: Mapped[str | None] = mapped_column(String(200))
    metric: Mapped[str] = mapped_column(String(120), nullable=False)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AnalyticsRuleVersionRow(UUIDPrimaryKey, Base):
    """An immutable rule version, including the validated condition."""

    __tablename__ = "analytics_rule_versions"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "rule_id", "version", name="uq_analytics_rule_versions_version"
        ),
        Index("ix_analytics_rule_versions_organization_id", "organization_id"),
        Index("ix_analytics_rule_versions_rule", "rule_id", "version"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    rule_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("analytics_rules.rule_id", ondelete="CASCADE"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    condition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    window_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="medium")
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AnomalyDetectionRow(UUIDPrimaryKey, Base):
    """Raw detector output, before it becomes a detection event."""

    __tablename__ = "anomaly_detections"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "entity_id",
            "metric",
            "algorithm_name",
            "window_end",
            "baseline_version",
            name="uq_anomaly_detections_observation",
        ),
        Index("ix_anomaly_detections_organization_id", "organization_id"),
        Index("ix_anomaly_detections_org_entity", "organization_id", "entity_id", "metric"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    entity_id: Mapped[str] = mapped_column(String(200), nullable=False)
    metric: Mapped[str] = mapped_column(String(120), nullable=False)
    algorithm_name: Mapped[str] = mapped_column(String(80), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(20), nullable=False)
    anomaly_score: Mapped[float | None] = mapped_column(Float)
    observed_value: Mapped[float | None] = mapped_column(Float)
    expected_value: Mapped[float | None] = mapped_column(Float)
    deviation: Mapped[float | None] = mapped_column(Float)
    baseline_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    baseline_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unit: Mapped[str | None] = mapped_column(String(40))
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TrendResultRow(UUIDPrimaryKey, Base):
    """Persisted trend analysis."""

    __tablename__ = "trend_results"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "entity_id", "metric", "window_end", name="uq_trend_results_window"
        ),
        Index("ix_trend_results_organization_id", "organization_id"),
        Index("ix_trend_results_org_entity", "organization_id", "entity_id", "metric"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    entity_id: Mapped[str] = mapped_column(String(200), nullable=False)
    metric: Mapped[str] = mapped_column(String(120), nullable=False)
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    magnitude: Mapped[float | None] = mapped_column(Float)
    method: Mapped[str] = mapped_column(String(80), nullable=False)
    r_squared: Mapped[float | None] = mapped_column(Float)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unit: Mapped[str | None] = mapped_column(String(40))
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ChangePointResultRow(UUIDPrimaryKey, Base):
    """Persisted change-point detection."""

    __tablename__ = "change_point_results"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "entity_id",
            "metric",
            "detected_at",
            name="uq_change_point_results_detected",
        ),
        Index("ix_change_point_results_organization_id", "organization_id"),
        Index("ix_change_point_results_org_entity", "organization_id", "entity_id", "metric"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    entity_id: Mapped[str] = mapped_column(String(200), nullable=False)
    metric: Mapped[str] = mapped_column(String(120), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    method: Mapped[str] = mapped_column(String(80), nullable=False)
    previous_mean: Mapped[float | None] = mapped_column(Float)
    new_mean: Mapped[float | None] = mapped_column(Float)
    magnitude: Mapped[float | None] = mapped_column(Float)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unit: Mapped[str | None] = mapped_column(String(40))
    uncertainty: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ForecastResultRow(UUIDPrimaryKey, Base):
    """Persisted forecast, including its evaluation against a naive baseline."""

    __tablename__ = "forecast_results"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "entity_id",
            "metric",
            "generated_at",
            "horizon_steps",
            name="uq_forecast_results_generation",
        ),
        Index("ix_forecast_results_organization_id", "organization_id"),
        Index("ix_forecast_results_org_entity", "organization_id", "entity_id", "metric"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    entity_id: Mapped[str] = mapped_column(String(200), nullable=False)
    metric: Mapped[str] = mapped_column(String(120), nullable=False)
    model_name: Mapped[str] = mapped_column(String(80), nullable=False)
    model_version: Mapped[str] = mapped_column(String(20), nullable=False)
    horizon_steps: Mapped[int] = mapped_column(Integer, nullable=False)
    predicted: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    lower_bound: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    upper_bound: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    evaluation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unit: Mapped[str | None] = mapped_column(String(40))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    limitations: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CorrelationResultRow(UUIDPrimaryKey, Base):
    """Persisted signal correlation. Always carries a no-causation statement."""

    __tablename__ = "correlation_results"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "entity_a",
            "metric_a",
            "entity_b",
            "metric_b",
            "method",
            "window_end",
            name="uq_correlation_results_observation",
        ),
        Index("ix_correlation_results_organization_id", "organization_id"),
        Index("ix_correlation_results_org_pair", "organization_id", "entity_a", "entity_b"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    entity_a: Mapped[str] = mapped_column(String(200), nullable=False)
    metric_a: Mapped[str] = mapped_column(String(120), nullable=False)
    entity_b: Mapped[str] = mapped_column(String(200), nullable=False)
    metric_b: Mapped[str] = mapped_column(String(120), nullable=False)
    method: Mapped[str] = mapped_column(String(40), nullable=False)
    coefficient: Mapped[float | None] = mapped_column(Float)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lag_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    caveat: Mapped[str] = mapped_column(Text, nullable=False, default="")
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ModelRegistryRow(UUIDPrimaryKey, Base):
    """A registered model version and its deployment state."""

    __tablename__ = "model_registry"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "model_key", "version", name="uq_model_registry_key_version"
        ),
        Index("ix_model_registry_organization_id", "organization_id"),
        Index("ix_model_registry_org_status", "organization_id", "status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    model_key: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    model_type: Mapped[str] = mapped_column(String(80), nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[ModelStatus] = mapped_column(
        Enum(ModelStatus, name="model_status", native_enum=True, values_callable=_enum_values),
        nullable=False,
        default=ModelStatus.REGISTERED,
    )
    dataset_ref: Mapped[str | None] = mapped_column(String(200))
    feature_versions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    training_params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    evaluation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    artifact_ref: Mapped[str | None] = mapped_column(String(200))
    """A reference, never a path to a file the process will deserialize blindly."""
    owner: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    rollback_version: Mapped[str | None] = mapped_column(String(40))
    trained_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ModelEvaluationRow(UUIDPrimaryKey, Base):
    """An evaluation of one model version against a dataset."""

    __tablename__ = "model_evaluations"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "model_id", "dataset_ref", name="uq_model_evaluations_dataset"
        ),
        Index("ix_model_evaluations_organization_id", "organization_id"),
        Index("ix_model_evaluations_model", "model_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    model_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("model_registry.id", ondelete="CASCADE"), nullable=False
    )
    dataset_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    evaluated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DetectionEventRow(UUIDPrimaryKey, Base):
    """The standardized detection event.

    Mutable only in ``status`` and lifecycle fields; the analytical content is written once.
    Large source payloads are never copied here — ``evidence_references`` points at the
    Layer 2 records instead.
    """

    __tablename__ = "detection_events"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "detection_id", name="uq_detection_events_detection_id"
        ),
        Index("ix_detection_events_organization_id", "organization_id"),
        Index("ix_detection_events_org_status", "organization_id", "status"),
        Index("ix_detection_events_org_entity", "organization_id", "entity_id", "metric"),
        Index("ix_detection_events_org_detected", "organization_id", "detected_at"),
        Index("ix_detection_events_org_severity", "organization_id", "severity"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    detection_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    domain: Mapped[str] = mapped_column(String(30), nullable=False)
    source_system: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    connection_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    entity_id: Mapped[str] = mapped_column(String(200), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(120))
    metric: Mapped[str] = mapped_column(String(120), nullable=False)
    detection_category: Mapped[str] = mapped_column(String(40), nullable=False)
    detection_type: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    observed_value: Mapped[float | None] = mapped_column(Float)
    expected_value: Mapped[float | None] = mapped_column(Float)
    deviation: Mapped[float | None] = mapped_column(Float)
    unit: Mapped[str | None] = mapped_column(String(40))
    observation_window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observation_window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    algorithm_name: Mapped[str] = mapped_column(String(80), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(20), nullable=False)
    model_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    model_version: Mapped[str | None] = mapped_column(String(40))
    baseline_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    baseline_version: Mapped[int | None] = mapped_column(Integer)
    feature_versions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    anomaly_score: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[float | None] = mapped_column(Float)
    data_quality_status: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown")
    evidence_references: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    explanation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    limitations: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    schema_version: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[DetectionStatus] = mapped_column(
        Enum(
            DetectionStatus,
            name="detection_status",
            native_enum=True,
            values_callable=_enum_values,
        ),
        nullable=False,
        default=DetectionStatus.OPEN,
    )
    group_key: Mapped[str | None] = mapped_column(String(200))
    """Groups duplicate detections of the same condition across a cooldown window."""
    suppressed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class DetectionEventHistoryRow(UUIDPrimaryKey, Base):
    """Append-only lifecycle history for a detection event."""

    __tablename__ = "detection_event_history"
    __table_args__ = (
        Index("ix_detection_event_history_organization_id", "organization_id"),
        Index("ix_detection_event_history_detection", "detection_id", "created_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    detection_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(30))
    to_status: Mapped[str] = mapped_column(String(30), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    reason: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# --- Layer 4: Work Intelligence Engine --------------------------------------------
#
# Layer 4 turns verified Layer 2 canonical records and Layer 3 detection events into a
# structured representation of organizational work. Every table below is tenant-owned and
# carries a NOT NULL ``organization_id`` so the same Row-Level Security treatment as
# Layers 1-3 applies without exception.
#
# Two design rules are visible in the shape of these tables:
#
# * **Derived values are recomputable.** A derived row is unique on its natural key, so
#   re-deriving the same source record is an upsert rather than a duplicate. The natural
#   keys are the identity mechanism, not application discipline.
# * **History is append-only.** Timeline, status, commitment, and outbox rows reject UPDATE
#   and DELETE by trigger, so a corrected value is a new row and the old one survives.
#
# JSONB is used only where the shape is genuinely variable: workflow step definitions,
# dependency evidence, extraction fields, and conflict candidate lists.


class WorkType(str, enum.Enum):
    """Canonical work categories. Extensible by adding a member, never by a runtime string."""

    TASK = "task"
    FOLLOW_UP = "follow_up"
    APPROVAL = "approval"
    PURCHASE_REQUIREMENT = "purchase_requirement"
    PRODUCTION_REQUIREMENT = "production_requirement"
    MAINTENANCE_ACTIVITY = "maintenance_activity"
    CUSTOMER_COMMITMENT = "customer_commitment"
    SUPPLIER_COMMITMENT = "supplier_commitment"
    DELIVERY_OBLIGATION = "delivery_obligation"
    DOCUMENT_REQUEST = "document_request"
    QUALITY_ISSUE = "quality_issue"
    FINANCIAL_REVIEW = "financial_review"
    OPERATIONAL_ISSUE = "operational_issue"
    PROJECT_ACTIVITY = "project_activity"
    GENERAL_WORK_ITEM = "general_work_item"


class WorkDomain(str, enum.Enum):
    """The functional domain a work item belongs to."""

    PRODUCTION = "production"
    PROCUREMENT = "procurement"
    SALES = "sales"
    MAINTENANCE = "maintenance"
    QUALITY = "quality"
    FINANCE = "finance"
    MANAGEMENT = "management"
    GENERAL = "general"


class WorkStatus(str, enum.Enum):
    """Canonical work status. Source statuses are mapped onto these, never conflated."""

    NEW = "new"
    OPEN = "open"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    WAITING = "waiting"
    BLOCKED = "blocked"
    ON_HOLD = "on_hold"
    UNDER_REVIEW = "under_review"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class WorkOrigin(str, enum.Enum):
    """How a work item came to exist. Never presented as more certain than it is."""

    EXPLICIT = "explicit"
    """Recorded as a task by the source system."""
    EXTRACTED = "extracted"
    """Extracted from a communication or document by an approved upstream extractor."""
    INFERRED = "inferred"
    """Derived from a structured operational record."""
    MANUAL = "manual"
    """Created by an authorized user."""


class PriorityLevel(str, enum.Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"
    CRITICAL = "critical"


class PrioritySource(str, enum.Enum):
    SOURCE = "source"
    """The source system's own priority."""
    USER = "user"
    """An explicit human assignment. Never overwritten by a rule."""
    RULE = "rule"
    """Deterministic operational metadata."""


class DeadlineType(str, enum.Enum):
    ABSOLUTE = "absolute"
    RELATIVE = "relative"
    SOURCE_PROVIDED = "source_provided"
    CONTRACTUAL = "contractual"
    ESTIMATED = "estimated"
    REQUESTED = "requested"
    RECURRING = "recurring"


class AssignmentRole(str, enum.Enum):
    OWNER = "owner"
    ASSIGNEE = "assignee"
    REQUESTER = "requester"
    APPROVER = "approver"
    COLLABORATOR = "collaborator"
    ACCOUNTABLE_MANAGER = "accountable_manager"
    ESCALATION = "escalation"


class ParticipantType(str, enum.Enum):
    USER = "user"
    TEAM = "team"
    DEPARTMENT = "department"
    EXTERNAL = "external"


class VerificationStatus(str, enum.Enum):
    """How much a derived value can be trusted."""

    VERIFIED = "verified"
    SOURCE_REPORTED = "source_reported"
    INFERRED = "inferred"
    UNRESOLVED = "unresolved"


class SourceReferenceRelationship(str, enum.Enum):
    PRIMARY = "primary"
    SUPPORTING = "supporting"
    MENTIONED = "mentioned"


class CommitmentType(str, enum.Enum):
    INTERNAL = "internal"
    CUSTOMER = "customer"
    SUPPLIER = "supplier"
    DEPARTMENTAL = "departmental"
    DELIVERY = "delivery"
    FINANCIAL = "financial"
    OPERATIONAL = "operational"


class CommitmentStatus(str, enum.Enum):
    """A requested or estimated date is deliberately distinct from a confirmed promise."""

    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    REQUESTED = "requested"
    ESTIMATED = "estimated"
    CONTRACTUAL = "contractual"
    INFERRED = "inferred"
    AT_RISK = "at_risk"
    MET = "met"
    MISSED = "missed"
    CANCELLED = "cancelled"


class DependencyRelationship(str, enum.Enum):
    BLOCKS = "blocks"
    BLOCKED_BY = "blocked_by"
    REQUIRES = "requires"
    WAITING_FOR = "waiting_for"
    PRECEDES = "precedes"
    RELATED_TO = "related_to"
    APPROVAL_REQUIRED = "approval_required"
    MATERIAL_DEPENDENCY = "material_dependency"
    INFORMATION_REQUIRED = "information_required"


class DependencyTargetType(str, enum.Enum):
    WORK = "work"
    DEPARTMENT = "department"
    SUPPLIER = "supplier"
    CUSTOMER = "customer"
    MATERIAL = "material"
    APPROVAL = "approval"
    MACHINE = "machine"
    DOCUMENT = "document"
    EXTERNAL_MILESTONE = "external_milestone"


class DependencyStatus(str, enum.Enum):
    OPEN = "open"
    SATISFIED = "satisfied"
    UNKNOWN = "unknown"


class BlockerType(str, enum.Enum):
    EXTERNAL = "external"
    INTERNAL = "internal"
    DEPENDENCY = "dependency"
    INFORMATION = "information"
    APPROVAL = "approval"
    MATERIAL = "material"
    CAPACITY = "capacity"
    UNKNOWN = "unknown"


class BlockerStatus(str, enum.Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    UNKNOWN = "unknown"


class FollowUpTargetType(str, enum.Enum):
    PERSON = "person"
    DEPARTMENT = "department"
    SUPPLIER = "supplier"
    CUSTOMER = "customer"
    INTERNAL = "internal"


class FollowUpStatus(str, enum.Enum):
    SUGGESTED = "suggested"
    OPEN = "open"
    OVERDUE = "overdue"
    DONE = "done"
    CANCELLED = "cancelled"


class Urgency(str, enum.Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class WorkflowInstanceStatus(str, enum.Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class WorkflowStepState(str, enum.Enum):
    """Observed state of a workflow step. Never advanced by elapsed time alone."""

    PENDING = "pending"
    ACTIVE = "active"
    WAITING = "waiting"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


class WorkConflictType(str, enum.Enum):
    DUPLICATE_WORK = "duplicate_work"
    CONFLICTING_OWNER = "conflicting_owner"
    CONFLICTING_DEADLINE = "conflicting_deadline"
    INCONSISTENT_STATUS = "inconsistent_status"
    MISSING_REFERENCE = "missing_reference"
    UNRESOLVED_IDENTITY = "unresolved_identity"
    AMBIGUOUS_ORGANIZATION = "ambiguous_organization"
    INCONSISTENT_PARTY_ID = "inconsistent_party_id"
    CONTRADICTORY_COMMITMENT = "contradictory_commitment"
    STALE_SOURCE = "stale_source"
    SOURCE_SYNC_CONFLICT = "source_sync_conflict"
    CIRCULAR_DEPENDENCY = "circular_dependency"


class WorkConflictStatus(str, enum.Enum):
    OPEN = "open"
    RESOLVED = "resolved"
    ACCEPTED = "accepted"


class ExtractionCandidateType(str, enum.Enum):
    TASK = "task"
    COMMITMENT = "commitment"
    DEADLINE = "deadline"
    DEPENDENCY = "dependency"
    OWNER = "owner"
    BLOCKER = "blocker"


class ExtractionVerification(str, enum.Enum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    REJECTED = "rejected"


class WorkTimelineEventType(str, enum.Enum):
    WORK_CREATED = "work.created"
    WORK_ASSIGNED = "work.assigned"
    WORK_STATUS_CHANGED = "work.status_changed"
    COMMITMENT_CREATED = "commitment.created"
    COMMITMENT_REVISED = "commitment.revised"
    DEADLINE_CHANGED = "deadline.changed"
    DEPARTMENT_UPDATED = "department.updated"
    DEPENDENCY_CREATED = "dependency.created"
    BLOCKER_IDENTIFIED = "blocker.identified"
    FOLLOWUP_CREATED = "followup.created"
    WORK_COMPLETED = "work.completed"
    SOURCE_UPDATED = "source.updated"


class WorkEventStatus(str, enum.Enum):
    PENDING = "pending"
    PUBLISHED = "published"
    FAILED = "failed"


class Department(UUIDPrimaryKey, Timestamped, Base):
    """A tenant-owned organizational unit. Work is assigned to departments, not free text."""

    __tablename__ = "departments"
    __table_args__ = (
        UniqueConstraint("organization_id", "department_key", name="uq_departments_org_key"),
        Index("ix_departments_organization_id", "organization_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    department_key: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    domain: Mapped[WorkDomain] = mapped_column(
        Enum(
            WorkDomain,
            name="work_domain",
            native_enum=True,
            values_callable=_enum_values,
        ),
        nullable=False,
        default=WorkDomain.GENERAL,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class WorkEntity(UUIDPrimaryKey, Base):
    """The canonical unit of work.

    One work entity may be described by several source records (see
    ``work_source_references``); the deterministic ``work_key`` is what makes re-derivation
    an upsert rather than a duplicate.
    """

    __tablename__ = "work_entities"
    __table_args__ = (
        UniqueConstraint("organization_id", "work_key", name="uq_work_entities_org_key"),
        Index("ix_work_entities_organization_id", "organization_id"),
        Index("ix_work_entities_org_status", "organization_id", "status"),
        Index("ix_work_entities_org_type", "organization_id", "work_type"),
        Index("ix_work_entities_org_domain", "organization_id", "domain"),
        Index("ix_work_entities_org_department", "organization_id", "department_id"),
        Index("ix_work_entities_org_owner", "organization_id", "owner_id"),
        Index("ix_work_entities_org_due", "organization_id", "due_at"),
        Index("ix_work_entities_org_updated", "organization_id", "updated_at"),
        Index("ix_work_entities_parent", "parent_work_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    work_key: Mapped[str] = mapped_column(String(300), nullable=False)
    work_type: Mapped[WorkType] = mapped_column(
        Enum(WorkType, name="work_type", native_enum=True, values_callable=_enum_values),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    domain: Mapped[WorkDomain] = mapped_column(
        Enum(WorkDomain, name="work_domain", native_enum=True, values_callable=_enum_values),
        nullable=False,
        default=WorkDomain.GENERAL,
    )
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL")
    )
    owner_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    """A verified owner, or NULL. NULL is the ``UNRESOLVED`` ownership state, never a guess."""
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    creator_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    priority: Mapped[PriorityLevel] = mapped_column(
        Enum(PriorityLevel, name="priority_level", native_enum=True, values_callable=_enum_values),
        nullable=False,
        default=PriorityLevel.NORMAL,
    )
    priority_source: Mapped[PrioritySource] = mapped_column(
        Enum(
            PrioritySource, name="priority_source", native_enum=True, values_callable=_enum_values
        ),
        nullable=False,
        default=PrioritySource.SOURCE,
    )
    status: Mapped[WorkStatus] = mapped_column(
        Enum(WorkStatus, name="work_status", native_enum=True, values_callable=_enum_values),
        nullable=False,
        default=WorkStatus.NEW,
    )
    source_status: Mapped[str | None] = mapped_column(String(120))
    """The source system's own status, preserved verbatim and never interpreted in place."""
    origin: Mapped[WorkOrigin] = mapped_column(
        Enum(WorkOrigin, name="work_origin", native_enum=True, values_callable=_enum_values),
        nullable=False,
        default=WorkOrigin.INFERRED,
    )
    confidence: Mapped[float | None] = mapped_column(Float)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_source: Mapped[DeadlineType | None] = mapped_column(
        Enum(DeadlineType, name="deadline_type", native_enum=True, values_callable=_enum_values)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parent_work_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("work_entities.id", ondelete="SET NULL")
    )
    customer_ref: Mapped[str | None] = mapped_column(String(200))
    supplier_ref: Mapped[str | None] = mapped_column(String(200))
    project_ref: Mapped[str | None] = mapped_column(String(200))
    order_ref: Mapped[str | None] = mapped_column(String(200))
    machine_ref: Mapped[str | None] = mapped_column(String(200))
    """Matches a Layer 3 detection ``entity_id`` when a verified relationship exists."""
    production_ref: Mapped[str | None] = mapped_column(String(200))
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    """Flexible, domain-specific attributes kept separate from the stable canonical fields."""
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class WorkSourceReference(UUIDPrimaryKey, Base):
    """A link from a work entity to a source record that describes it.

    ``relationship`` is what prevents "a person mentioned in an email" from being treated
    as an owner: a ``MENTIONED`` reference is evidence, not an assignment.
    """

    __tablename__ = "work_source_references"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "work_id",
            "canonical_record_id",
            name="uq_work_source_references_org_work_record",
        ),
        Index("ix_work_source_references_organization_id", "organization_id"),
        Index("ix_work_source_references_work", "work_id"),
        Index("ix_work_source_references_canonical", "canonical_record_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    work_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("work_entities.id", ondelete="CASCADE"), nullable=False
    )
    canonical_record_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("canonical_records.id", ondelete="CASCADE"), nullable=False
    )
    raw_record_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("raw_records.id", ondelete="SET NULL")
    )
    source_system: Mapped[str] = mapped_column(String(120), nullable=False)
    source_record_key: Mapped[str] = mapped_column(String(512), nullable=False)
    source_record_type: Mapped[str] = mapped_column(String(120), nullable=False)
    relationship: Mapped[SourceReferenceRelationship] = mapped_column(
        Enum(
            SourceReferenceRelationship,
            name="source_reference_relationship",
            native_enum=True,
            values_callable=_enum_values,
        ),
        nullable=False,
        default=SourceReferenceRelationship.SUPPORTING,
    )
    confidence: Mapped[float | None] = mapped_column(Float)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WorkAssignment(UUIDPrimaryKey, Base):
    """One participant in a work item and the role they hold."""

    __tablename__ = "work_assignments"
    __table_args__ = (
        Index("ix_work_assignments_organization_id", "organization_id"),
        Index("ix_work_assignments_work", "work_id"),
        Index("ix_work_assignments_org_user", "organization_id", "user_id"),
        Index("ix_work_assignments_org_department", "organization_id", "department_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    work_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("work_entities.id", ondelete="CASCADE"), nullable=False
    )
    participant_type: Mapped[ParticipantType] = mapped_column(
        Enum(
            ParticipantType, name="participant_type", native_enum=True, values_callable=_enum_values
        ),
        nullable=False,
        default=ParticipantType.USER,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    department_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    external_name: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[AssignmentRole] = mapped_column(
        Enum(
            AssignmentRole, name="assignment_role", native_enum=True, values_callable=_enum_values
        ),
        nullable=False,
    )
    verification: Mapped[VerificationStatus] = mapped_column(
        Enum(
            VerificationStatus,
            name="verification_status",
            native_enum=True,
            values_callable=_enum_values,
        ),
        nullable=False,
        default=VerificationStatus.UNRESOLVED,
    )
    confidence: Mapped[float | None] = mapped_column(Float)
    source_reference_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("work_source_references.id", ondelete="SET NULL")
    )
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class WorkStatusHistory(UUIDPrimaryKey, Base):
    """Append-only record of a work item's status transitions."""

    __tablename__ = "work_status_history"
    __table_args__ = (
        Index("ix_work_status_history_organization_id", "organization_id"),
        Index("ix_work_status_history_work", "work_id", "created_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    work_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("work_entities.id", ondelete="CASCADE"), nullable=False
    )
    from_status: Mapped[str | None] = mapped_column(String(30))
    to_status: Mapped[str] = mapped_column(String(30), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    reason: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WorkPriority(UUIDPrimaryKey, Base):
    """Deterministic prioritization metadata. Never authoritative over a human priority."""

    __tablename__ = "work_priorities"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "work_id", "source", name="uq_work_priorities_org_work_source"
        ),
        Index("ix_work_priorities_organization_id", "organization_id"),
        Index("ix_work_priorities_work", "work_id"),
        Index("ix_work_priorities_org_level", "organization_id", "level"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    work_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("work_entities.id", ondelete="CASCADE"), nullable=False
    )
    level: Mapped[PriorityLevel] = mapped_column(
        Enum(PriorityLevel, name="priority_level", native_enum=True, values_callable=_enum_values),
        nullable=False,
        default=PriorityLevel.NORMAL,
    )
    source: Mapped[PrioritySource] = mapped_column(
        Enum(
            PrioritySource, name="priority_source", native_enum=True, values_callable=_enum_values
        ),
        nullable=False,
    )
    overdue: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    due_soon: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    blocked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    dependency_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    waiting_duration_seconds: Mapped[int | None] = mapped_column(Integer)
    commitment_status: Mapped[str | None] = mapped_column(String(30))
    source_completeness: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    related_detection_ids: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WorkCommitment(UUIDPrimaryKey, Base):
    """A promise made by one party to another, with its verification state.

    A requested date is not a confirmed promise: ``REQUESTED`` and ``PROPOSED`` are
    distinct statuses from ``CONFIRMED`` and ``CONTRACTUAL``.
    """

    __tablename__ = "work_commitments"
    __table_args__ = (
        UniqueConstraint("organization_id", "commitment_key", name="uq_work_commitments_org_key"),
        Index("ix_work_commitments_organization_id", "organization_id"),
        Index("ix_work_commitments_work", "work_id"),
        Index("ix_work_commitments_org_due", "organization_id", "due_at"),
        Index("ix_work_commitments_org_status", "organization_id", "commitment_status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    commitment_key: Mapped[str] = mapped_column(String(300), nullable=False)
    work_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("work_entities.id", ondelete="SET NULL")
    )
    commitment_type: Mapped[CommitmentType] = mapped_column(
        Enum(
            CommitmentType, name="commitment_type", native_enum=True, values_callable=_enum_values
        ),
        nullable=False,
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    committing_party: Mapped[str] = mapped_column(String(200), nullable=False)
    receiving_party: Mapped[str] = mapped_column(String(200), nullable=False)
    responsible_owner: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    related_customer_ref: Mapped[str | None] = mapped_column(String(200))
    related_supplier_ref: Mapped[str | None] = mapped_column(String(200))
    related_order_ref: Mapped[str | None] = mapped_column(String(200))
    promised_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actual_completion_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    commitment_status: Mapped[CommitmentStatus] = mapped_column(
        Enum(
            CommitmentStatus,
            name="commitment_status",
            native_enum=True,
            values_callable=_enum_values,
        ),
        nullable=False,
        default=CommitmentStatus.PROPOSED,
    )
    verification_status: Mapped[VerificationStatus] = mapped_column(
        Enum(
            VerificationStatus,
            name="verification_status",
            native_enum=True,
            values_callable=_enum_values,
        ),
        nullable=False,
        default=VerificationStatus.UNRESOLVED,
    )
    confidence: Mapped[float | None] = mapped_column(Float)
    source_reference: Mapped[str | None] = mapped_column(String(300))
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class WorkCommitmentHistory(UUIDPrimaryKey, Base):
    """Append-only history of commitment revisions, so a moved date is visible, not erased."""

    __tablename__ = "work_commitment_history"
    __table_args__ = (
        Index("ix_work_commitment_history_organization_id", "organization_id"),
        Index("ix_work_commitment_history_commitment", "commitment_id", "created_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    commitment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("work_commitments.id", ondelete="CASCADE"), nullable=False
    )
    from_status: Mapped[str | None] = mapped_column(String(30))
    to_status: Mapped[str] = mapped_column(String(30), nullable=False)
    previous_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    new_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    reason: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WorkDependency(UUIDPrimaryKey, Base):
    """A typed edge from a work item to another work item or an external entity."""

    __tablename__ = "work_dependencies"
    __table_args__ = (
        Index("ix_work_dependencies_organization_id", "organization_id"),
        Index("ix_work_dependencies_source", "source_work_id"),
        Index("ix_work_dependencies_target", "target_work_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    source_work_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("work_entities.id", ondelete="CASCADE"), nullable=False
    )
    target_work_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("work_entities.id", ondelete="SET NULL")
    )
    target_entity_type: Mapped[DependencyTargetType] = mapped_column(
        Enum(
            DependencyTargetType,
            name="dependency_target_type",
            native_enum=True,
            values_callable=_enum_values,
        ),
        nullable=False,
        default=DependencyTargetType.WORK,
    )
    target_entity_ref: Mapped[str | None] = mapped_column(String(300))
    relationship: Mapped[DependencyRelationship] = mapped_column(
        Enum(
            DependencyRelationship,
            name="dependency_relationship",
            native_enum=True,
            values_callable=_enum_values,
        ),
        nullable=False,
    )
    status: Mapped[DependencyStatus] = mapped_column(
        Enum(
            DependencyStatus,
            name="dependency_status",
            native_enum=True,
            values_callable=_enum_values,
        ),
        nullable=False,
        default=DependencyStatus.UNKNOWN,
    )
    origin: Mapped[WorkOrigin] = mapped_column(
        Enum(WorkOrigin, name="work_origin", native_enum=True, values_callable=_enum_values),
        nullable=False,
        default=WorkOrigin.INFERRED,
    )
    confidence: Mapped[float | None] = mapped_column(Float)
    verification_status: Mapped[VerificationStatus] = mapped_column(
        Enum(
            VerificationStatus,
            name="verification_status",
            native_enum=True,
            values_callable=_enum_values,
        ),
        nullable=False,
        default=VerificationStatus.UNRESOLVED,
    )
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class WorkBlocker(UUIDPrimaryKey, Base):
    """Why work is not progressing.

    ``blocker_type = UNKNOWN`` and ``verification_status = UNRESOLVED`` are the honest
    representation when the reason is not evidenced.
    """

    __tablename__ = "work_blockers"
    __table_args__ = (
        Index("ix_work_blockers_organization_id", "organization_id"),
        Index("ix_work_blockers_work", "affected_work_id"),
        Index("ix_work_blockers_org_status", "organization_id", "blocker_status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    affected_work_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("work_entities.id", ondelete="CASCADE"), nullable=False
    )
    blocker_type: Mapped[BlockerType] = mapped_column(
        Enum(BlockerType, name="blocker_type", native_enum=True, values_callable=_enum_values),
        nullable=False,
        default=BlockerType.UNKNOWN,
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    blocking_entity: Mapped[str | None] = mapped_column(String(300))
    responsible_department_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL")
    )
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expected_resolution_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    blocker_status: Mapped[BlockerStatus] = mapped_column(
        Enum(BlockerStatus, name="blocker_status", native_enum=True, values_callable=_enum_values),
        nullable=False,
        default=BlockerStatus.OPEN,
    )
    evidence_references: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    """References only, never payload values: the platform's provenance-not-payloads rule."""
    provenance: Mapped[WorkOrigin] = mapped_column(
        Enum(WorkOrigin, name="work_origin", native_enum=True, values_callable=_enum_values),
        nullable=False,
        default=WorkOrigin.INFERRED,
    )
    verification_status: Mapped[VerificationStatus] = mapped_column(
        Enum(
            VerificationStatus,
            name="verification_status",
            native_enum=True,
            values_callable=_enum_values,
        ),
        nullable=False,
        default=VerificationStatus.UNRESOLVED,
    )
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class WorkFollowUp(UUIDPrimaryKey, Base):
    """Something requiring follow-up, produced by a documented deterministic rule."""

    __tablename__ = "work_followups"
    __table_args__ = (
        Index("ix_work_followups_organization_id", "organization_id"),
        Index("ix_work_followups_work", "related_work_id"),
        Index("ix_work_followups_org_status", "organization_id", "follow_up_status"),
        Index("ix_work_followups_org_due", "organization_id", "next_due_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    related_work_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("work_entities.id", ondelete="CASCADE"), nullable=False
    )
    target_type: Mapped[FollowUpTargetType] = mapped_column(
        Enum(
            FollowUpTargetType,
            name="followup_target_type",
            native_enum=True,
            values_callable=_enum_values,
        ),
        nullable=False,
    )
    target_ref: Mapped[str] = mapped_column(String(300), nullable=False)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    urgency: Mapped[Urgency] = mapped_column(
        Enum(Urgency, name="urgency", native_enum=True, values_callable=_enum_values),
        nullable=False,
        default=Urgency.NORMAL,
    )
    follow_up_status: Mapped[FollowUpStatus] = mapped_column(
        Enum(
            FollowUpStatus, name="followup_status", native_enum=True, values_callable=_enum_values
        ),
        nullable=False,
        default=FollowUpStatus.SUGGESTED,
    )
    rule_id: Mapped[str] = mapped_column(String(120), nullable=False)
    provenance: Mapped[str] = mapped_column(String(20), nullable=False, default="rule")
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class WorkflowDefinition(UUIDPrimaryKey, Base):
    """A tenant-configurable workflow. No manufacturing workflow is hardcoded in the engine."""

    __tablename__ = "workflow_definitions"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "workflow_key",
            "version",
            name="uq_workflow_definitions_org_key_version",
        ),
        Index("ix_workflow_definitions_organization_id", "organization_id"),
        Index("ix_workflow_definitions_org_type", "organization_id", "workflow_type"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    workflow_key: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    workflow_type: Mapped[str] = mapped_column(String(80), nullable=False)
    department_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("departments.id", ondelete="SET NULL")
    )
    entry_conditions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    exit_conditions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    steps: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    status_mappings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WorkflowInstance(UUIDPrimaryKey, Base):
    """Binds a work item to a workflow definition version."""

    __tablename__ = "workflow_instances"
    __table_args__ = (
        Index("ix_workflow_instances_organization_id", "organization_id"),
        Index("ix_workflow_instances_definition", "definition_id"),
        Index("ix_workflow_instances_work", "work_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    definition_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workflow_definitions.id", ondelete="CASCADE"),
        nullable=False,
    )
    work_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("work_entities.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[WorkflowInstanceStatus] = mapped_column(
        Enum(
            WorkflowInstanceStatus,
            name="workflow_instance_status",
            native_enum=True,
            values_callable=_enum_values,
        ),
        nullable=False,
        default=WorkflowInstanceStatus.ACTIVE,
    )
    current_step_key: Mapped[str | None] = mapped_column(String(120))
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class WorkflowStep(UUIDPrimaryKey, Base):
    """Observed state of one step of a workflow instance."""

    __tablename__ = "workflow_steps"
    __table_args__ = (
        UniqueConstraint(
            "organization_id", "instance_id", "step_key", name="uq_workflow_steps_instance_step"
        ),
        Index("ix_workflow_steps_organization_id", "organization_id"),
        Index("ix_workflow_steps_instance", "instance_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workflow_instances.id", ondelete="CASCADE"), nullable=False
    )
    step_key: Mapped[str] = mapped_column(String(120), nullable=False)
    step_name: Mapped[str] = mapped_column(String(200), nullable=False)
    step_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    responsible_role: Mapped[str | None] = mapped_column(String(80))
    observed_state: Mapped[WorkflowStepState] = mapped_column(
        Enum(
            WorkflowStepState,
            name="workflow_step_state",
            native_enum=True,
            values_callable=_enum_values,
        ),
        nullable=False,
        default=WorkflowStepState.UNKNOWN,
    )
    depends_on: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    source_reference: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class WorkTimelineEvent(UUIDPrimaryKey, Base):
    """Append-only unified work timeline.

    Four distinct times are preserved and never conflated: when the event occurred, the
    source's own timestamp, when Layer 4 ingested it, and when Layer 4 processed it.
    """

    __tablename__ = "work_timeline_events"
    __table_args__ = (
        Index("ix_work_timeline_events_organization_id", "organization_id"),
        Index("ix_work_timeline_events_work", "work_id", "event_at"),
        Index("ix_work_timeline_events_org_type", "organization_id", "event_type"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    work_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("work_entities.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False, default="system")
    previous_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    new_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    source_reference: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    provenance: Mapped[str] = mapped_column(String(20), nullable=False, default="inferred")
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WorkConflict(UUIDPrimaryKey, Base):
    """A detected conflict between sources. Never silently resolved."""

    __tablename__ = "work_conflicts"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "entity_type",
            "entity_key",
            "conflict_type",
            name="uq_work_conflicts_org_entity_type",
        ),
        Index("ix_work_conflicts_organization_id", "organization_id"),
        Index("ix_work_conflicts_org_status", "organization_id", "status"),
        Index("ix_work_conflicts_work", "work_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    work_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("work_entities.id", ondelete="SET NULL")
    )
    entity_type: Mapped[str] = mapped_column(String(60), nullable=False)
    entity_key: Mapped[str] = mapped_column(String(300), nullable=False)
    conflict_type: Mapped[WorkConflictType] = mapped_column(
        Enum(
            WorkConflictType,
            name="work_conflict_type",
            native_enum=True,
            values_callable=_enum_values,
        ),
        nullable=False,
    )
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    candidates: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[WorkConflictStatus] = mapped_column(
        Enum(
            WorkConflictStatus,
            name="work_conflict_status",
            native_enum=True,
            values_callable=_enum_values,
        ),
        nullable=False,
        default=WorkConflictStatus.OPEN,
    )
    resolved_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WorkExtraction(UUIDPrimaryKey, Base):
    """A candidate work fact received from an approved upstream extractor.

    An extraction is a candidate, never an assignment. It becomes work only through a
    deterministic promotion path or an authorized action.
    """

    __tablename__ = "work_extractions"
    __table_args__ = (
        UniqueConstraint("organization_id", "extraction_key", name="uq_work_extractions_org_key"),
        Index("ix_work_extractions_organization_id", "organization_id"),
        Index("ix_work_extractions_org_status", "organization_id", "verification_status"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    extraction_key: Mapped[str] = mapped_column(String(300), nullable=False)
    canonical_record_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("canonical_records.id", ondelete="SET NULL")
    )
    source_reference: Mapped[str | None] = mapped_column(String(300))
    extraction_method: Mapped[str] = mapped_column(String(120), nullable=False)
    extraction_version: Mapped[str] = mapped_column(String(40), nullable=False)
    candidate_type: Mapped[ExtractionCandidateType] = mapped_column(
        Enum(
            ExtractionCandidateType,
            name="extraction_candidate_type",
            native_enum=True,
            values_callable=_enum_values,
        ),
        nullable=False,
    )
    fields: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    verification_status: Mapped[ExtractionVerification] = mapped_column(
        Enum(
            ExtractionVerification,
            name="extraction_verification",
            native_enum=True,
            values_callable=_enum_values,
        ),
        nullable=False,
        default=ExtractionVerification.UNVERIFIED,
    )
    promoted_work_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("work_entities.id", ondelete="SET NULL")
    )
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class WorkOutboxEvent(UUIDPrimaryKey, Base):
    """Transactional outbox for Layer 4 events.

    Layer 2's outbox is keyed to a canonical record by a NOT NULL foreign key, so it cannot
    carry a work event. This outbox references the work entity (the aggregate for every
    Layer 4 event) and is written in the same transaction as the state change it describes,
    so an event exists if and only if the change committed.
    """

    __tablename__ = "work_outbox_events"
    __table_args__ = (
        UniqueConstraint("organization_id", "idempotency_key", name="uq_work_outbox_org_key"),
        Index("ix_work_outbox_events_organization_id", "organization_id"),
        Index("ix_work_outbox_events_org_status", "organization_id", "status"),
        Index("ix_work_outbox_events_status_created", "status", "created_at"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    work_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("work_entities.id", ondelete="CASCADE")
    )
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(300), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[WorkEventStatus] = mapped_column(
        Enum(
            WorkEventStatus,
            name="work_event_status",
            native_enum=True,
            values_callable=_enum_values,
        ),
        nullable=False,
        default=WorkEventStatus.PENDING,
    )
    attempt: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
