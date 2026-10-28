"""Synchronization service.

One synchronization is a bounded unit of capture work:

1. Re-establish tenant context and re-authorize the operation. The worker is not trusted
   just because it holds a job id.
2. Resolve the connection's credentials through the secrets provider.
3. Page through the connector under resource limits and a cancellation token.
4. Persist each page idempotently.
5. Deliver envelopes to Layer 2 in bounded batches.
6. Advance the checkpoint only when the page was persisted and delivered.
7. Record counters, the audit trail, and metrics.

Failure semantics
-----------------
A page that fails permanently ends the run with ``failed`` and does not advance the
checkpoint, so the next run re-reads the same range. A page that partially succeeds ends
the run as ``partial`` and still advances the checkpoint, because the records that were
captured are persisted and the failures are reported. Delivery failures are recorded on
the receipt rather than discarding the records, so no captured data is silently lost.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

import structlog
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult

from humisense.config import Settings
from humisense.connectors.types import (
    Cursor,
    PartialFailure,
)
from humisense.db.models import (
    Connection,
    IngestionReceipt,
    ReceiptStatus,
    SyncJob,
    SyncStatus,
)
from humisense.db.session import Database
from humisense.errors import (
    ConnectorError,
    HumisenseError,
    NotFoundError,
    SyncCancelledError,
    is_retryable,
)
from humisense.observability.metrics import Metrics
from humisense.services.audit import (
    OUTCOME_FAILURE,
    OUTCOME_SUCCESS,
    AuditAction,
    AuditActor,
    AuditEntry,
    AuditService,
)
from humisense.services.ingestion import IngestionService
from humisense.services.records import RecordService
from humisense.services.runtime import ConnectorRuntime

logger = structlog.get_logger(__name__)

MAX_PAGES_PER_RUN = 10_000
"""Safety valve: a connector that never clears ``has_more`` cannot loop forever."""


class DbCancellationToken:
    """Cancellation token backed by the ``sync_jobs.cancel_requested`` flag.

    The flag is polled at a bounded interval rather than on every record, so a large page
    does not turn into one query per row. Polling is deliberately separate from the
    worker's own connection so a long transaction does not hide a cancellation request.
    """

    def __init__(
        self,
        database: Database,
        *,
        organization_id: uuid.UUID,
        job_id: uuid.UUID,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        self._database = database
        self._organization_id = organization_id
        self._job_id = job_id
        self._poll_interval = poll_interval_seconds
        self._last_poll = 0.0
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise SyncCancelledError(detail="Cancellation was requested for this job")

    async def poll(self) -> None:
        now = time.monotonic()
        if now - self._last_poll < self._poll_interval:
            self.raise_if_cancelled()
            return
        self._last_poll = now
        async with self._database.tenant_session(self._organization_id) as session:
            flag = await session.scalar(
                select(SyncJob.cancel_requested).where(
                    SyncJob.organization_id == self._organization_id,
                    SyncJob.id == self._job_id,
                )
            )
        if flag:
            self._cancelled = True
        self.raise_if_cancelled()


@dataclass(slots=True)
class SyncOutcome:
    """Result of one synchronization run."""

    job_id: uuid.UUID
    status: SyncStatus
    records_fetched: int = 0
    records_persisted: int = 0
    records_delivered: int = 0
    records_failed: int = 0
    error_code: str | None = None
    error_message: str | None = None
    partial_failures: list[PartialFailure] = field(default_factory=list)
    pages: int = 0
    duration_ms: float = 0.0
    retryable: bool = False


class SyncService:
    """Runs synchronization jobs."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        audit: AuditService,
        runtime: ConnectorRuntime,
        records: RecordService,
        ingestion: IngestionService,
        metrics: Metrics | None = None,
    ) -> None:
        self._database = database
        self._settings = settings
        self._audit = audit
        self._runtime = runtime
        self._records = records
        self._ingestion = ingestion
        self._metrics = metrics

    # --- Job lifecycle ----------------------------------------------------

    async def create_job(
        self,
        *,
        organization_id: uuid.UUID,
        connection_id: uuid.UUID,
        triggered_by: uuid.UUID | None,
        correlation_id: str,
    ) -> SyncJob:
        """Create a queued job after checking connection state and tenant concurrency."""
        async with self._database.tenant_session(organization_id) as session:
            connection = await session.scalar(
                select(Connection).where(
                    Connection.organization_id == organization_id,
                    Connection.id == connection_id,
                )
            )
            if connection is None:
                raise NotFoundError(detail="Connection not found for this organization")
            if not connection.enabled:
                from humisense.errors import ConflictError

                raise ConflictError(detail="Connection is disabled")

            active = await session.scalar(
                select(SyncJob.id).where(
                    SyncJob.organization_id == organization_id,
                    SyncJob.connection_id == connection_id,
                    SyncJob.status.in_([SyncStatus.QUEUED, SyncStatus.RUNNING]),
                )
            )
            if active is not None:
                from humisense.errors import ConflictError

                raise ConflictError(
                    detail="A synchronization is already queued or running for this connection"
                )

            # Per-tenant resource bound. Queued work counts as well as running work, so
            # a tenant cannot queue an unbounded backlog that later saturates the worker
            # pool at the expense of other tenants.
            active_for_tenant = await session.scalar(
                select(func.count())
                .select_from(SyncJob)
                .where(
                    SyncJob.organization_id == organization_id,
                    SyncJob.status.in_([SyncStatus.QUEUED, SyncStatus.RUNNING]),
                )
            )
            if int(active_for_tenant or 0) >= self._settings.sync_max_concurrent_jobs_per_tenant:
                from humisense.errors import ConflictError

                raise ConflictError(
                    detail="The organization has reached its concurrent synchronization limit"
                )

            job = SyncJob(
                organization_id=organization_id,
                connection_id=connection_id,
                status=SyncStatus.QUEUED,
                cursor_before=dict(connection.checkpoint or {}) or None,
                correlation_id=correlation_id,
                triggered_by=triggered_by,
                max_attempts=3,
            )
            session.add(job)
            await session.flush()
            await session.refresh(job)
            return job

    async def get_job(self, organization_id: uuid.UUID, job_id: uuid.UUID) -> SyncJob:
        async with self._database.tenant_session(organization_id) as session:
            job = await session.scalar(
                select(SyncJob).where(
                    SyncJob.organization_id == organization_id, SyncJob.id == job_id
                )
            )
            if job is None:
                raise NotFoundError(detail="Synchronization job not found")
            return job

    async def list_jobs(
        self,
        organization_id: uuid.UUID,
        connection_id: uuid.UUID,
        *,
        limit: int,
        offset: int = 0,
    ) -> list[SyncJob]:
        async with self._database.tenant_session(organization_id) as session:
            statement = (
                select(SyncJob)
                .where(
                    SyncJob.organization_id == organization_id,
                    SyncJob.connection_id == connection_id,
                )
                .order_by(SyncJob.queued_at.desc())
                .limit(limit)
                .offset(offset)
            )
            return list((await session.scalars(statement)).all())

    async def request_cancel(self, organization_id: uuid.UUID, job_id: uuid.UUID) -> SyncJob:
        """Set the cancellation flag. The worker observes it at its next poll."""
        async with self._database.tenant_session(organization_id) as session:
            job = await session.scalar(
                select(SyncJob).where(
                    SyncJob.organization_id == organization_id, SyncJob.id == job_id
                )
            )
            if job is None:
                raise NotFoundError(detail="Synchronization job not found")
            if job.status in {SyncStatus.SUCCEEDED, SyncStatus.FAILED, SyncStatus.CANCELLED}:
                return job
            job.cancel_requested = True
            await session.flush()
            await session.refresh(job)
            return job

    # --- Execution --------------------------------------------------------

    async def run_job(self, organization_id: uuid.UUID, job_id: uuid.UUID) -> SyncOutcome:
        """Execute a queued job.

        Safe to call from the worker and from tests. It re-derives everything from the two
        identifiers; nothing else from the queue payload is trusted.
        """
        started = time.monotonic()

        async with self._database.tenant_session(organization_id) as session:
            job = await session.scalar(
                select(SyncJob).where(
                    SyncJob.organization_id == organization_id, SyncJob.id == job_id
                )
            )
            if job is None:
                raise NotFoundError(detail="Synchronization job not found")
            if job.status != SyncStatus.QUEUED:
                # Already running or finished; do not run twice.
                return SyncOutcome(
                    job_id=job_id,
                    status=job.status,
                    error_code="job_not_queued",
                    error_message="Job was not in the queued state",
                )
            connection = await session.scalar(
                select(Connection).where(
                    Connection.organization_id == organization_id,
                    Connection.id == job.connection_id,
                )
            )
            if connection is None:
                raise NotFoundError(detail="Connection not found for this organization")

            job.status = SyncStatus.RUNNING
            job.started_at = datetime.now(UTC)
            job.attempt += 1
            job.correlation_id = job.correlation_id or str(job_id)
            # The job's correlation id is the trace identifier for the whole run: it is
            # carried into the connector context, the stored records, and the Layer 2
            # handoff, so one identifier ties the run together end to end.
            connection_snapshot = ConnectionSnapshot.from_model(
                connection, correlation_id=job.correlation_id
            )
            await session.flush()

        correlation_id = connection_snapshot.correlation_id or str(job_id)
        actor = AuditActor.worker(organization_id, correlation_id=correlation_id)
        await self._audit.record(
            actor,
            AuditEntry(
                action=AuditAction.SYNC_STARTED,
                outcome=OUTCOME_SUCCESS,
                target_type="connection",
                target_id=str(connection_snapshot.id),
                metadata={"job_id": str(job_id), "attempt": job.attempt},
            ),
        )
        if self._metrics:
            self._metrics.sync_started(organization_id=organization_id)

        token = DbCancellationToken(self._database, organization_id=organization_id, job_id=job_id)
        outcome = SyncOutcome(job_id=job_id, status=SyncStatus.RUNNING)

        try:
            outcome = await self._execute(
                organization_id=organization_id,
                snapshot=connection_snapshot,
                job_id=job_id,
                token=token,
                outcome=outcome,
            )
        except SyncCancelledError:
            outcome.status = SyncStatus.CANCELLED
            outcome.error_code = SyncCancelledError.code
            outcome.error_message = "Cancellation was requested."
        except ConnectorError as exc:
            outcome.status = SyncStatus.FAILED
            outcome.error_code = exc.code
            outcome.error_message = exc.public_message
            outcome.retryable = is_retryable(exc.code)
        except HumisenseError as exc:
            outcome.status = SyncStatus.FAILED
            outcome.error_code = exc.code
            outcome.error_message = exc.public_message
            outcome.retryable = is_retryable(exc.code)
        except Exception as exc:  # noqa: BLE001 - a connector bug must not crash the worker
            logger.error(
                "sync_unhandled_error",
                job_id=str(job_id),
                error_type=type(exc).__name__,
                correlation_id=correlation_id,
            )
            outcome.status = SyncStatus.FAILED
            outcome.error_code = "internal_error"
            outcome.error_message = "The synchronization failed due to an internal error."
            outcome.retryable = False

        outcome.duration_ms = (time.monotonic() - started) * 1000
        await self._finalize(organization_id, job_id, outcome, actor)
        return outcome

    async def _execute(
        self,
        *,
        organization_id: uuid.UUID,
        snapshot: ConnectionSnapshot,
        job_id: uuid.UUID,
        token: DbCancellationToken,
        outcome: SyncOutcome,
    ) -> SyncOutcome:
        connection = await self._runtime_load(organization_id, snapshot.id)
        connector = await self._runtime.build_connector(
            connection,
            correlation_id=snapshot.correlation_id or str(job_id),
            cancellation=token,
        )
        # The token must be reachable from the connector's checks as well.
        connector.context.cancellation = token

        cursor = self._coerce_cursor(snapshot.checkpoint)
        try:
            pages = 0
            while True:
                await token.poll()
                if pages >= MAX_PAGES_PER_RUN:
                    logger.warning("sync_page_limit_reached", job_id=str(job_id))
                    break

                page = await connector.fetch_records(cursor, self._settings.sync_batch_size)
                pages += 1
                outcome.pages = pages
                outcome.records_fetched += len(page.records) + len(page.partial_failures)
                outcome.partial_failures.extend(page.partial_failures)
                outcome.records_failed += len(page.partial_failures)

                if page.records:
                    persisted = await self._records.persist_batch(
                        organization_id=organization_id,
                        connection_id=snapshot.id,
                        connector_type=snapshot.connector_type,
                        correlation_id=snapshot.correlation_id or str(job_id),
                        drafts=page.records,
                        sync_job_id=job_id,
                    )
                    outcome.records_persisted += len(persisted)

                    envelopes = [record.envelope for record in persisted]
                    delivery = await self._ingestion.deliver_envelopes(
                        envelopes, correlation_id=snapshot.correlation_id or str(job_id)
                    )
                    outcome.records_delivered += delivery.delivered
                    if delivery.failed:
                        outcome.records_failed += delivery.failed
                    await self._record_receipts(
                        organization_id=organization_id,
                        persisted=persisted,
                        job_id=job_id,
                        delivered=delivery.delivered,
                        failed=delivery.failed,
                        failed_keys=set(delivery.failed_keys),
                        error_code=delivery.error_code,
                    )
                    if self._metrics:
                        self._metrics.records_ingested(
                            organization_id=organization_id, count=delivery.delivered
                        )

                if not page.has_more or page.next_cursor is None:
                    cursor = self._coerce_cursor(page.next_cursor) if page.next_cursor else cursor
                    break
                cursor = self._coerce_cursor(page.next_cursor)
                # Advance the checkpoint after each completed page so a later failure
                # resumes rather than re-scanning from the beginning.
                await self._persist_checkpoint(organization_id, job_id, page.next_cursor, outcome)
        finally:
            await connector.close()

        if outcome.records_failed and outcome.records_delivered:
            outcome.status = SyncStatus.PARTIAL
        elif outcome.records_failed:
            outcome.status = SyncStatus.PARTIAL if outcome.records_persisted else SyncStatus.FAILED
            if outcome.status == SyncStatus.FAILED:
                outcome.error_code = "no_records_captured"
                outcome.error_message = "No records could be captured in this run."
        else:
            outcome.status = SyncStatus.SUCCEEDED

        await self._persist_checkpoint(
            organization_id, job_id, cursor.model_dump(mode="json") if cursor else None, outcome
        )
        return outcome

    async def _record_receipts(
        self,
        *,
        organization_id: uuid.UUID,
        persisted: list[Any],
        job_id: uuid.UUID,
        delivered: int,
        failed: int,
        failed_keys: set[str],
        error_code: str | None,
    ) -> None:
        """Write one ingestion receipt per persisted record.

        A record is identified by ``source_record_id``, which is derived from the source
        content rather than from the run, so a re-run that re-reads the same rows would
        otherwise collide on the unique constraint and fail the whole job. The insert is
        therefore conflict-tolerant: an existing receipt means the record was already
        accepted by Layer 2 and needs no second acknowledgement.
        """
        if not persisted:
            return
        rows = []
        for record in persisted:
            key = record.envelope.idempotency_key()
            status = (
                ReceiptStatus.FAILED
                if key in failed_keys or (failed and delivered == 0)
                else ReceiptStatus.ACCEPTED
            )
            rows.append(
                {
                    "organization_id": organization_id,
                    "source_record_id": record.id,
                    "sync_job_id": job_id,
                    "status": status,
                    "idempotency_key": key,
                    "attempts": 1,
                    "error_code": error_code if status == ReceiptStatus.FAILED else None,
                    "accepted_at": datetime.now(UTC) if status == ReceiptStatus.ACCEPTED else None,
                }
            )
        async with self._database.tenant_session(organization_id) as session:
            await session.execute(
                pg_insert(IngestionReceipt)
                .values(rows)
                .on_conflict_do_nothing(constraint="uq_ingestion_receipts_source_record_id")
            )

    async def _persist_checkpoint(
        self,
        organization_id: uuid.UUID,
        job_id: uuid.UUID,
        checkpoint: dict[str, Any] | None,
        outcome: SyncOutcome,
    ) -> None:
        async with self._database.tenant_session(organization_id) as session:
            job = await session.scalar(
                select(SyncJob).where(
                    SyncJob.organization_id == organization_id, SyncJob.id == job_id
                )
            )
            if job is None:
                return
            job.cursor_after = checkpoint
            job.records_fetched = outcome.records_fetched
            job.records_persisted = outcome.records_persisted
            job.records_delivered = outcome.records_delivered
            job.records_failed = outcome.records_failed
            if checkpoint is not None:
                # The connection carries the durable checkpoint, which is what the next
                # run reads as its starting cursor. Writing it only on the job row would
                # record the position without ever resuming from it, so every run would
                # re-scan from the beginning.
                connection = await session.scalar(
                    select(Connection).where(
                        Connection.organization_id == organization_id,
                        Connection.id == job.connection_id,
                    )
                )
                if connection is not None:
                    connection.checkpoint = checkpoint
            await session.flush()

    async def _finalize(
        self,
        organization_id: uuid.UUID,
        job_id: uuid.UUID,
        outcome: SyncOutcome,
        actor: AuditActor,
    ) -> None:
        async with self._database.tenant_session(organization_id) as session:
            job = await session.scalar(
                select(SyncJob).where(
                    SyncJob.organization_id == organization_id, SyncJob.id == job_id
                )
            )
            if job is None:
                return
            job.status = outcome.status
            job.finished_at = datetime.now(UTC)
            job.duration_ms = outcome.duration_ms
            job.records_fetched = outcome.records_fetched
            job.records_persisted = outcome.records_persisted
            job.records_delivered = outcome.records_delivered
            job.records_failed = outcome.records_failed
            job.error_code = outcome.error_code
            job.error_message = outcome.error_message
            if outcome.status in {SyncStatus.SUCCEEDED, SyncStatus.PARTIAL}:
                job.cursor_after = job.cursor_after or job.cursor_before
            await session.flush()
            connection_id = job.connection_id

        action = {
            SyncStatus.SUCCEEDED: AuditAction.SYNC_SUCCEEDED,
            SyncStatus.PARTIAL: AuditAction.SYNC_PARTIAL,
            SyncStatus.CANCELLED: AuditAction.SYNC_CANCELLED,
        }.get(outcome.status, AuditAction.SYNC_FAILED)

        await self._audit.record(
            actor,
            AuditEntry(
                action=action,
                outcome=(
                    OUTCOME_SUCCESS if outcome.status == SyncStatus.SUCCEEDED else OUTCOME_FAILURE
                ),
                target_type="connection",
                target_id=str(connection_id),
                metadata={
                    "job_id": str(job_id),
                    "status": outcome.status.value,
                    "records_fetched": outcome.records_fetched,
                    "records_persisted": outcome.records_persisted,
                    "records_delivered": outcome.records_delivered,
                    "records_failed": outcome.records_failed,
                    "error_code": outcome.error_code,
                },
            ),
        )

        if self._metrics:
            self._metrics.sync_finished(
                organization_id=organization_id,
                status=outcome.status.value,
                duration_seconds=outcome.duration_ms / 1000.0,
                error_code=outcome.error_code,
            )

        await self._runtime_connection_update(organization_id, connection_id, outcome)

    async def _runtime_load(self, organization_id: uuid.UUID, connection_id: uuid.UUID) -> Any:
        # Reuse the worker-scoped loader so tenant context is applied the same way.
        async with self._database.tenant_session(organization_id) as session:
            connection = await session.scalar(
                select(Connection).where(
                    Connection.organization_id == organization_id,
                    Connection.id == connection_id,
                )
            )
            if connection is None:
                raise NotFoundError(detail="Connection not found for this organization")
            return connection

    async def _runtime_connection_update(
        self, organization_id: uuid.UUID, connection_id: uuid.UUID, outcome: SyncOutcome
    ) -> None:
        from humisense.services.connections import ConnectionService

        service = ConnectionService(self._database, self._settings, self._audit, self._runtime)
        if outcome.status in {SyncStatus.SUCCEEDED, SyncStatus.PARTIAL}:
            await service.record_sync_outcome(
                organization_id, connection_id, ok=True, error_code=None
            )
        elif outcome.status == SyncStatus.CANCELLED:
            # A cancellation is not a connection fault; leave the status alone.
            return
        else:
            await service.record_sync_outcome(
                organization_id,
                connection_id,
                ok=False,
                error_code=outcome.error_code or "sync_failed",
            )

    @staticmethod
    def _coerce_cursor(raw: dict[str, Any] | None) -> Cursor | None:
        if not raw:
            return None
        try:
            return Cursor.model_validate(raw)
        except ValidationError:
            # A checkpoint written by an older schema is discarded rather than fatal.
            return None


@dataclass(frozen=True, slots=True)
class ConnectionSnapshot:
    """The immutable subset of a connection needed to run a job.

    Captured inside the transaction that marked the job running, so the job cannot be
    influenced by a concurrent update to the connection row.
    """

    id: uuid.UUID
    organization_id: uuid.UUID
    connector_type: str
    config: dict[str, Any]
    checkpoint: dict[str, Any] | None
    correlation_id: str | None

    @classmethod
    def from_model(
        cls, connection: Connection, *, correlation_id: str | None = None
    ) -> ConnectionSnapshot:
        return cls(
            id=connection.id,
            organization_id=connection.organization_id,
            connector_type=connection.connector_type,
            config=dict(connection.config or {}),
            checkpoint=dict(connection.checkpoint) if connection.checkpoint else None,
            correlation_id=correlation_id,
        )


def _affected_rows(result: Any) -> int:
    """Return the number of rows an UPDATE or DELETE affected.

    ``Session.execute`` is typed as returning ``Result[Any]``, which has no ``rowcount``.
    For a DML statement the runtime object is a ``CursorResult``, which does. This keeps
    the cast in one place instead of scattering ignores across call sites.
    """
    return int(cast(CursorResult[Any], result).rowcount or 0)


async def reap_stale_jobs(database: Database, *, older_than_seconds: int = 3600) -> int:
    """Mark jobs stuck in ``running`` as failed.

    A hard worker kill leaves no chance to finalize the job. Without this reaper the job
    row would block future runs for that connection forever.
    """
    from sqlalchemy import select, update

    from humisense.db.models import Organization

    cutoff = datetime.fromtimestamp(datetime.now(UTC).timestamp() - older_than_seconds, tz=UTC)
    reaped = 0

    # Row-Level Security denies a session that carries no tenant context, so a single
    # cross-tenant UPDATE would match nothing. The tenant list is read from
    # ``organizations``, which is not tenant-scoped, and each tenant is reaped inside its
    # own scoped transaction. That keeps the isolation guarantee intact rather than
    # weakening it for the sake of a maintenance task.
    async with database.system_session() as session:
        organization_ids = list((await session.scalars(select(Organization.id))).all())

    for organization_id in organization_ids:
        async with database.tenant_session(organization_id) as session:
            result = await session.execute(
                update(SyncJob)
                .where(
                    SyncJob.organization_id == organization_id,
                    SyncJob.status == SyncStatus.RUNNING,
                    SyncJob.started_at.is_not(None),
                    SyncJob.started_at < cutoff,
                )
                .values(
                    status=SyncStatus.FAILED,
                    error_code="stale_job_reaped",
                    error_message="The worker running this job stopped responding.",
                    finished_at=datetime.now(UTC),
                )
            )
            reaped += _affected_rows(result)
    return reaped
