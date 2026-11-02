"""Layer 3 analytics endpoints.

The console surface for detections, data quality, rule configuration, and running a job.

Tenant isolation is structural, not conventional: every repository method opens a
tenant-scoped session, so a request can only read or write its own tenant's rows even if a
handler forgot to filter. The handlers here pass the principal's organization through and
never accept a tenant identifier from the request body.

Running a job and configuring a rule are privileged: they consume compute or change what the
platform considers anomalous, so they require a dedicated permission and, for the state
changes, a CSRF check.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Query, status

from humisense.api.deps import ContainerDep, PrincipalDep, enforce_csrf, require_permission
from humisense.api.schemas import (
    AnalyticsRunRequest,
    AnalyticsRunResponse,
    DetectionListResponse,
    DetectionResponse,
    DetectionStatusUpdateRequest,
    QualityListResponse,
    QualityResultResponse,
    RuleCreateRequest,
    RuleResponse,
)
from humisense.layer3.contract import RuleCondition
from humisense.layer3.rules import RuleError, validate_condition
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

router = APIRouter(tags=["analytics"])

CSRF = Depends(enforce_csrf)


def _detection_response(row: object) -> DetectionResponse:
    return DetectionResponse(
        detection_id=row.detection_id,  # type: ignore[attr-defined]
        domain=row.domain,  # type: ignore[attr-defined]
        entity_id=row.entity_id,  # type: ignore[attr-defined]
        entity_type=row.entity_type,  # type: ignore[attr-defined]
        metric=row.metric,  # type: ignore[attr-defined]
        detection_category=row.detection_category,  # type: ignore[attr-defined]
        detection_type=row.detection_type,  # type: ignore[attr-defined]
        severity=row.severity,  # type: ignore[attr-defined]
        status=row.status.value if hasattr(row.status, "value") else row.status,  # type: ignore[attr-defined]
        observed_value=row.observed_value,  # type: ignore[attr-defined]
        expected_value=row.expected_value,  # type: ignore[attr-defined]
        deviation=row.deviation,  # type: ignore[attr-defined]
        unit=row.unit,  # type: ignore[attr-defined]
        observation_window_start=row.observation_window_start,  # type: ignore[attr-defined]
        observation_window_end=row.observation_window_end,  # type: ignore[attr-defined]
        detected_at=row.detected_at,  # type: ignore[attr-defined]
        algorithm_name=row.algorithm_name,  # type: ignore[attr-defined]
        algorithm_version=row.algorithm_version,  # type: ignore[attr-defined]
        baseline_id=row.baseline_id,  # type: ignore[attr-defined]
        baseline_version=row.baseline_version,  # type: ignore[attr-defined]
        anomaly_score=row.anomaly_score,  # type: ignore[attr-defined]
        confidence=row.confidence,  # type: ignore[attr-defined]
        data_quality_status=row.data_quality_status,  # type: ignore[attr-defined]
        evidence_references=row.evidence_references or [],  # type: ignore[attr-defined]
        explanation=row.explanation,  # type: ignore[attr-defined]
        limitations=row.limitations or [],  # type: ignore[attr-defined]
        schema_version=row.schema_version,  # type: ignore[attr-defined]
        correlation_id=row.correlation_id,  # type: ignore[attr-defined]
    )


@router.post(
    "/analytics/run",
    response_model=AnalyticsRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[CSRF, Depends(require_permission(Permission.ANALYTICS_RUN))],
)
async def run_analytics(
    request: AnalyticsRunRequest,
    container: ContainerDep,
    principal: PrincipalDep,
) -> AnalyticsRunResponse:
    """Run an analytics job over a bounded window for the caller's tenant."""
    correlation_id = get_correlation_id() or str(uuid.uuid4())
    await container.audit.record(
        AuditActor.user(principal, correlation_id=correlation_id),
        AuditEntry(
            action=AuditAction.ANALYTICS_RUN_REQUESTED,
            outcome=OUTCOME_SUCCESS,
            target_type="analytics_job",
            target_id=request.job_type,
            metadata={"since": str(request.since), "until": str(request.until)},
        ),
    )
    try:
        if request.job_type == "quality_scan":
            result = await container.layer3_engine.run_quality_scan(
                principal.organization_id,
                correlation_id=correlation_id,
                connection_id=request.connection_id,
                since=request.since,
                until=request.until,
                row_limit=request.row_limit,
            )
        else:
            result = await container.layer3_engine.run_full(
                principal.organization_id,
                correlation_id=correlation_id,
                connection_id=request.connection_id,
                since=request.since,
                until=request.until,
                row_limit=request.row_limit,
            )
    except Exception:
        await container.audit.record(
            AuditActor.user(principal, correlation_id=correlation_id),
            AuditEntry(
                action=AuditAction.ANALYTICS_RUN_FAILED,
                outcome=OUTCOME_FAILURE,
                target_type="analytics_job",
                target_id=request.job_type,
                metadata={},
            ),
        )
        raise
    await container.audit.record(
        AuditActor.user(principal, correlation_id=correlation_id),
        AuditEntry(
            action=AuditAction.ANALYTICS_RUN_SUCCEEDED,
            outcome=OUTCOME_SUCCESS,
            target_type="analytics_job",
            target_id=request.job_type,
            metadata=dict(result.counters),
        ),
    )
    return AnalyticsRunResponse(
        job_type=result.job_type,
        correlation_id=result.correlation_id,
        status=result.status.value,
        signals_analyzed=result.signals_analyzed,
        detections=result.detections,
        suppressed=result.suppressed,
        quality_issues=result.quality_issues,
        skips=[skip.as_metadata() for skip in result.skips],
        counters=result.counters,
    )


@router.get(
    "/analytics/detections",
    response_model=DetectionListResponse,
    dependencies=[Depends(require_permission(Permission.ANALYTICS_READ))],
)
async def list_detections(
    container: ContainerDep,
    principal: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    severity: Annotated[str | None, Query(max_length=20)] = None,
    detection_status: Annotated[str | None, Query(alias="status", max_length=20)] = None,
    entity_id: Annotated[str | None, Query(max_length=200)] = None,
) -> DetectionListResponse:
    """List detections for the caller's tenant, newest first."""
    rows = await container.layer3_repository.list_detections(
        principal.organization_id,
        limit=limit,
        offset=offset,
        severity=severity,
        status=detection_status,
        entity_id=entity_id,
    )
    return DetectionListResponse(
        items=[_detection_response(row) for row in rows],
        total=len(rows),
    )


@router.get(
    "/analytics/detections/{detection_id}",
    response_model=DetectionResponse,
    dependencies=[Depends(require_permission(Permission.ANALYTICS_READ))],
)
async def get_detection(
    detection_id: uuid.UUID,
    container: ContainerDep,
    principal: PrincipalDep,
) -> DetectionResponse:
    """Return one detection, or 404 when it is not the caller's."""
    row = await container.layer3_repository.get_detection(
        principal.organization_id, detection_id=detection_id
    )
    if row is None:
        from humisense.errors import NotFoundError

        raise NotFoundError("detection not found")
    return _detection_response(row)


@router.patch(
    "/analytics/detections/{detection_id}",
    response_model=DetectionResponse,
    dependencies=[CSRF, Depends(require_permission(Permission.ANALYTICS_CONFIGURE))],
)
async def update_detection(
    detection_id: uuid.UUID,
    request: DetectionStatusUpdateRequest,
    container: ContainerDep,
    principal: PrincipalDep,
) -> DetectionResponse:
    """Change a detection's lifecycle status, recording the transition."""
    from humisense.errors import NotFoundError

    correlation_id = get_correlation_id() or str(uuid.uuid4())
    suppressed_until = None
    if request.status == "suppressed" and request.suppress_for_seconds:
        suppressed_until = datetime.now(tz=UTC) + timedelta(seconds=request.suppress_for_seconds)
    row = await container.layer3_repository.update_detection_status(
        principal.organization_id,
        detection_id=detection_id,
        to_status=request.status,
        actor_id=principal.user_id,
        reason=request.reason,
        correlation_id=correlation_id,
        resolved=request.status == "resolved",
        suppressed_until=suppressed_until,
    )
    if row is None:
        raise NotFoundError("detection not found")
    await container.audit.record(
        AuditActor.user(principal, correlation_id=correlation_id),
        AuditEntry(
            action=AuditAction.ANALYTICS_RUN_SUCCEEDED,
            outcome=OUTCOME_SUCCESS,
            target_type="detection",
            target_id=str(detection_id),
            metadata={"status": request.status},
        ),
    )
    return _detection_response(row)


@router.get(
    "/analytics/quality",
    response_model=QualityListResponse,
    dependencies=[Depends(require_permission(Permission.ANALYTICS_READ))],
)
async def list_quality(
    container: ContainerDep,
    principal: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    quality_status: Annotated[str | None, Query(alias="status", max_length=20)] = None,
    entity_id: Annotated[str | None, Query(max_length=200)] = None,
) -> QualityListResponse:
    """List data-quality assessments for the caller's tenant, newest first."""
    rows = await container.layer3_repository.list_quality_results(
        principal.organization_id,
        limit=limit,
        offset=offset,
        status=quality_status,
        entity_id=entity_id,
    )
    return QualityListResponse(
        items=[
            QualityResultResponse(
                entity_id=row.entity_id,
                metric=row.metric,
                status=row.status,
                evaluated_count=row.evaluated_count,
                missing_count=row.missing_count,
                invalid_count=row.invalid_count,
                duplicate_count=row.duplicate_count,
                issues=row.issues or [],
                window_start=row.window_start,
                window_end=row.window_end,
            )
            for row in rows
        ],
        total=len(rows),
    )


@router.get(
    "/analytics/rules",
    response_model=list[RuleResponse],
    dependencies=[Depends(require_permission(Permission.ANALYTICS_READ))],
)
async def list_rules(
    container: ContainerDep,
    principal: PrincipalDep,
) -> list[RuleResponse]:
    """List the caller's tenant's rules with their current version."""
    pairs = await container.layer3_repository.list_rules(principal.organization_id)
    return [
        RuleResponse(
            rule_id=rule.rule_id,
            name=rule.name,
            description=rule.description,
            domain=rule.domain,
            scope=rule.scope,
            entity_id=rule.entity_id,
            metric=rule.metric,
            condition=version.condition,
            window_seconds=version.window_seconds,
            severity=version.severity,
            enabled=rule.enabled,
            version=version.version,
        )
        for rule, version in pairs
    ]


@router.post(
    "/analytics/rules",
    response_model=RuleResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[CSRF, Depends(require_permission(Permission.ANALYTICS_CONFIGURE))],
)
async def create_rule(
    request: RuleCreateRequest,
    container: ContainerDep,
    principal: PrincipalDep,
) -> RuleResponse:
    """Create or update a rule.

    The condition is validated against the bounded condition schema before it is stored: an
    unknown operator or an over-deep tree is rejected here, so an invalid rule can never
    reach the engine.
    """
    from humisense.errors import ValidationFailure

    correlation_id = get_correlation_id() or str(uuid.uuid4())
    try:
        condition = RuleCondition.model_validate(request.condition)
        validate_condition(condition)
    except (RuleError, ValueError) as error:
        raise ValidationFailure(f"invalid rule condition: {error}") from error

    rule_id = uuid.uuid4()
    version = await container.layer3_repository.upsert_rule(
        principal.organization_id,
        rule_id=rule_id,
        name=request.name,
        description=request.description,
        domain=request.domain,
        scope=request.scope,
        entity_id=request.entity_id,
        metric=request.metric,
        condition=condition.model_dump(mode="json"),
        window_seconds=request.window_seconds,
        severity=request.severity,
        enabled=request.enabled,
    )
    await container.audit.record(
        AuditActor.user(principal, correlation_id=correlation_id),
        AuditEntry(
            action=AuditAction.ANALYTICS_RULE_UPDATED,
            outcome=OUTCOME_SUCCESS,
            target_type="analytics_rule",
            target_id=str(rule_id),
            metadata={"version": version, "metric": request.metric},
        ),
    )
    return RuleResponse(
        rule_id=rule_id,
        name=request.name,
        description=request.description,
        domain=request.domain,
        scope=request.scope,
        entity_id=request.entity_id,
        metric=request.metric,
        condition=condition.model_dump(mode="json"),
        window_seconds=request.window_seconds,
        severity=request.severity,
        enabled=request.enabled,
        version=version,
    )
