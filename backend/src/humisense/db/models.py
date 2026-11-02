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
    BigInteger,
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
    source_time_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

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

