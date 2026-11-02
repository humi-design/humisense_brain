"""Layer 2 ingestion endpoints.

Two authentication models meet here and are deliberately kept apart:

* ``POST /ingestion/batches`` is a machine-to-machine endpoint. The producer is a collector
  service, not a person, so it presents a service bearer token. The organization comes from
  the batch envelope but is *verified*, not trusted: the connection named in the batch must
  belong to that organization, and the check runs inside a tenant-scoped session, so a
  producer cannot write into a tenant whose connection it does not name.
* The quarantine endpoints are human-facing and use the session principal, with a
  permission and CSRF check, like the rest of the console API.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Header, Query, status

from humisense.api.deps import ContainerDep, PrincipalDep, enforce_csrf, require_permission
from humisense.api.schemas import (
    IngestBatchRequest,
    IngestBatchResponse,
    QuarantineItemResponse,
    QuarantineListResponse,
    QuarantineReplayResponse,
    RecordOutcomeResponse,
)
from humisense.layer2.receiver import ReceivedBatch, ReceivedRecord
from humisense.observability.correlation import get_correlation_id
from humisense.security.permissions import Permission
from humisense.services.audit import (
    OUTCOME_FAILURE,
    OUTCOME_SUCCESS,
    AuditAction,
    AuditActor,
    AuditEntry,
)

logger = structlog.get_logger(__name__)

# Human-facing, session-authenticated endpoints. Always registered.
router = APIRouter(tags=["ingestion"])
# Machine-to-machine receiver. Registered only when the receiver is enabled, so a disabled
# deployment has no route to reach it at all.
machine_router = APIRouter(tags=["ingestion"])

CSRF = Depends(enforce_csrf)


@machine_router.post(
    "/ingestion/batches",
    response_model=IngestBatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def ingest_batch(
    batch: IngestBatchRequest,
    container: ContainerDep,
    authorization: Annotated[str | None, Header()] = None,
) -> IngestBatchResponse:
    """Accept a batch from a producer service.

    The bearer token is verified in constant time. A batch is never rejected wholesale for
    one bad record: each record is validated, normalized, and stored on its own, and a
    failure becomes a quarantine row so the rest of the batch still lands.
    """
    presented = None
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:]
    container.layer2_receiver.authenticate(presented)

    correlation_id = get_correlation_id() or batch.correlation_id or batch.batch_id
    received = ReceivedBatch(
        organization_id=batch.organization_id,
        batch_id=batch.batch_id,
        correlation_id=correlation_id,
        producer=batch.producer,
        records=[
            ReceivedRecord(
                connection_id=item.connection_id,
                connector_type=item.connector_type,
                source_record_id=item.source_record_id,
                record_type=item.record_type,
                source_timestamp=item.source_timestamp,
                observed_timestamp=item.observed_timestamp,
                payload=item.payload,
                metadata=item.metadata,
                source_schema_version=item.source_schema_version,
                content_hash=item.content_hash,
                source_record_pk=None,
            )
            for item in batch.records
        ],
    )
    try:
        result = await container.layer2_receiver.receive(received)
    except Exception:
        # A rejected batch is recorded before it propagates: the audit row is the durable
        # evidence that a producer attempted a write, which a log line is not.
        await container.audit.record(
            AuditActor.worker(batch.organization_id, correlation_id=correlation_id),
            AuditEntry(
                action=AuditAction.INGESTION_BATCH_REJECTED,
                outcome=OUTCOME_FAILURE,
                target_type="ingest_batch",
                target_id=batch.batch_id,
                metadata={"producer": batch.producer, "records": len(batch.records)},
            ),
        )
        raise
    await container.audit.record(
        AuditActor.worker(batch.organization_id, correlation_id=correlation_id),
        AuditEntry(
            action=AuditAction.INGESTION_BATCH_RECEIVED,
            outcome=OUTCOME_SUCCESS,
            target_type="ingest_batch",
            target_id=result.batch_id,
            metadata={
                "producer": batch.producer,
                "accepted": result.accepted,
                "duplicates": result.duplicates,
                "quarantined": result.quarantined,
                "failed": result.failed,
            },
        ),
    )
    return IngestBatchResponse(
        batch_id=result.batch_id,
        correlation_id=correlation_id,
        accepted=result.accepted,
        duplicates=result.duplicates,
        quarantined=result.quarantined,
        failed=result.failed,
        outcomes=[
            RecordOutcomeResponse(
                source_record_id=outcome.source_record_id,
                state=outcome.state,
                error_code=outcome.error_code,
            )
            for outcome in result.outcomes
        ],
    )


@router.get(
    "/ingestion/quarantine",
    response_model=QuarantineListResponse,
    dependencies=[Depends(require_permission(Permission.INGESTION_READ))],
)
async def list_quarantine(
    container: ContainerDep,
    principal: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> QuarantineListResponse:
    """List records that were quarantined and have not been resolved."""
    items = await container.layer2_quarantine.list_open(principal.organization_id, limit=limit)
    return QuarantineListResponse(
        items=[
            QuarantineItemResponse(
                quarantine_id=item.quarantine_id,
                stage=item.stage,
                error_code=item.error_code,
                source_record_key=item.source_record_key,
                status=item.status,
                replay_count=item.replay_count,
                created_at=item.created_at,
            )
            for item in items
        ],
        total=len(items),
    )


@router.post(
    "/ingestion/quarantine/{quarantine_id}/replay",
    response_model=QuarantineReplayResponse,
    dependencies=[CSRF, Depends(require_permission(Permission.INGESTION_REPLAY))],
)
async def replay_quarantine(
    quarantine_id: uuid.UUID,
    container: ContainerDep,
    principal: PrincipalDep,
) -> QuarantineReplayResponse:
    """Re-run the pipeline over a quarantined record's preserved payload.

    The payload is read from ``raw_records``, so replay does not depend on the source still
    being reachable. Replay is idempotent: the canonical record's unique key turns a second
    replay into a duplicate rather than a second record.
    """
    outcome = await container.layer2_quarantine.replay(principal.organization_id, quarantine_id)
    await container.audit.record(
        AuditActor.user(principal, correlation_id=get_correlation_id()),
        AuditEntry(
            action=AuditAction.QUARANTINE_REPLAYED,
            outcome=OUTCOME_SUCCESS if outcome.replayed else OUTCOME_FAILURE,
            target_type="record_quarantine",
            target_id=str(quarantine_id),
            metadata={"state": outcome.state, "error_code": outcome.error_code},
        ),
    )
    return QuarantineReplayResponse(
        quarantine_id=outcome.quarantine_id,
        replayed=outcome.replayed,
        state=outcome.state,
        error_code=outcome.error_code,
    )
