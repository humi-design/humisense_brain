"""Source-record persistence and envelope construction.

Layer 1's job ends at a validated, persisted envelope with a stable identity. It does not
normalize, deduplicate semantically, or convert units.

Idempotency
-----------
``(connection_id, source_record_id, content_hash)`` is unique. Re-capturing an unchanged
record is a no-op, which is what makes the documented at-least-once delivery safe. A
record whose *content* changed under the same source id is a new row, because the hash
differs; that is intentional, since Layer 2 needs to see the change.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult

from humisense.connectors.types import (
    SCHEMA_VERSION,
    SourceRecordDraft,
    SourceRecordEnvelope,
)
from humisense.db.models import SourceRecord
from humisense.db.session import Database
from humisense.errors import ConnectorDataError
from humisense.security.crypto import content_hash

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PersistedRecord:
    """A record after persistence, with the envelope Layer 2 will receive."""

    id: uuid.UUID
    envelope: SourceRecordEnvelope
    is_new: bool


def _affected_rows(result: Any) -> int:
    """Return the number of rows an UPDATE or DELETE affected.

    ``Session.execute`` is typed as returning ``Result[Any]``, which has no ``rowcount``.
    For a DML statement the runtime object is a ``CursorResult``, which does. This keeps
    the cast in one place instead of scattering ignores across call sites.
    """
    return int(cast(CursorResult[Any], result).rowcount or 0)


def build_envelope(
    *,
    draft: SourceRecordDraft,
    organization_id: uuid.UUID,
    connection_id: uuid.UUID,
    connector_type: str,
    correlation_id: str,
    captured_at: datetime | None = None,
) -> SourceRecordEnvelope:
    """Build the standard envelope from a captured draft.

    Raises:
        ConnectorDataError: when the record exceeds the per-record size cap.
    """
    import json

    from humisense.connectors.types import MAX_PAYLOAD_BYTES

    payload_bytes = len(
        json.dumps(draft.payload, separators=(",", ":"), ensure_ascii=False, default=str).encode(
            "utf-8"
        )
    )
    if payload_bytes > MAX_PAYLOAD_BYTES:
        raise ConnectorDataError(
            detail="Record payload exceeded the per-record size limit",
            context={"payload_bytes": payload_bytes, "max_bytes": MAX_PAYLOAD_BYTES},
        )

    envelope = SourceRecordEnvelope(
        connector_type=connector_type,
        connection_id=connection_id,
        organization_id=organization_id,
        source_record_id=draft.source_record_id,
        record_type=draft.record_type,
        source_timestamp=draft.source_timestamp,
        captured_at=captured_at or datetime.now(UTC),
        payload=draft.payload,
        metadata={**draft.metadata, "untrusted": True, "schema_version": SCHEMA_VERSION},
        content_hash=content_hash(draft.payload),
        correlation_id=correlation_id,
        untrusted=True,
    )
    return envelope


class RecordService:
    """Persists source records idempotently and reads them back tenant-scoped."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def persist_batch(
        self,
        *,
        organization_id: uuid.UUID,
        connection_id: uuid.UUID,
        connector_type: str,
        correlation_id: str,
        drafts: list[SourceRecordDraft],
        sync_job_id: uuid.UUID | None = None,
    ) -> list[PersistedRecord]:
        """Persist a batch, skipping records that already exist unchanged.

        Uses ``INSERT ... ON CONFLICT DO NOTHING`` so concurrent workers cannot create
        duplicates, then reads back the rows to build envelopes.
        """
        if not drafts:
            return []

        envelopes = [
            build_envelope(
                draft=draft,
                organization_id=organization_id,
                connection_id=connection_id,
                connector_type=connector_type,
                correlation_id=correlation_id,
            )
            for draft in drafts
        ]

        rows = [
            {
                "organization_id": organization_id,
                "connection_id": connection_id,
                "sync_job_id": sync_job_id,
                "connector_type": connector_type,
                "source_record_id": envelope.source_record_id,
                "record_type": envelope.record_type,
                "schema_version": SCHEMA_VERSION,
                "source_timestamp": envelope.source_timestamp,
                "captured_at": envelope.captured_at,
                "payload": envelope.payload,
                "record_metadata": envelope.metadata,
                "content_hash": envelope.content_hash,
                "correlation_id": correlation_id,
            }
            for envelope in envelopes
        ]

        persisted: list[PersistedRecord] = []
        async with self._database.tenant_session(organization_id) as session:
            statement = (
                pg_insert(SourceRecord)
                .values(rows)
                .on_conflict_do_nothing(constraint="uq_source_records_identity")
                .returning(SourceRecord.id, SourceRecord.source_record_id)
            )
            inserted = {row.source_record_id: row.id for row in (await session.execute(statement))}

            # Resolve ids for records that already existed, so the caller still gets a
            # usable handle without a second write.
            missing = [
                envelope.source_record_id
                for envelope in envelopes
                if envelope.source_record_id not in inserted
            ]
            existing_ids: dict[str, uuid.UUID] = {}
            if missing:
                found = await session.execute(
                    select(SourceRecord.source_record_id, SourceRecord.id).where(
                        SourceRecord.connection_id == connection_id,
                        SourceRecord.source_record_id.in_(missing),
                    )
                )
                existing_ids = {row.source_record_id: row.id for row in found}

        for envelope in envelopes:
            record_id = inserted.get(envelope.source_record_id) or existing_ids.get(
                envelope.source_record_id
            )
            if record_id is None:
                # The row exists but was filtered by RLS or removed concurrently. Treat
                # it as not persisted rather than fabricating an id.
                logger.warning(
                    "record_persist_unresolved",
                    connection_id=str(connection_id),
                )
                continue
            persisted.append(
                PersistedRecord(
                    id=record_id,
                    envelope=envelope,
                    is_new=envelope.source_record_id in inserted,
                )
            )
        return persisted

    async def list_records(
        self,
        organization_id: uuid.UUID,
        connection_id: uuid.UUID,
        *,
        limit: int,
        offset: int = 0,
        record_type: str | None = None,
    ) -> list[SourceRecord]:
        """Return captured records for one connection, newest first."""
        async with self._database.tenant_session(organization_id) as session:
            statement = select(SourceRecord).where(
                SourceRecord.organization_id == organization_id,
                SourceRecord.connection_id == connection_id,
            )
            if record_type:
                statement = statement.where(SourceRecord.record_type == record_type)
            statement = (
                statement.order_by(SourceRecord.captured_at.desc()).limit(limit).offset(offset)
            )
            return list((await session.scalars(statement)).all())

    async def count_records(self, organization_id: uuid.UUID, connection_id: uuid.UUID) -> int:
        from sqlalchemy import func

        async with self._database.tenant_session(organization_id) as session:
            total = await session.scalar(
                select(func.count())
                .select_from(SourceRecord)
                .where(
                    SourceRecord.organization_id == organization_id,
                    SourceRecord.connection_id == connection_id,
                )
            )
        return int(total or 0)

    async def delete_records_for_connection(
        self, organization_id: uuid.UUID, connection_id: uuid.UUID
    ) -> int:
        """Delete captured records for one connection.

        Part of the tenant data-lifecycle contract: deleting a connection removes the data
        it captured. Receipts cascade.
        """
        from sqlalchemy import delete

        async with self._database.tenant_session(organization_id) as session:
            result = await session.execute(
                delete(SourceRecord).where(
                    SourceRecord.organization_id == organization_id,
                    SourceRecord.connection_id == connection_id,
                )
            )
            removed = _affected_rows(result)
        logger.info(
            "records_deleted",
            organization_id=str(organization_id),
            connection_id=str(connection_id),
            removed=removed,
        )
        return removed

    @staticmethod
    def to_public_dict(record: SourceRecord, *, max_payload_bytes: int = 16_384) -> dict[str, Any]:
        """Serialize a record for the API preview endpoint.

        The payload is truncated so a preview cannot be used to exfiltrate a large volume
        of source data in one request. Payload content is returned as data; the frontend
        must render it as text, never as HTML.
        """
        import json

        payload = record.payload or {}
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str)
        truncated = len(encoded.encode("utf-8")) > max_payload_bytes
        if truncated:
            payload = {"_truncated": True, "_preview": encoded[:max_payload_bytes]}
        return {
            "id": str(record.id),
            "connector_type": record.connector_type,
            "source_record_id": record.source_record_id,
            "record_type": record.record_type,
            "schema_version": record.schema_version,
            "source_timestamp": record.source_timestamp.isoformat()
            if record.source_timestamp
            else None,
            "captured_at": record.captured_at.isoformat(),
            "content_hash": record.content_hash,
            "payload": payload,
            "payload_truncated": truncated,
            "metadata": record.record_metadata or {},
            "untrusted": True,
        }
