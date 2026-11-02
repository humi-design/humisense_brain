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
            "organization_id", "entity_id", "metric", "window_end",
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
            "organization_id", "entity_id", "metric", "method", "window_end",
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
            "organization_id", "entity_id", "metric", "name", "version", "at",
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
            "organization_id", "entity_id", "metric", "baseline_type",
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
            "organization_id", "entity_id", "metric", "algorithm_name", "window_end",
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
            "organization_id", "entity_id", "metric", "detected_at",
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
            "organization_id", "entity_id", "metric", "generated_at", "horizon_steps",
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
            "organization_id", "entity_a", "metric_a", "entity_b", "metric_b", "method",
            "window_end", name="uq_correlation_results_observation",
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
