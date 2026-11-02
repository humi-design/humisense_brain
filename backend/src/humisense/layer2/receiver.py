"""Ingestion receiver.

The boundary between Layer 1 and Layer 2. It authenticates the caller, validates the batch
envelope shape, converts each record into an :class:`IngestRecord`, and hands the batch to
the pipeline. It performs no normalization itself.

Security properties:

* **Service authentication.** The caller presents a bearer token that is compared in
  constant time. A missing or wrong token is refused before any record is parsed. There is
  no path that accepts an unauthenticated batch.
* **Tenant from the envelope, cross-checked against the connection.** The batch's
  ``organization_id`` must match the connection's tenant; a mismatch is refused, so a
  compromised producer cannot write into another tenant's pipeline.
* **Bounded input.** Batch size and per-record payload size are bounded before storage.
* **Content is data.** Every record is marked untrusted. Nothing here executes, interprets,
  or forwards payload content as an instruction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import select

from humisense.config import Settings
from humisense.db.models import Connection
from humisense.db.session import Database
from humisense.errors import AuthenticationError, ValidationFailure
from humisense.layer2.errors import ErrorCode
from humisense.layer2.pipeline import IngestionPipeline, IngestRecord, IngestResult
from humisense.observability.metrics import Metrics
from humisense.security.crypto import constant_time_equals

logger = structlog.get_logger(__name__)

SUPPORTED_ENVELOPE_VERSIONS = frozenset({"1.0"})
MAX_RECORDS_PER_BATCH = 200
MAX_PAYLOAD_BYTES = 1 * 1024 * 1024


@dataclass(slots=True)
class ReceivedRecord:
    """One record as it arrives on the wire."""

    connection_id: uuid.UUID
    connector_type: str
    source_record_id: str
    record_type: str
    source_timestamp: datetime | None
    observed_timestamp: datetime
    payload: dict[str, Any]
    metadata: dict[str, Any]
    source_schema_version: str
    content_hash: str | None
    source_record_pk: uuid.UUID | None


@dataclass(slots=True)
class ReceivedBatch:
    organization_id: uuid.UUID
    batch_id: str
    correlation_id: str
    records: list[ReceivedRecord]
    producer: str | None = None


class IngestionReceiver:
    """Authenticates, validates, and forwards a batch to the pipeline."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        pipeline: IngestionPipeline | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self._database = database
        self._settings = settings
        self._metrics = metrics
        self._pipeline = pipeline or IngestionPipeline(database, settings, metrics=metrics)

    def authenticate(self, presented_token: str | None) -> None:
        """Verify the service token, refusing anything that does not match.

        A deployment with no configured ingest token refuses every request rather than
        accepting unauthenticated writes: failing closed is the only safe default here.
        """
        expected = self._settings.layer2_ingest_token_resolved()
        if not expected:
            raise AuthenticationError(detail="The ingestion receiver has no configured token")
        if not presented_token:
            raise AuthenticationError(detail="No service token was presented")
        if not constant_time_equals(expected, presented_token):
            raise AuthenticationError(detail="The service token did not match")

    async def receive(self, batch: ReceivedBatch) -> IngestResult:
        """Validate the batch and run it through the pipeline."""
        if not batch.records:
            raise ValidationFailure(detail="An ingestion batch must contain at least one record")
        if len(batch.records) > MAX_RECORDS_PER_BATCH:
            raise ValidationFailure(
                detail="The ingestion batch exceeds the maximum record count",
                context={"max_records": MAX_RECORDS_PER_BATCH},
            )

        # Every record must belong to the same connection, and that connection must belong to
        # the batch's tenant. This is the check that prevents a producer from writing into a
        # tenant it does not own.
        connection_ids = {record.connection_id for record in batch.records}
        if len(connection_ids) != 1:
            raise ValidationFailure(detail="A batch must reference exactly one connection")
        connection_id = next(iter(connection_ids))
        await self._assert_connection_owned(batch.organization_id, connection_id)

        ingest_records: list[IngestRecord] = []
        for record in batch.records:
            if record.source_schema_version not in SUPPORTED_ENVELOPE_VERSIONS:
                raise ValidationFailure(
                    detail="The envelope schema version is not supported",
                    context={"schema_version": record.source_schema_version},
                )
            ingest_records.append(
                IngestRecord(
                    connection_id=record.connection_id,
                    connector_type=record.connector_type,
                    source_record_id=record.source_record_id,
                    record_type=record.record_type,
                    source_timestamp=record.source_timestamp,
                    observed_timestamp=record.observed_timestamp,
                    payload=record.payload,
                    metadata=record.metadata,
                    source_schema_version=record.source_schema_version,
                    content_hash=record.content_hash,
                    source_record_pk=record.source_record_pk,
                )
            )

        if self._metrics:
            self._metrics.layer2_received(count=len(ingest_records))

        return await self._pipeline.ingest_batch(
            organization_id=batch.organization_id,
            batch_id=batch.batch_id,
            correlation_id=batch.correlation_id,
            records=ingest_records,
            producer=batch.producer,
        )

    async def _assert_connection_owned(
        self, organization_id: uuid.UUID, connection_id: uuid.UUID
    ) -> None:
        async with self._database.tenant_session(organization_id) as session:
            found = await session.scalar(
                select(Connection.id).where(
                    Connection.organization_id == organization_id,
                    Connection.id == connection_id,
                )
            )
        if found is None:
            # Not "forbidden": the connection simply does not exist for this tenant.
            raise ValidationFailure(
                detail="The connection does not belong to this organization",
                context={"error_code": ErrorCode.CONNECTION_OWNERSHIP_MISMATCH.value},
            )
