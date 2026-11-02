"""Layer 2 ingestion pipeline.

Receives batches from Layer 1, preserves the source payload, validates, normalizes,
deduplicates, stores the canonical record, and enqueues a downstream event — all in the
order the architecture requires:

    RECEIVE → VALIDATE → PRESERVE → NORMALIZE → DEDUPLICATE → STORE → OUTBOX

Design commitments encoded here:

* **Per-record isolation.** One bad record produces a quarantine row, never a failed batch.
* **Source preservation first.** The raw payload is written before normalization, so a
  normalization bug cannot lose the source data.
* **Idempotency is database-enforced.** Deduplication relies on a unique constraint, not on
  a read-then-write race, so concurrent redeliveries cannot create duplicates.
* **Publication is transactional.** The outbox row is written in the same transaction as the
  canonical record, so a downstream event exists if and only if the record does.
* **Bounded work.** Batches are processed in bounded chunks; nothing is buffered without
  limit.

The service is storage- and queue-agnostic: it takes a :class:`Database` and the settings,
and returns a structured :class:`IngestResult`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from humisense.config import Settings
from humisense.db.models import (
    CanonicalRecord,
    IngestBatch,
    IngestBatchStatus,
    OutboxEvent,
    OutboxStatus,
    QuarantineStatus,
    RawRecord,
    RecordProcessingEvent,
    RecordQuarantine,
    RecordState,
)
from humisense.db.session import Database
from humisense.layer2.contract import (
    CANONICAL_SCHEMA_VERSION,
    PROCESSING_VERSION,
)
from humisense.layer2.errors import ErrorCode, Stage, StageError
from humisense.layer2.normalizers.base import (
    NormalizationInput,
    NormalizedResult,
    NormalizerRegistry,
    build_default_registry,
)
from humisense.layer2.states import ProcessingState
from humisense.layer2.validation import infer_family, validate_record
from humisense.observability.metrics import Metrics
from humisense.security.crypto import content_hash

logger = structlog.get_logger(__name__)

MAX_PREVIEW_BYTES = 2048
"""Quarantine keeps only a small preview of a payload, never the whole record."""


@dataclass(slots=True)
class IngestRecord:
    """One record as received from Layer 1."""

    connection_id: uuid.UUID
    connector_type: str
    source_record_id: str
    record_type: str
    source_timestamp: datetime | None
    observed_timestamp: datetime
    payload: dict[str, Any]
    metadata: dict[str, Any] = field(default_factory=dict)
    source_schema_version: str = "1.0"
    content_hash: str | None = None
    source_record_pk: uuid.UUID | None = None
    """The Layer 1 ``source_records`` row id, when the delivery is in-process."""

    def resolved_hash(self) -> str:
        return self.content_hash or content_hash(self.payload)


@dataclass(slots=True)
class RecordOutcome:
    """What happened to one record."""

    source_record_id: str
    state: str
    error_code: str | None = None


@dataclass(slots=True)
class IngestResult:
    """The result of one batch."""

    batch_id: str
    accepted: int = 0
    duplicates: int = 0
    quarantined: int = 0
    failed: int = 0
    outcomes: list[RecordOutcome] = field(default_factory=list)


def _truncate_preview(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a small, safe preview of a payload for the quarantine table."""
    import json

    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str)
    if len(encoded.encode("utf-8")) <= MAX_PREVIEW_BYTES:
        return {"preview": payload}
    return {"preview_truncated": True, "preview": encoded[:MAX_PREVIEW_BYTES]}


class IngestionPipeline:
    """Runs the Layer 2 pipeline for received batches."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        registry: NormalizerRegistry | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self._database = database
        self._settings = settings
        self._registry = registry or build_default_registry()
        self._metrics = metrics

    async def ingest_batch(
        self,
        *,
        organization_id: uuid.UUID,
        batch_id: str,
        correlation_id: str,
        records: list[IngestRecord],
        producer: str | None = None,
    ) -> IngestResult:
        """Process a batch, isolating per-record failures.

        The batch row is created first (idempotently, on ``(org, batch_id)``), so a
        redelivered batch is recognized even before its records are examined.
        """
        import time

        started = time.perf_counter()
        result = IngestResult(batch_id=batch_id)

        existing = await self._existing_batch(organization_id, batch_id)
        if existing is not None:
            logger.info(
                "ingest_batch_redelivered",
                batch_id=batch_id,
                organization_id=str(organization_id),
                correlation_id=correlation_id,
            )
            # The batch was already processed; report its stored counters rather than
            # reprocessing. Records are still deduplicated individually below, so a partial
            # redelivery is handled correctly.
            result.accepted = existing.records_accepted
            result.duplicates = existing.records_duplicate
            result.quarantined = existing.records_quarantined
            result.failed = existing.records_failed

        batch_row_id = await self._ensure_batch(
            organization_id=organization_id,
            batch_id=batch_id,
            correlation_id=correlation_id,
            producer=producer,
            received=len(records),
        )

        for record in records:
            outcome = await self._process_record(
                organization_id=organization_id,
                batch_row_id=batch_row_id,
                correlation_id=correlation_id,
                record=record,
            )
            result.outcomes.append(outcome)
            if outcome.state == RecordState.DUPLICATE.value:
                result.duplicates += 1
            elif outcome.state == RecordState.QUARANTINED.value:
                result.quarantined += 1
            elif outcome.state == RecordState.FAILED.value:
                result.failed += 1
            else:
                result.accepted += 1

        await self._finalize_batch(
            organization_id=organization_id,
            batch_row_id=batch_row_id,
            received=len(records),
            accepted=result.accepted,
            duplicates=result.duplicates,
            quarantined=result.quarantined,
            failed=result.failed,
        )
        if self._metrics:
            self._metrics.layer2_batch_processed(duration_seconds=time.perf_counter() - started)
        return result

    # --- Batch bookkeeping -------------------------------------------------

    async def _existing_batch(
        self, organization_id: uuid.UUID, batch_id: str
    ) -> IngestBatch | None:
        async with self._database.tenant_session(organization_id) as session:
            row = await session.scalar(
                select(IngestBatch).where(
                    IngestBatch.organization_id == organization_id,
                    IngestBatch.batch_id == batch_id,
                )
            )
        return row

    async def _ensure_batch(
        self,
        *,
        organization_id: uuid.UUID,
        batch_id: str,
        correlation_id: str,
        producer: str | None,
        received: int,
    ) -> uuid.UUID:
        async with self._database.tenant_session(organization_id) as session:
            statement = (
                pg_insert(IngestBatch)
                .values(
                    id=uuid.uuid4(),
                    organization_id=organization_id,
                    batch_id=batch_id,
                    correlation_id=correlation_id,
                    producer=producer,
                    status=IngestBatchStatus.PROCESSING,
                    records_received=received,
                )
                .on_conflict_do_nothing(constraint="uq_ingest_batches_org_batch")
                .returning(IngestBatch.id)
            )
            row = (await session.execute(statement)).first()
            if row is not None:
                return uuid.UUID(str(row[0]))
            existing_id = await session.scalar(
                select(IngestBatch.id).where(
                    IngestBatch.organization_id == organization_id,
                    IngestBatch.batch_id == batch_id,
                )
            )
            return uuid.UUID(str(existing_id))

    async def _finalize_batch(
        self,
        *,
        organization_id: uuid.UUID,
        batch_row_id: uuid.UUID,
        received: int,
        accepted: int,
        duplicates: int,
        quarantined: int,
        failed: int,
    ) -> None:
        status = IngestBatchStatus.PARTIAL if quarantined or failed else IngestBatchStatus.COMPLETED
        async with self._database.tenant_session(organization_id) as session:
            batch = await session.scalar(select(IngestBatch).where(IngestBatch.id == batch_row_id))
            if batch is None:
                return
            batch.status = status
            batch.records_received = received
            batch.records_accepted = accepted
            batch.records_duplicate = duplicates
            batch.records_quarantined = quarantined
            batch.records_failed = failed
            batch.completed_at = datetime.now(UTC)

    # --- Per-record pipeline ----------------------------------------------

    async def _process_record(
        self,
        *,
        organization_id: uuid.UUID,
        batch_row_id: uuid.UUID,
        correlation_id: str,
        record: IngestRecord,
    ) -> RecordOutcome:
        """Run one record through every stage, isolating failures."""
        # 1. Preserve the raw payload first. If anything downstream fails, the source data
        #    is already safe and can be replayed.
        try:
            raw_id, is_duplicate = await self._preserve_raw(
                organization_id=organization_id,
                batch_row_id=batch_row_id,
                correlation_id=correlation_id,
                record=record,
            )
        except Exception as exc:  # noqa: BLE001 - storage faults isolate to this record
            logger.error(
                "layer2_preserve_failed",
                source_record_id=record.source_record_id,
                error_type=type(exc).__name__,
                correlation_id=correlation_id,
            )
            await self._quarantine(
                organization_id=organization_id,
                batch_row_id=batch_row_id,
                connection_id=record.connection_id,
                correlation_id=correlation_id,
                error=StageError(
                    stage=Stage.STORAGE,
                    code=ErrorCode.STORAGE_FAILED,
                    detail="The source payload could not be preserved",
                ),
                source_record_key=record.source_record_id,
                payload=record.payload,
            )
            return RecordOutcome(
                record.source_record_id, RecordState.FAILED.value, ErrorCode.STORAGE_FAILED.value
            )

        if is_duplicate:
            # An exact redelivery. Terminal, and not a failure.
            if self._metrics:
                self._metrics.layer2_duplicate()
            await self._ledger(
                organization_id=organization_id,
                correlation_id=correlation_id,
                to_state=ProcessingState.DUPLICATE,
                from_state=ProcessingState.RECEIVED,
                stage=Stage.DEDUPLICATION,
                error_code=ErrorCode.DUPLICATE_DELIVERY.value,
                canonical_record_id=None,
            )
            return RecordOutcome(
                record.source_record_id,
                RecordState.DUPLICATE.value,
                ErrorCode.DUPLICATE_DELIVERY.value,
            )

        # 2. Validate.
        family = infer_family(record.record_type, record.payload)
        validation = validate_record(
            payload=record.payload, family=family, source_timestamp=record.source_timestamp
        )
        if not validation.ok:
            if self._metrics:
                self._metrics.layer2_validation_failed(reason=validation.errors[0].code.value)
            await self._quarantine(
                organization_id=organization_id,
                batch_row_id=batch_row_id,
                connection_id=record.connection_id,
                correlation_id=correlation_id,
                error=validation.errors[0],
                source_record_key=record.source_record_id,
                payload=record.payload,
                raw_record_id=raw_id,
            )
            return RecordOutcome(
                record.source_record_id,
                RecordState.QUARANTINED.value,
                validation.errors[0].code.value,
            )

        # 3. Normalize.
        normalizer = self._registry.find(record.record_type, record.source_schema_version)
        if normalizer is None:
            await self._quarantine(
                organization_id=organization_id,
                batch_row_id=batch_row_id,
                connection_id=record.connection_id,
                correlation_id=correlation_id,
                error=StageError(
                    stage=Stage.NORMALIZATION,
                    code=ErrorCode.NO_NORMALIZER,
                    detail="No normalizer supports this record type",
                ),
                source_record_key=record.source_record_id,
                payload=record.payload,
                raw_record_id=raw_id,
            )
            return RecordOutcome(
                record.source_record_id,
                RecordState.QUARANTINED.value,
                ErrorCode.NO_NORMALIZER.value,
            )

        try:
            normalized = normalizer.normalize(
                NormalizationInput(
                    record_type=record.record_type,
                    source_schema_version=record.source_schema_version,
                    payload=record.payload,
                    metadata=record.metadata,
                    source_timestamp=validation.source_timestamp,
                )
            )
        except Exception as exc:  # noqa: BLE001 - a normalizer bug isolates to this record
            logger.error(
                "layer2_normalizer_crashed",
                normalizer=normalizer.name,
                error_type=type(exc).__name__,
                correlation_id=correlation_id,
            )
            await self._quarantine(
                organization_id=organization_id,
                batch_row_id=batch_row_id,
                connection_id=record.connection_id,
                correlation_id=correlation_id,
                error=StageError(
                    stage=Stage.NORMALIZATION,
                    code=ErrorCode.NORMALIZER_FAILED,
                    detail="The normalizer raised an unexpected error",
                ),
                source_record_key=record.source_record_id,
                payload=record.payload,
                raw_record_id=raw_id,
            )
            return RecordOutcome(
                record.source_record_id,
                RecordState.QUARANTINED.value,
                ErrorCode.NORMALIZER_FAILED.value,
            )

        if isinstance(normalized, list):
            first = (
                normalized[0]
                if normalized
                else StageError(
                    stage=Stage.NORMALIZATION,
                    code=ErrorCode.NORMALIZER_FAILED,
                    detail="The normalizer returned no result",
                )
            )
            if self._metrics:
                self._metrics.layer2_normalization_failed(reason=first.code.value)
            await self._quarantine(
                organization_id=organization_id,
                batch_row_id=batch_row_id,
                connection_id=record.connection_id,
                correlation_id=correlation_id,
                error=first,
                source_record_key=record.source_record_id,
                payload=record.payload,
                raw_record_id=raw_id,
            )
            return RecordOutcome(
                record.source_record_id, RecordState.QUARANTINED.value, first.code.value
            )

        # 4. Store the canonical record and its outbox event in one transaction.
        try:
            await self._store_canonical(
                organization_id=organization_id,
                raw_record_id=raw_id,
                correlation_id=correlation_id,
                record=record,
                normalized=normalized,
                family=family or "generic",
                normalizer_name=normalizer.name,
                normalizer_version=normalizer.version,
                validation_timezone=validation.source_timezone,
                validation_time_metadata=validation.source_time_metadata,
            )
        except Exception as exc:  # noqa: BLE001 - storage faults isolate to this record
            logger.error(
                "layer2_store_failed",
                source_record_id=record.source_record_id,
                error_type=type(exc).__name__,
                correlation_id=correlation_id,
            )
            await self._quarantine(
                organization_id=organization_id,
                batch_row_id=batch_row_id,
                connection_id=record.connection_id,
                correlation_id=correlation_id,
                error=StageError(
                    stage=Stage.STORAGE,
                    code=ErrorCode.STORAGE_FAILED,
                    detail="The canonical record could not be stored",
                ),
                source_record_key=record.source_record_id,
                payload=record.payload,
                raw_record_id=raw_id,
            )
            return RecordOutcome(
                record.source_record_id, RecordState.FAILED.value, ErrorCode.STORAGE_FAILED.value
            )

        if self._metrics:
            self._metrics.layer2_stored(data_family=family or "generic")
        return RecordOutcome(record.source_record_id, RecordState.STORED.value)

    # --- Stages ------------------------------------------------------------

    async def _preserve_raw(
        self,
        *,
        organization_id: uuid.UUID,
        batch_row_id: uuid.UUID,
        correlation_id: str,
        record: IngestRecord,
    ) -> tuple[uuid.UUID, bool]:
        """Insert the raw record. Returns ``(id, is_duplicate)``.

        Deduplication is delegated to the unique constraint: a conflicting insert is a
        duplicate, so two concurrent redeliveries cannot both insert.
        """
        digest = record.resolved_hash()
        async with self._database.tenant_session(organization_id) as session:
            statement = (
                pg_insert(RawRecord)
                .values(
                    id=uuid.uuid4(),
                    organization_id=organization_id,
                    connection_id=record.connection_id,
                    source_record_id=record.source_record_pk,
                    batch_row_id=batch_row_id,
                    connector_type=record.connector_type,
                    source_record_key=record.source_record_id,
                    record_type=record.record_type,
                    source_schema_version=record.source_schema_version,
                    idempotency_key=f"{record.connection_id}:{record.source_record_id}:{digest}",
                    source_timestamp=record.source_timestamp,
                    observed_timestamp=record.observed_timestamp,
                    payload=record.payload,
                    record_metadata={**record.metadata, "untrusted": True},
                    content_hash=digest,
                    correlation_id=correlation_id,
                    untrusted=True,
                )
                .on_conflict_do_nothing(constraint="uq_raw_records_identity")
                .returning(RawRecord.id)
            )
            row = (await session.execute(statement)).first()
            if row is not None:
                return uuid.UUID(str(row[0])), False
            existing = await session.scalar(
                select(RawRecord.id).where(
                    RawRecord.organization_id == organization_id,
                    RawRecord.connection_id == record.connection_id,
                    RawRecord.source_record_key == record.source_record_id,
                    RawRecord.content_hash == digest,
                )
            )
            return uuid.UUID(str(existing)), True

    async def _store_canonical(
        self,
        *,
        organization_id: uuid.UUID,
        raw_record_id: uuid.UUID,
        correlation_id: str,
        record: IngestRecord,
        normalized: NormalizedResult,
        family: str,
        normalizer_name: str,
        normalizer_version: str,
        validation_timezone: str | None,
        validation_time_metadata: dict[str, Any],
    ) -> None:
        """Write the canonical record and its outbox event atomically."""
        now = datetime.now(UTC)
        idempotency_key = (
            f"{record.connection_id}:{record.source_record_id}:{record.resolved_hash()}"
        )
        async with self._database.tenant_session(organization_id) as session:
            canonical = CanonicalRecord(
                id=uuid.uuid4(),
                organization_id=organization_id,
                raw_record_id=raw_record_id,
                connection_id=record.connection_id,
                idempotency_key=idempotency_key,
                source_record_key=record.source_record_id,
                record_type=record.record_type,
                entity_type=normalized.entity_type,
                event_type=normalized.event_type,
                data_family=family,
                source_timestamp=record.source_timestamp,
                event_timestamp=normalized.event_timestamp,
                observed_timestamp=record.observed_timestamp,
                received_timestamp=now,
                processed_timestamp=now,
                source_timezone=validation_timezone,
                source_time_metadata=validation_time_metadata,
                normalized_payload=normalized.normalized_payload,
                extensions=normalized.extensions,
                record_metadata={**normalized.metadata, "untrusted": True},
                unit_conversions=normalized.unit_conversions,
                quality_code=normalized.quality_code,
                schema_version=CANONICAL_SCHEMA_VERSION,
                source_schema_version=record.source_schema_version,
                processing_version=PROCESSING_VERSION,
                normalizer=normalizer_name,
                normalizer_version=normalizer_version,
                processing_state=RecordState.STORED,
                correlation_id=correlation_id,
            )
            session.add(canonical)
            await session.flush()

            session.add(
                OutboxEvent(
                    id=uuid.uuid4(),
                    organization_id=organization_id,
                    canonical_record_id=canonical.id,
                    event_type="record.normalized",
                    payload={
                        "canonical_record_id": str(canonical.id),
                        "record_type": record.record_type,
                        "data_family": family,
                        "idempotency_key": idempotency_key,
                    },
                    status=OutboxStatus.PENDING,
                    correlation_id=correlation_id,
                )
            )
            session.add(
                RecordProcessingEvent(
                    id=uuid.uuid4(),
                    organization_id=organization_id,
                    canonical_record_id=canonical.id,
                    from_state=ProcessingState.RECEIVED.value,
                    to_state=ProcessingState.STORED.value,
                    stage=Stage.STORAGE.value,
                    correlation_id=correlation_id,
                )
            )

    async def _quarantine(
        self,
        *,
        organization_id: uuid.UUID,
        batch_row_id: uuid.UUID,
        connection_id: uuid.UUID,
        correlation_id: str,
        error: StageError,
        source_record_key: str,
        payload: dict[str, Any],
        raw_record_id: uuid.UUID | None = None,
    ) -> None:
        """Record a quarantined record with a bounded preview and safe detail."""
        async with self._database.tenant_session(organization_id) as session:
            session.add(
                RecordQuarantine(
                    id=uuid.uuid4(),
                    organization_id=organization_id,
                    raw_record_id=raw_record_id,
                    batch_row_id=batch_row_id,
                    connection_id=connection_id,
                    source_record_key=source_record_key,
                    stage=error.stage.value,
                    error_code=error.code.value,
                    detail=error.detail,
                    payload_preview=_truncate_preview(payload),
                    status=QuarantineStatus.OPEN,
                    correlation_id=correlation_id,
                )
            )
            session.add(
                RecordProcessingEvent(
                    id=uuid.uuid4(),
                    organization_id=organization_id,
                    canonical_record_id=None,
                    from_state=ProcessingState.RECEIVED.value,
                    to_state=ProcessingState.QUARANTINED.value,
                    stage=error.stage.value,
                    error_code=error.code.value,
                    detail=error.detail,
                    correlation_id=correlation_id,
                )
            )
        if self._metrics:
            self._metrics.layer2_quarantined(reason=error.code.value)

    async def _ledger(
        self,
        *,
        organization_id: uuid.UUID,
        correlation_id: str,
        to_state: ProcessingState,
        from_state: ProcessingState | None,
        stage: Stage,
        error_code: str | None,
        canonical_record_id: uuid.UUID | None,
    ) -> None:
        async with self._database.tenant_session(organization_id) as session:
            session.add(
                RecordProcessingEvent(
                    id=uuid.uuid4(),
                    organization_id=organization_id,
                    canonical_record_id=canonical_record_id,
                    from_state=from_state.value if from_state else None,
                    to_state=to_state.value,
                    stage=stage.value,
                    error_code=error_code,
                    correlation_id=correlation_id,
                )
            )
