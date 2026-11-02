"""Connection, synchronization, record, audit, and overview endpoints.

Every handler depends on :data:`PrincipalDep`, so authentication is enforced by the
dependency graph rather than by a decorator that is easy to forget. Authorization is
enforced twice: once here as a route-level permission, and again inside the service, so a
handler that is ever called from another entry point (a script, a worker, a future
endpoint) cannot skip the check.

Object-level authorization is by construction: services look connections and jobs up by
``(organization_id, id)``, where the organization comes from the session. A foreign
identifier yields 404, not 403, so the API does not confirm that another tenant's object
exists.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select

from humisense.api.deps import (
    ContainerDep,
    PrincipalDep,
    SettingsDep,
    enforce_csrf,
    require_permission,
)
from humisense.api.schemas import (
    AuditEventListResponse,
    AuditEventResponse,
    ConnectionCreateRequest,
    ConnectionListResponse,
    ConnectionResponse,
    ConnectionStatusResponse,
    ConnectionTestResponse,
    ConnectionUpdateRequest,
    OverviewResponse,
    SourceRecordListResponse,
    SourceRecordResponse,
    SyncAcceptedResponse,
    SyncJobListResponse,
    SyncJobResponse,
)
from humisense.connectors.base import ensure_available, get_manifest
from humisense.db.models import (
    Connection,
    ConnectionStatus,
    SyncJob,
)
from humisense.errors import NotFoundError, ValidationFailure
from humisense.observability.correlation import get_correlation_id
from humisense.security.permissions import Permission
from humisense.services.audit import (
    OUTCOME_SUCCESS,
    AuditAction,
    AuditActor,
    AuditEntry,
)
from humisense.services.connections import ConnectionDraft

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["connections"])

CSRF = Depends(enforce_csrf)


async def _enforce_write_budget(
    container: ContainerDep, principal: PrincipalDep, settings: SettingsDep
) -> None:
    """Apply the per-tenant write budget to every mutating connection route.

    This is a dependency rather than a per-handler call, so a new mutating route inherits
    the limit by reusing the shared dependency list instead of relying on its author to
    remember to add a check.
    """
    await container.rate_limiter.check(
        scope="connection_write",
        identifier=str(principal.organization_id),
        limit=settings.rate_limit_writes_per_minute,
        window_seconds=60,
    )


WRITE_BUDGET = Depends(_enforce_write_budget)

RECORD_PREVIEW_NOTICE = (
    "Captured source content is untrusted data. It is returned as opaque values and must "
    "be rendered as text, never as HTML."
)


def _connection_response(connection: Connection) -> ConnectionResponse:
    """Serialize a connection without exposing the credential reference."""
    return ConnectionResponse(
        id=connection.id,
        connector_type=connection.connector_type,
        name=connection.name,
        description=connection.description,
        config=connection.config or {},
        has_credentials=bool(connection.secret_ref),
        status=connection.status.value,
        enabled=connection.enabled,
        capabilities=connection.capabilities or {},
        last_tested_at=connection.last_tested_at,
        last_success_at=connection.last_success_at,
        last_error_code=connection.last_error_code,
        created_at=connection.created_at,
        updated_at=connection.updated_at,
    )


# --- Connections ----------------------------------------------------------


@router.get(
    "/connections",
    response_model=ConnectionListResponse,
    summary="List connections in the caller's organization",
    dependencies=[Depends(require_permission(Permission.CONNECTIONS_READ))],
)
async def list_connections(
    container: ContainerDep,
    settings: SettingsDep,
    principal: PrincipalDep,
    limit: Annotated[int | None, Query(ge=1)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    connection_status: Annotated[str | None, Query(alias="status", max_length=20)] = None,
) -> ConnectionListResponse:
    """List tenant-scoped connections with bounded pagination."""
    effective_limit = min(limit or settings.default_page_size, settings.max_page_size)
    parsed_status: ConnectionStatus | None = None
    if connection_status:
        try:
            parsed_status = ConnectionStatus(connection_status)
        except ValueError as exc:
            raise ValidationFailure(
                detail="Unknown status filter",
                context={"allowed": [s.value for s in ConnectionStatus]},
            ) from exc

    rows, total = await container.connections.list_connections(
        principal, limit=effective_limit, offset=offset, status=parsed_status
    )
    return ConnectionListResponse(
        items=[_connection_response(row) for row in rows],
        total=total,
        limit=effective_limit,
        offset=offset,
    )


@router.post(
    "/connections",
    response_model=ConnectionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a connection",
    dependencies=[Depends(require_permission(Permission.CONNECTIONS_CREATE)), CSRF, WRITE_BUDGET],
)
async def create_connection(
    container: ContainerDep,
    principal: PrincipalDep,
    payload: ConnectionCreateRequest,
) -> ConnectionResponse:
    """Create a tenant-owned connection.

    ``connector_type`` is validated against the registry. Configuration is validated
    against the connector's own schema, and secret fields are diverted to the secrets
    provider rather than the configuration column.
    """
    ensure_available(payload.connector_type)
    draft = ConnectionDraft(
        connector_type=payload.connector_type,
        name=payload.name,
        description=payload.description,
        config=payload.config,
        credentials=payload.credentials,
    )
    connection = await container.connections.create_connection(principal, draft)
    return _connection_response(connection)


@router.get(
    "/connections/{connection_id}",
    response_model=ConnectionResponse,
    summary="Read one connection",
    dependencies=[Depends(require_permission(Permission.CONNECTIONS_READ))],
)
async def get_connection(
    container: ContainerDep, principal: PrincipalDep, connection_id: uuid.UUID
) -> ConnectionResponse:
    connection = await container.connections.get_connection(principal, connection_id)
    return _connection_response(connection)


@router.patch(
    "/connections/{connection_id}",
    response_model=ConnectionResponse,
    summary="Update a connection",
    dependencies=[Depends(require_permission(Permission.CONNECTIONS_UPDATE)), CSRF, WRITE_BUDGET],
)
async def update_connection(
    container: ContainerDep,
    principal: PrincipalDep,
    connection_id: uuid.UUID,
    payload: ConnectionUpdateRequest,
) -> ConnectionResponse:
    """Update a connection. The connector type is immutable."""
    if (
        payload.name is None
        and payload.description is None
        and payload.config is None
        and payload.credentials is None
        and payload.enabled is None
    ):
        raise ValidationFailure(detail="No fields were supplied to update")

    connection = await container.connections.update_connection(
        principal,
        connection_id,
        name=payload.name,
        description=payload.description,
        config=payload.config,
        credentials=payload.credentials,
        enabled=payload.enabled,
    )
    return _connection_response(connection)


@router.delete(
    "/connections/{connection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a connection",
    dependencies=[Depends(require_permission(Permission.CONNECTIONS_DELETE)), CSRF, WRITE_BUDGET],
)
async def delete_connection(
    container: ContainerDep, principal: PrincipalDep, connection_id: uuid.UUID
) -> None:
    """Delete a connection, revoke its credentials, and cascade its captured records."""
    await container.connections.delete_connection(principal, connection_id)
    return None


@router.post(
    "/connections/{connection_id}/test",
    response_model=ConnectionTestResponse,
    summary="Test a connection",
    dependencies=[Depends(require_permission(Permission.CONNECTIONS_TEST)), CSRF],
)
async def test_connection(
    container: ContainerDep,
    settings: SettingsDep,
    principal: PrincipalDep,
    connection_id: uuid.UUID,
) -> ConnectionTestResponse:
    """Probe the source system and record the outcome.

    The result is the connector's own honest report. A connector that cannot reach its
    source returns ``ok: false`` with a machine-readable code, never a fabricated success.
    """
    connection = await container.connections.get_connection(principal, connection_id)

    # Rate limit per tenant so a caller cannot use the test endpoint to hammer a source.
    await container.rate_limiter.check(
        scope="connection_test",
        identifier=str(principal.organization_id),
        limit=settings.rate_limit_tests_per_minute,
        window_seconds=60,
    )

    correlation_id = get_correlation_id()
    actor = AuditActor.user(principal, correlation_id=correlation_id)
    connector = None
    try:
        connector = await container.runtime.build_connector(
            connection, correlation_id=correlation_id
        )
        result = await connector.test_connection()
    except Exception as exc:  # noqa: BLE001 - a connector fault must not become a 500
        from humisense.errors import HumisenseError

        if isinstance(exc, HumisenseError):
            code = exc.code
            message = exc.public_message
        else:
            logger.error(
                "connection_test_crashed",
                connection_id=str(connection_id),
                error_type=type(exc).__name__,
            )
            code = "connector_error"
            message = "The connection test could not be completed."
        await container.connections.record_test_result(
            principal.organization_id,
            connection_id,
            ok=False,
            error_code=code,
            actor=actor,
        )
        return ConnectionTestResponse(
            ok=False, message=message, error_code=code, latency_ms=None, details={}
        )
    finally:
        if connector is not None:
            await connector.close()

    await container.connections.record_test_result(
        principal.organization_id,
        connection_id,
        ok=result.ok,
        error_code=result.error_code,
        actor=actor,
    )
    return ConnectionTestResponse(
        ok=result.ok,
        message=result.message,
        error_code=result.error_code,
        latency_ms=result.latency_ms,
        details=result.details,
    )


# --- Synchronization ------------------------------------------------------


@router.post(
    "/connections/{connection_id}/sync",
    response_model=SyncAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a synchronization",
    dependencies=[Depends(require_permission(Permission.SYNC_RUN)), CSRF],
)
async def start_sync(
    container: ContainerDep,
    settings: SettingsDep,
    principal: PrincipalDep,
    connection_id: uuid.UUID,
) -> SyncAcceptedResponse:
    """Enqueue a synchronization and return immediately.

    The work happens in a worker process, not in this request, so a slow source system
    cannot occupy request workers. The response carries the job so the caller can poll it.
    """
    await container.rate_limiter.check(
        scope="sync_start",
        identifier=str(principal.organization_id),
        limit=settings.rate_limit_sync_per_hour,
        window_seconds=3600,
    )
    # Confirm the caller can see this connection before creating a job for it.
    await container.connections.get_connection(principal, connection_id)

    correlation_id = get_correlation_id()
    job = await container.sync.create_job(
        organization_id=principal.organization_id,
        connection_id=connection_id,
        triggered_by=principal.user_id,
        correlation_id=correlation_id,
    )
    await container.audit.record(
        AuditActor.user(principal, correlation_id=correlation_id),
        AuditEntry(
            action=AuditAction.SYNC_REQUESTED,
            outcome=OUTCOME_SUCCESS,
            target_type="connection",
            target_id=str(connection_id),
            metadata={"job_id": str(job.id)},
        ),
    )
    queue_job_id = await container.enqueue_sync(
        organization_id=principal.organization_id,
        job_id=job.id,
        correlation_id=correlation_id,
    )
    return SyncAcceptedResponse(job=SyncJobResponse.model_validate(job), queue_job_id=queue_job_id)


@router.post(
    "/connections/{connection_id}/sync/{job_id}/cancel",
    response_model=SyncJobResponse,
    summary="Request cancellation of a synchronization",
    dependencies=[Depends(require_permission(Permission.SYNC_CANCEL)), CSRF],
)
async def cancel_sync(
    container: ContainerDep,
    principal: PrincipalDep,
    connection_id: uuid.UUID,
    job_id: uuid.UUID,
) -> SyncJobResponse:
    """Set the cancellation flag. The worker observes it at its next poll."""
    # Verify the connection belongs to the caller before touching the job.
    await container.connections.get_connection(principal, connection_id)
    job = await container.sync.request_cancel(principal.organization_id, job_id)
    if job.connection_id != connection_id:
        raise NotFoundError(detail="Synchronization job not found")
    await container.audit.record(
        AuditActor.user(principal, correlation_id=get_correlation_id()),
        AuditEntry(
            action=AuditAction.SYNC_CANCELLED,
            outcome=OUTCOME_SUCCESS,
            target_type="connection",
            target_id=str(connection_id),
            metadata={"job_id": str(job_id), "requested": True},
        ),
    )
    return SyncJobResponse.model_validate(job)


@router.get(
    "/connections/{connection_id}/status",
    response_model=ConnectionStatusResponse,
    summary="Connection status",
    dependencies=[Depends(require_permission(Permission.SYNC_READ))],
)
async def connection_status(
    container: ContainerDep,
    principal: PrincipalDep,
    connection_id: uuid.UUID,
) -> ConnectionStatusResponse:
    """Return health, the last job, and the captured-record count."""
    connection = await container.connections.get_connection(principal, connection_id)
    jobs = await container.sync.list_jobs(principal.organization_id, connection_id, limit=1)
    record_count = await container.records.count_records(principal.organization_id, connection_id)
    manifest = get_manifest(connection.connector_type)
    return ConnectionStatusResponse(
        connection_id=connection.id,
        status=connection.status.value,
        enabled=connection.enabled,
        last_tested_at=connection.last_tested_at,
        last_success_at=connection.last_success_at,
        last_error_code=connection.last_error_code,
        record_count=record_count,
        last_job=SyncJobResponse.model_validate(jobs[0]) if jobs else None,
        connector_capabilities=manifest.capabilities.as_dict(),
    )


@router.get(
    "/connections/{connection_id}/jobs",
    response_model=SyncJobListResponse,
    summary="List synchronization jobs",
    dependencies=[Depends(require_permission(Permission.SYNC_READ))],
)
async def list_jobs(
    container: ContainerDep,
    settings: SettingsDep,
    principal: PrincipalDep,
    connection_id: uuid.UUID,
    limit: Annotated[int | None, Query(ge=1)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SyncJobListResponse:
    """List synchronization history for one connection."""
    await container.connections.get_connection(principal, connection_id)
    effective_limit = min(limit or settings.default_page_size, settings.max_page_size)
    jobs = await container.sync.list_jobs(
        principal.organization_id, connection_id, limit=effective_limit, offset=offset
    )
    return SyncJobListResponse(
        items=[SyncJobResponse.model_validate(job) for job in jobs],
        total=len(jobs),
        limit=effective_limit,
        offset=offset,
    )


@router.get(
    "/connections/{connection_id}/records",
    response_model=SourceRecordListResponse,
    summary="Preview captured records",
    dependencies=[Depends(require_permission(Permission.RECORDS_READ))],
)
async def list_records(
    container: ContainerDep,
    settings: SettingsDep,
    principal: PrincipalDep,
    connection_id: uuid.UUID,
    limit: Annotated[int | None, Query(ge=1)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    record_type: Annotated[str | None, Query(max_length=120)] = None,
) -> SourceRecordListResponse:
    """Return a bounded preview of captured records.

    Payloads are truncated per record so this endpoint cannot be used to bulk-export
    source data, and they are returned as opaque values with an explicit untrusted marker.
    """
    await container.connections.get_connection(principal, connection_id)
    effective_limit = min(limit or settings.default_page_size, settings.max_page_size)
    records = await container.records.list_records(
        principal.organization_id,
        connection_id,
        limit=effective_limit,
        offset=offset,
        record_type=record_type,
    )
    total = await container.records.count_records(principal.organization_id, connection_id)
    return SourceRecordListResponse(
        items=[
            SourceRecordResponse(**container.records.to_public_dict(record)) for record in records
        ],
        total=total,
        limit=effective_limit,
        offset=offset,
        notice=RECORD_PREVIEW_NOTICE,
    )


# --- Audit ----------------------------------------------------------------


@router.get(
    "/audit-events",
    response_model=AuditEventListResponse,
    summary="List audit events",
    dependencies=[Depends(require_permission(Permission.AUDIT_READ))],
)
async def list_audit_events(
    container: ContainerDep,
    settings: SettingsDep,
    principal: PrincipalDep,
    limit: Annotated[int | None, Query(ge=1)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    action: Annotated[str | None, Query(max_length=80)] = None,
) -> AuditEventListResponse:
    """List the tenant's audit trail, newest first."""
    effective_limit = min(limit or settings.default_page_size, settings.max_page_size)
    events = await container.audit.list_events(
        principal.organization_id, limit=effective_limit, offset=offset, action=action
    )
    total = await container.audit.count_events(principal.organization_id)
    await container.audit.record(
        AuditActor.user(principal, correlation_id=get_correlation_id()),
        AuditEntry(
            action=AuditAction.AUDIT_VIEWED,
            outcome=OUTCOME_SUCCESS,
            target_type="audit_events",
            metadata={"limit": effective_limit, "filter_action": action},
        ),
    )
    return AuditEventListResponse(
        items=[
            AuditEventResponse(
                id=event.id,
                actor_id=event.actor_id,
                actor_type=event.actor_type.value,
                action=event.action,
                target_type=event.target_type,
                target_id=event.target_id,
                outcome=event.outcome,
                correlation_id=event.correlation_id,
                metadata=event.event_metadata or {},
                created_at=event.created_at,
            )
            for event in events
        ],
        total=total,
        limit=effective_limit,
        offset=offset,
    )


# --- Overview -------------------------------------------------------------


@router.get(
    "/overview",
    response_model=OverviewResponse,
    summary="Organization overview",
    dependencies=[Depends(require_permission(Permission.CONNECTIONS_READ))],
)
async def overview(
    container: ContainerDep,
    principal: PrincipalDep,
) -> OverviewResponse:
    """Return real counts for the overview page. No placeholder values."""
    from humisense.connectors.base import registry
    from humisense.db.models import SourceRecord

    async with container.database.tenant_session(principal.organization_id) as session:
        rows = (
            await session.execute(
                select(Connection.status, func.count())
                .where(Connection.organization_id == principal.organization_id)
                .group_by(Connection.status)
            )
        ).all()
        status_counts: dict[str, int] = {status.value: count for status, count in rows}
        record_total = await session.scalar(
            select(func.count())
            .select_from(SourceRecord)
            .where(SourceRecord.organization_id == principal.organization_id)
        )
        recent_jobs = list(
            (
                await session.scalars(
                    select(SyncJob)
                    .where(SyncJob.organization_id == principal.organization_id)
                    .order_by(SyncJob.queued_at.desc())
                    .limit(10)
                )
            ).all()
        )

    manifests = registry.manifests()
    available = sum(1 for m in manifests if m.availability.value == "available")
    not_configured = len(manifests) - available

    from humisense.db.models import Organization

    async with container.database.system_session() as session:
        org_name = await session.scalar(
            select(Organization.name).where(Organization.id == principal.organization_id)
        )

    return OverviewResponse(
        organization_id=principal.organization_id,
        organization_name=str(org_name or "Unknown organization"),
        connections_total=sum(status_counts.values()),
        connections_active=status_counts.get(ConnectionStatus.ACTIVE, 0),
        connections_error=status_counts.get(ConnectionStatus.ERROR, 0),
        connections_disabled=status_counts.get(ConnectionStatus.DISABLED, 0),
        connector_types_available=available,
        connector_types_not_configured=not_configured,
        records_captured=int(record_total or 0),
        last_sync=SyncJobResponse.model_validate(recent_jobs[0]) if recent_jobs else None,
        recent_jobs=[SyncJobResponse.model_validate(job) for job in recent_jobs],
        ingestion_sink=container.ingestion.sink_name,
        layer2_configured=container.ingestion.sink_name != "disabled",
    )
