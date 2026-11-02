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
