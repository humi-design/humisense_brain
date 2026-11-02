"""Quarantine inspection and replay.

A quarantined record is not lost: the raw payload is preserved, and an operator can see why
it was quarantined and replay it once the cause is fixed (a normalizer was added, a unit rule
was corrected). Replay re-runs the pipeline over the *preserved* payload, so it never depends
on the source being reachable again.

Replay is bounded and idempotent:

* The raw payload is the input, so replaying twice converges: the canonical record's unique
  key makes the second attempt a duplicate rather than a second record.
* ``replay_count`` is incremented, so a record stuck in a replay loop is visible.
* Replay is tenant-scoped and audited.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import select

from humisense.config import Settings
from humisense.db.models import (
    QuarantineStatus,
    RawRecord,
    RecordQuarantine,
    RecordState,
)
from humisense.db.session import Database
from humisense.errors import NotFoundError
from humisense.layer2.pipeline import IngestionPipeline, IngestRecord
from humisense.observability.metrics import Metrics

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class QuarantineItem:
    """A quarantine row projected for an operator."""

    quarantine_id: uuid.UUID
    stage: str
    error_code: str
    source_record_key: str | None
    status: str
    replay_count: int
    created_at: datetime


@dataclass(slots=True)
class ReplayOutcome:
    quarantine_id: uuid.UUID
    replayed: bool
    state: str
    error_code: str | None = None


class QuarantineService:
    """Lists quarantined records and replays them."""

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

    async def list_open(
        self, organization_id: uuid.UUID, *, limit: int = 100
    ) -> list[QuarantineItem]:
        async with self._database.tenant_session(organization_id) as session:
            rows = (
                await session.scalars(
                    select(RecordQuarantine)
                    .where(
                        RecordQuarantine.organization_id == organization_id,
                        RecordQuarantine.status == QuarantineStatus.OPEN,
                    )
                    .order_by(RecordQuarantine.created_at.desc())
                    .limit(limit)
                )
            ).all()
            return [
                QuarantineItem(
                    quarantine_id=row.id,
                    stage=row.stage,
                    error_code=row.error_code,
                    source_record_key=row.source_record_key,
                    status=row.status.value,
                    replay_count=row.replay_count,
                    created_at=row.created_at,
                )
                for row in rows
            ]

    async def replay(self, organization_id: uuid.UUID, quarantine_id: uuid.UUID) -> ReplayOutcome:
        """Re-run the pipeline over a quarantined record's preserved payload."""
        async with self._database.tenant_session(organization_id) as session:
            quarantine = await session.scalar(
                select(RecordQuarantine).where(
                    RecordQuarantine.organization_id == organization_id,
                    RecordQuarantine.id == quarantine_id,
                )
            )
            if quarantine is None:
                # A quarantine id from another tenant is indistinguishable from one that does
                # not exist, and the response must be the same for both: a distinct "belongs
                # to someone else" answer would leak the existence of another tenant's data.
                raise NotFoundError(detail="The quarantine record was not found")
            if quarantine.raw_record_id is None:
                # Nothing preserved to replay — the failure happened before preservation.
                return ReplayOutcome(quarantine_id, replayed=False, state="unreplayable")
            raw = await session.scalar(
                select(RawRecord).where(
                    RawRecord.organization_id == organization_id,
                    RawRecord.id == quarantine.raw_record_id,
                )
            )
            if raw is None:
                return ReplayOutcome(quarantine_id, replayed=False, state="unreplayable")
            record = IngestRecord(
                connection_id=raw.connection_id,
                connector_type=raw.connector_type,
                source_record_id=raw.source_record_key,
                record_type=raw.record_type,
                source_timestamp=raw.source_timestamp,
                observed_timestamp=raw.observed_timestamp,
                payload=raw.payload,
                metadata={k: v for k, v in raw.record_metadata.items() if k != "untrusted"},
                source_schema_version=raw.source_schema_version,
                content_hash=raw.content_hash,
                source_record_pk=raw.source_record_id,
            )
            quarantine.replay_count += 1
            quarantine.status = QuarantineStatus.REPLAYED
            quarantine.resolved_at = datetime.now(UTC)

        result = await self._pipeline.ingest_batch(
            organization_id=organization_id,
            batch_id=f"replay-{quarantine_id}",
            correlation_id=f"replay-{quarantine_id}",
            records=[record],
        )
        outcome = result.outcomes[0] if result.outcomes else None
        state = outcome.state if outcome else RecordState.FAILED.value
        error_code = outcome.error_code if outcome else None
        logger.info(
            "layer2_quarantine_replayed",
            quarantine_id=str(quarantine_id),
            state=state,
            organization_id=str(organization_id),
        )
        return ReplayOutcome(quarantine_id, replayed=True, state=state, error_code=error_code)
