"""Initial schema with Row-Level Security.

Revision ID: 0001_initial
Revises:
Create Date: 2026-01-01

This revision creates every table in the Layer 1 data model, then applies:

* the ``app_current_org()`` helper that reads a transaction-local GUC;
* ``ENABLE`` **and** ``FORCE`` Row-Level Security on every tenant-owned table, with a
  policy whose ``USING`` and ``WITH CHECK`` both require ``organization_id`` to equal the
  current tenant, so a connection without tenant context matches no rows;
* a trigger that rejects ``UPDATE`` and ``DELETE`` on ``audit_events``, making the audit
  trail append-only at the database level.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from humisense.db.rls import apply_rls, remove_rls

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    role_enum = postgresql.ENUM(
        "viewer", "operator", "admin", "owner", name="role", create_type=False
    )
    connection_status_enum = postgresql.ENUM(
        "pending", "active", "error", "disabled", name="connection_status", create_type=False
    )
    sync_status_enum = postgresql.ENUM(
        "queued",
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "partial",
        name="sync_status",
        create_type=False,
    )
    receipt_status_enum = postgresql.ENUM(
        "pending", "accepted", "rejected", "failed", name="receipt_status", create_type=False
    )
    actor_type_enum = postgresql.ENUM(
        "user", "system", "worker", name="actor_type", create_type=False
    )

    for enum in (
        role_enum,
        connection_status_enum,
        sync_status_enum,
        receipt_status_enum,
        actor_type_enum,
    ):
        enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "organizations",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(80), nullable=False, unique=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("issuer", sa.String(500), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("email", sa.String(320)),
        sa.Column("display_name", sa.String(200)),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("issuer", "subject", name="uq_users_issuer_subject"),
    )

    op.create_table(
        "memberships",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "organization_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", role_enum, nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "organization_id", name="uq_memberships_user_org"),
    )
    op.create_index("ix_memberships_organization_id", "memberships", ["organization_id"])

    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "organization_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("csrf_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("auth_method", sa.String(20), nullable=False, server_default="dev"),
        sa.Column("client_fingerprint", sa.String(64)),
    )
    op.create_index("ix_sessions_token_hash", "sessions", ["token_hash"], unique=True)
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])

    op.create_table(
        "connections",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("connector_type", sa.String(80), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("config", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("secret_ref", sa.String(255)),
        sa.Column("status", connection_status_enum, nullable=False, server_default="pending"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("capabilities", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("checkpoint", postgresql.JSONB()),
        sa.Column("last_tested_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(80)),
        sa.Column("created_by", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("organization_id", "name", name="uq_connections_organization_id_name"),
        sa.CheckConstraint("char_length(name) >= 1", name="ck_connections_name_not_empty"),
    )
    op.create_index("ix_connections_organization_id", "connections", ["organization_id"])

    op.create_table(
        "sync_jobs",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "connection_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sync_status_enum, nullable=False, server_default="queued"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("cursor_before", postgresql.JSONB()),
        sa.Column("cursor_after", postgresql.JSONB()),
        sa.Column("records_fetched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_persisted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_delivered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(80)),
        sa.Column("error_message", sa.Text()),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Float()),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("triggered_by", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.CheckConstraint("attempt >= 0", name="ck_sync_jobs_attempt_non_negative"),
    )
    op.create_index("ix_sync_jobs_organization_id", "sync_jobs", ["organization_id"])
    op.create_index("ix_sync_jobs_connection_id", "sync_jobs", ["connection_id"])
    op.create_index("ix_sync_jobs_status", "sync_jobs", ["status"])

    op.create_table(
        "source_records",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "connection_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sync_job_id", sa.Uuid(as_uuid=True), sa.ForeignKey("sync_jobs.id", ondelete="SET NULL")),
        sa.Column("connector_type", sa.String(80), nullable=False),
        sa.Column("source_record_id", sa.String(512), nullable=False),
        sa.Column("record_type", sa.String(120), nullable=False),
        sa.Column("schema_version", sa.String(20), nullable=False),
        sa.Column("source_timestamp", sa.DateTime(timezone=True)),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("record_metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "connection_id", "source_record_id", "content_hash", name="uq_source_records_identity"
        ),
    )
    op.create_index("ix_source_records_organization_id", "source_records", ["organization_id"])
    op.create_index(
        "ix_source_records_connection_id_captured_at",
        "source_records",
        ["connection_id", "captured_at"],
    )

    op.create_table(
        "ingestion_receipts",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_record_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("source_records.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sync_job_id", sa.Uuid(as_uuid=True), sa.ForeignKey("sync_jobs.id", ondelete="SET NULL")),
        sa.Column("status", receipt_status_enum, nullable=False, server_default="pending"),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(80)),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("source_record_id", name="uq_ingestion_receipts_source_record_id"),
    )
    op.create_index("ix_ingestion_receipts_organization_id", "ingestion_receipts", ["organization_id"])

    op.create_table(
        "secrets",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("algorithm", sa.String(40), nullable=False, server_default="AES-256-GCM"),
        sa.Column("key_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("nonce", sa.LargeBinary(12), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("created_by", sa.Uuid(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("organization_id", "name", name="uq_secrets_organization_id_name"),
    )
    op.create_index("ix_secrets_organization_id", "secrets", ["organization_id"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            sa.Uuid(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="SET NULL"),
        ),
        sa.Column("actor_id", sa.Uuid(as_uuid=True)),
        sa.Column("actor_type", actor_type_enum, nullable=False, server_default="user"),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("target_type", sa.String(60)),
        sa.Column("target_id", sa.String(64)),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("correlation_id", sa.String(64)),
        sa.Column("event_metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_audit_events_organization_id_created_at",
        "audit_events",
        ["organization_id", "created_at"],
    )
    op.create_index("ix_audit_events_action", "audit_events", ["action"])
    op.create_index("ix_audit_events_correlation_id", "audit_events", ["correlation_id"])

    apply_rls()


def downgrade() -> None:
    remove_rls()
    for table in (
        "audit_events",
        "secrets",
        "ingestion_receipts",
        "source_records",
        "sync_jobs",
        "connections",
        "sessions",
        "memberships",
        "users",
        "organizations",
    ):
        op.drop_table(table)
    for name in ("actor_type", "receipt_status", "sync_status", "connection_status", "role"):
        op.execute(f"DROP TYPE IF EXISTS {name}")
