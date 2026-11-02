"""Layer 2 schema: ingestion, raw preservation, canonical records, quarantine, outbox.

Revision ID: 0002_layer2
Revises: 0001_initial
Create Date: 2026-01-02

This revision adds the Layer 2 tables, then applies the same Row-Level Security treatment
Layer 1 uses:

* ``ENABLE`` **and** ``FORCE`` Row-Level Security on every tenant-owned table, with a policy
  requiring ``organization_id = app_current_org()``.
* An ``UPDATE``-rejecting trigger on ``raw_records`` so the preserved source payload is
  immutable at the database boundary, not merely by application convention.
* An ``UPDATE``/``DELETE``-rejecting trigger on ``record_processing_events`` so the
  processing ledger is append-only.

Indexes follow query patterns: list by tenant, filter by tenant+connection, tenant+state,
tenant+record type, and tenant+source timestamp.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from humisense.db.rls import apply_layer2_rls, remove_layer2_rls

revision: str = "0002_layer2"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    batch_status = postgresql.ENUM(
        "received", "processing", "completed", "partial", "failed",
        name="ingest_batch_status", create_type=False,
    )
    record_state = postgresql.ENUM(
        "received", "validating", "validated", "normalizing", "normalized", "stored",
        "published", "retry_pending", "failed", "quarantined", "duplicate",
        name="record_state", create_type=False,
    )
    quarantine_status = postgresql.ENUM(
        "open", "replayed", "discarded", name="quarantine_status", create_type=False
    )
    outbox_status = postgresql.ENUM(
        "pending", "published", "failed", name="outbox_status", create_type=False
    )
    for enum in (batch_status, record_state, quarantine_status, outbox_status):
        enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "ingest_batches",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("batch_id", sa.String(64), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("producer", sa.String(80)),
        sa.Column("status", batch_status, nullable=False, server_default="received"),
        sa.Column("records_received", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_accepted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_duplicate", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("records_quarantined", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(80)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("organization_id", "batch_id", name="uq_ingest_batches_org_batch"),
        sa.CheckConstraint("records_received >= 0", name="ck_ingest_batches_records_received_non_negative"),
    )
    op.create_index("ix_ingest_batches_organization_id", "ingest_batches", ["organization_id"])
    op.create_index(
        "ix_ingest_batches_organization_created", "ingest_batches", ["organization_id", "created_at"]
    )

    op.create_table(
        "raw_records",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("connection_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_record_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("source_records.id", ondelete="SET NULL")),
        sa.Column("batch_row_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("ingest_batches.id", ondelete="SET NULL")),
        sa.Column("connector_type", sa.String(80), nullable=False),
        sa.Column("source_record_key", sa.String(512), nullable=False),
        sa.Column("record_type", sa.String(120), nullable=False),
        sa.Column("source_schema_version", sa.String(20), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("source_timestamp", sa.DateTime(timezone=True)),
        sa.Column("observed_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_timestamp", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("record_metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("content_hash", sa.String(128), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("untrusted", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.UniqueConstraint(
            "organization_id", "connection_id", "source_record_key", "content_hash",
            name="uq_raw_records_identity",
        ),
    )
    op.create_index("ix_raw_records_organization_id", "raw_records", ["organization_id"])
    op.create_index("ix_raw_records_org_connection", "raw_records", ["organization_id", "connection_id"])
    op.create_index("ix_raw_records_org_source_ts", "raw_records", ["organization_id", "source_timestamp"])
    op.create_index("ix_raw_records_batch", "raw_records", ["batch_row_id"])

    op.create_table(
        "canonical_records",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("raw_record_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("raw_records.id", ondelete="CASCADE"), nullable=False),
        sa.Column("connection_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("source_record_key", sa.String(512), nullable=False),
        sa.Column("record_type", sa.String(120), nullable=False),
        sa.Column("entity_type", sa.String(120)),
        sa.Column("event_type", sa.String(120)),
        sa.Column("data_family", sa.String(20), nullable=False),
        sa.Column("source_timestamp", sa.DateTime(timezone=True)),
        sa.Column("event_timestamp", sa.DateTime(timezone=True)),
        sa.Column("observed_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_timestamp", sa.DateTime(timezone=True)),
        sa.Column("source_timezone", sa.String(64)),
        sa.Column("source_time_metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("normalized_payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("extensions", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("record_metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("unit_conversions", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("quality_code", sa.String(40)),
        sa.Column("schema_version", sa.String(20), nullable=False),
        sa.Column("source_schema_version", sa.String(20), nullable=False),
        sa.Column("processing_version", sa.String(40), nullable=False),
        sa.Column("normalizer", sa.String(120)),
        sa.Column("normalizer_version", sa.String(40)),
        sa.Column("processing_state", record_state, nullable=False, server_default="received"),
        sa.Column("attempt", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("last_error_code", sa.String(80)),
        sa.Column("last_error_detail", sa.Text()),
        sa.Column("superseded_by", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("canonical_records.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("organization_id", "idempotency_key", name="uq_canonical_records_identity"),
    )
    op.create_index("ix_canonical_records_organization_id", "canonical_records", ["organization_id"])
    op.create_index("ix_canonical_records_org_record_type", "canonical_records", ["organization_id", "record_type"])
    op.create_index("ix_canonical_records_org_source_ts", "canonical_records", ["organization_id", "source_timestamp"])
    op.create_index("ix_canonical_records_org_state", "canonical_records", ["organization_id", "processing_state"])
    op.create_index("ix_canonical_records_raw", "canonical_records", ["raw_record_id"])

    op.create_table(
        "record_quarantine",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("raw_record_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("raw_records.id", ondelete="CASCADE")),
        sa.Column("canonical_record_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("canonical_records.id", ondelete="CASCADE")),
        sa.Column("batch_row_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("ingest_batches.id", ondelete="SET NULL")),
        sa.Column("connection_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("connections.id", ondelete="CASCADE")),
        sa.Column("source_record_key", sa.String(512)),
        sa.Column("stage", sa.String(30), nullable=False),
        sa.Column("error_code", sa.String(80), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("payload_preview", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("status", quarantine_status, nullable=False, server_default="open"),
        sa.Column("replay_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("resolved_by", sa.Uuid(as_uuid=True)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_record_quarantine_organization_id", "record_quarantine", ["organization_id"])
    op.create_index("ix_record_quarantine_org_status", "record_quarantine", ["organization_id", "status"])
    op.create_index("ix_record_quarantine_org_created", "record_quarantine", ["organization_id", "created_at"])

    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("canonical_record_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("canonical_records.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("status", outbox_status, nullable=False, server_default="pending"),
        sa.Column("attempt", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.String(80)),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("canonical_record_id", name="uq_outbox_events_canonical_record"),
    )
    op.create_index("ix_outbox_events_organization_id", "outbox_events", ["organization_id"])
    op.create_index("ix_outbox_events_org_status", "outbox_events", ["organization_id", "status"])
    op.create_index("ix_outbox_events_status_created", "outbox_events", ["status", "created_at"])

    op.create_table(
        "record_processing_events",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        sa.Column("organization_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("canonical_record_id", sa.Uuid(as_uuid=True),
                  sa.ForeignKey("canonical_records.id", ondelete="CASCADE")),
        sa.Column("from_state", sa.String(30)),
        sa.Column("to_state", sa.String(30), nullable=False),
        sa.Column("stage", sa.String(30), nullable=False),
        sa.Column("error_code", sa.String(80)),
        sa.Column("detail", sa.Text()),
        sa.Column("attempt", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_record_processing_events_organization_id", "record_processing_events", ["organization_id"]
    )
    op.create_index(
        "ix_record_processing_events_canonical",
        "record_processing_events",
        ["canonical_record_id", "created_at"],
    )

    apply_layer2_rls()


def downgrade() -> None:
    remove_layer2_rls()
    for table in (
        "record_processing_events",
        "outbox_events",
        "record_quarantine",
        "canonical_records",
        "raw_records",
        "ingest_batches",
    ):
        op.drop_table(table)
    for name in ("outbox_status", "quarantine_status", "record_state", "ingest_batch_status"):
        op.execute(f"DROP TYPE IF EXISTS {name}")
