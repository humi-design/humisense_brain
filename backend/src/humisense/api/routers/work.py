"""Layer 4 work intelligence endpoints.

The read surface for work: entities, commitments, deadlines, dependencies, blockers,
follow-ups, workflows, timeline, conflicts, and the personal, department, and executive
views. Plus the small set of authorized work-state changes the product supports.

Tenant isolation is structural: every repository call opens a tenant-scoped session, so a
handler cannot read or write another tenant's work even if it forgot to filter. The
principal's organization is passed through from the authenticated session; no tenant
identifier is ever accepted from the request.

Reads require ``work:read``; state changes require ``work:manage`` and a CSRF check. There
is no endpoint that sends anything to an external party: those actions belong to later
layers and require authorization Layer 4 does not have.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Query, status

from humisense.api.deps import ContainerDep, PrincipalDep, enforce_csrf, require_permission
from humisense.api.schemas import (
    BlockerListResponse,
    BlockerResponse,
    CommitmentListResponse,
    CommitmentResponse,
    ConflictListResponse,
    ConflictResolveRequest,
    ConflictResponse,
    DepartmentListResponse,
    DepartmentOverviewResponse,
    DepartmentResponse,
    DependencyListResponse,
    DependencyResponse,
    ExecutiveOverviewResponse,
    ExtractionListResponse,
    ExtractionResponse,
    FollowUpListResponse,
    FollowUpResponse,
    MyWorkResponse,
    WorkAssignRequest,
    WorkEntityDetailResponse,
    WorkEntityResponse,
    WorkflowInstanceListResponse,
    WorkflowInstanceResponse,
    WorkflowStepResponse,
    WorkListResponse,
    WorkPriorityResponse,
    WorkRunRequest,
    WorkRunResponse,
    WorkSourceReferenceResponse,
    WorkStatusUpdateRequest,
    WorkSummaryItem,
    WorkTimelineEventResponse,
    WorkTimelineResponse,
)
from humisense.db.models import AssignmentRole, WorkStatus
from humisense.layer4.views import WorkItemSummary, WorkScope
from humisense.observability.correlation import get_correlation_id
from humisense.security.permissions import Permission

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["work"])

CSRF = Depends(enforce_csrf)


def _scope(principal: PrincipalDep) -> WorkScope:
    return WorkScope(
        organization_id=principal.organization_id,
        user_id=principal.user_id,
        role=principal.role,
    )


def _entity_response(row: Any) -> WorkEntityResponse:
    return WorkEntityResponse(
        work_id=row.id,
        work_key=row.work_key,
        work_type=row.work_type.value,
        title=row.title,
        description=row.description,
        domain=row.domain.value,
        department_id=row.department_id,
        owner_id=row.owner_id,
        assigned_user_id=row.assigned_user_id,
        priority=row.priority.value,
        priority_source=row.priority_source.value,
        status=row.status.value,
        source_status=row.source_status,
        origin=row.origin.value,
        confidence=row.confidence,
        start_at=row.start_at,
        due_at=row.due_at,
        due_source=row.due_source.value if row.due_source else None,
        completed_at=row.completed_at,
        customer_ref=row.customer_ref,
        supplier_ref=row.supplier_ref,
        order_ref=row.order_ref,
        project_ref=row.project_ref,
        machine_ref=row.machine_ref,
        production_ref=row.production_ref,
        attributes=row.attributes or {},
        schema_version=row.schema_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _summary_response(item: WorkItemSummary) -> WorkSummaryItem:
    return WorkSummaryItem(
        work_id=item.work_id,
        title=item.title,
        work_type=item.work_type,
        domain=item.domain,
        status=item.status,
        priority=item.priority,
        due_at=item.due_at,
        overdue=item.overdue,
        due_soon=item.due_soon,
        blocked=item.blocked,
        owner_id=item.owner_id,
        department_id=item.department_id,
        updated_at=item.updated_at,
    )


@router.get(
    "/work",
    response_model=WorkListResponse,
    dependencies=[Depends(require_permission(Permission.WORK_READ))],
)
async def list_work(
    container: ContainerDep,
    principal: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    work_status: Annotated[str | None, Query(alias="status", max_length=30)] = None,
    work_type: Annotated[str | None, Query(max_length=80)] = None,
    domain: Annotated[str | None, Query(max_length=40)] = None,
    department_id: Annotated[uuid.UUID | None, Query()] = None,
    owner_id: Annotated[uuid.UUID | None, Query()] = None,
    due_before: Annotated[datetime | None, Query()] = None,
    due_after: Annotated[datetime | None, Query()] = None,
    search: Annotated[str | None, Query(max_length=100)] = None,
) -> WorkListResponse:
    """List work for the caller's tenant, newest first."""
    rows, total = await container.layer4_repository.list_work_entities(
        principal.organization_id,
        limit=limit,
        offset=offset,
        status=work_status,
        work_type=work_type,
        domain=domain,
        department_id=department_id,
        owner_id=owner_id,
        due_before=due_before,
        due_after=due_after,
        search=search,
    )
    return WorkListResponse(
        items=[_entity_response(row) for row in rows], total=total, limit=limit, offset=offset
    )


@router.get(
    "/work/my-work",
    response_model=MyWorkResponse,
    dependencies=[Depends(require_permission(Permission.WORK_READ))],
)
async def my_work(
    container: ContainerDep,
    principal: PrincipalDep,
    as_of: Annotated[datetime | None, Query()] = None,
) -> MyWorkResponse:
    """Return the personal work view for the authenticated user."""
    view = await container.work.personal_view(_scope(principal), as_of=as_of)
    return MyWorkResponse(
        as_of=view.as_of,
        my_work=[_summary_response(item) for item in view.my_work],
        overdue=[_summary_response(item) for item in view.overdue],
        due_soon=[_summary_response(item) for item in view.due_soon],
        waiting_for_me=[_summary_response(item) for item in view.waiting_for_me],
        waiting_on_others=[_summary_response(item) for item in view.waiting_on_others],
        counts=view.counts,
    )


@router.get(
    "/work/departments",
    response_model=DepartmentListResponse,
    dependencies=[Depends(require_permission(Permission.WORK_READ))],
)
async def list_departments(
    container: ContainerDep, principal: PrincipalDep
) -> DepartmentListResponse:
    rows = await container.layer4_repository.list_departments(principal.organization_id)
    return DepartmentListResponse(
        items=[
            DepartmentResponse(
                department_id=row.id,
                department_key=row.department_key,
                name=row.name,
                domain=row.domain.value,
                is_active=row.is_active,
            )
            for row in rows
        ]
    )


@router.get(
    "/work/departments/{department_id}/overview",
    response_model=DepartmentOverviewResponse,
    dependencies=[Depends(require_permission(Permission.WORK_READ))],
)
async def department_overview(
    department_id: uuid.UUID,
    container: ContainerDep,
    principal: PrincipalDep,
    as_of: Annotated[datetime | None, Query()] = None,
) -> DepartmentOverviewResponse:
    view = await container.work.department_view(
        _scope(principal), department_id=department_id, as_of=as_of
    )
    return DepartmentOverviewResponse(
        department_id=view.department_id,
        as_of=view.as_of,
        status_counts=view.status_counts,
        open_work=view.open_work,
        overdue=view.overdue,
        blocked=view.blocked,
        unassigned=view.unassigned,
        items=[_summary_response(item) for item in view.items],
    )


@router.get(
    "/work/executive-overview",
    response_model=ExecutiveOverviewResponse,
    dependencies=[Depends(require_permission(Permission.WORK_READ))],
)
async def executive_overview(
    container: ContainerDep,
    principal: PrincipalDep,
    as_of: Annotated[datetime | None, Query()] = None,
) -> ExecutiveOverviewResponse:
    view = await container.work.executive_view(_scope(principal), as_of=as_of)
    return ExecutiveOverviewResponse(
        as_of=view.as_of,
        critical_work=[_summary_response(item) for item in view.critical_work],
        overdue_high_priority=[_summary_response(item) for item in view.overdue_high_priority],
        blocked_items=[_summary_response(item) for item in view.blocked_items],
        counts=view.counts,
    )


@router.get(
    "/work/commitments",
    response_model=CommitmentListResponse,
    dependencies=[Depends(require_permission(Permission.WORK_READ))],
)
async def list_commitments(
    container: ContainerDep,
    principal: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    work_id: Annotated[uuid.UUID | None, Query()] = None,
    commitment_status: Annotated[str | None, Query(alias="status", max_length=30)] = None,
    commitment_type: Annotated[str | None, Query(max_length=30)] = None,
) -> CommitmentListResponse:
    rows, total = await container.layer4_repository.list_commitments(
        principal.organization_id,
        limit=limit,
        offset=offset,
        work_id=work_id,
        commitment_status=commitment_status,
        commitment_type=commitment_type,
    )
    return CommitmentListResponse(
        items=[
            CommitmentResponse(
                commitment_id=row.id,
                work_id=row.work_id,
                commitment_type=row.commitment_type.value,
                description=row.description,
                committing_party=row.committing_party,
                receiving_party=row.receiving_party,
                responsible_owner=row.responsible_owner,
                related_customer_ref=row.related_customer_ref,
                related_supplier_ref=row.related_supplier_ref,
                related_order_ref=row.related_order_ref,
                promised_at=row.promised_at,
                due_at=row.due_at,
                actual_completion_at=row.actual_completion_at,
                commitment_status=row.commitment_status.value,
                verification_status=row.verification_status.value,
                confidence=row.confidence,
                source_reference=row.source_reference,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/work/dependencies",
    response_model=DependencyListResponse,
    dependencies=[Depends(require_permission(Permission.WORK_READ))],
)
async def list_dependencies(
    container: ContainerDep,
    principal: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
    work_id: Annotated[uuid.UUID | None, Query()] = None,
) -> DependencyListResponse:
    rows = await container.layer4_repository.list_dependencies(
        principal.organization_id, work_id=work_id, limit=limit + offset
    )
    page = rows[offset : offset + limit]
    return DependencyListResponse(
        items=[
            DependencyResponse(
                dependency_id=row.id,
                source_work_id=row.source_work_id,
                target_work_id=row.target_work_id,
                target_entity_type=row.target_entity_type.value,
                target_entity_ref=row.target_entity_ref,
                relationship=row.relationship.value,
                status=row.status.value,
                origin=row.origin.value,
                verification_status=row.verification_status.value,
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
            for row in page
        ],
        total=len(rows),
        limit=limit,
        offset=offset,
    )


@router.get(
    "/work/blockers",
    response_model=BlockerListResponse,
    dependencies=[Depends(require_permission(Permission.WORK_READ))],
)
async def list_blockers(
    container: ContainerDep,
    principal: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    work_id: Annotated[uuid.UUID | None, Query()] = None,
    blocker_status: Annotated[str | None, Query(alias="status", max_length=20)] = None,
) -> BlockerListResponse:
    rows, total = await container.layer4_repository.list_blockers(
        principal.organization_id,
        limit=limit,
        offset=offset,
        work_id=work_id,
        blocker_status=blocker_status,
    )
    return BlockerListResponse(
        items=[
            BlockerResponse(
                blocker_id=row.id,
                affected_work_id=row.affected_work_id,
                blocker_type=row.blocker_type.value,
                description=row.description,
                blocking_entity=row.blocking_entity,
                responsible_department_id=row.responsible_department_id,
                detected_at=row.detected_at,
                expected_resolution_at=row.expected_resolution_at,
                blocker_status=row.blocker_status.value,
                evidence_references=row.evidence_references or [],
                provenance=row.provenance.value,
                verification_status=row.verification_status.value,
            )
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/work/followups",
    response_model=FollowUpListResponse,
    dependencies=[Depends(require_permission(Permission.WORK_READ))],
)
async def list_followups(
    container: ContainerDep,
    principal: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    work_id: Annotated[uuid.UUID | None, Query()] = None,
    follow_up_status: Annotated[str | None, Query(alias="status", max_length=20)] = None,
    target_type: Annotated[str | None, Query(max_length=20)] = None,
) -> FollowUpListResponse:
    rows, total = await container.layer4_repository.list_followups(
        principal.organization_id,
        limit=limit,
        offset=offset,
        work_id=work_id,
        follow_up_status=follow_up_status,
        target_type=target_type,
    )
    return FollowUpListResponse(
        items=[
            FollowUpResponse(
                follow_up_id=row.id,
                related_work_id=row.related_work_id,
                target_type=row.target_type.value,
                target_ref=row.target_ref,
                owner_id=row.owner_id,
                reason=row.reason,
                last_activity_at=row.last_activity_at,
                next_due_at=row.next_due_at,
                urgency=row.urgency.value,
                follow_up_status=row.follow_up_status.value,
                rule_id=row.rule_id,
                provenance=row.provenance,
            )
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/work/conflicts",
    response_model=ConflictListResponse,
    dependencies=[Depends(require_permission(Permission.WORK_READ))],
)
async def list_conflicts(
    container: ContainerDep,
    principal: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    conflict_status: Annotated[str | None, Query(alias="status", max_length=20)] = None,
    conflict_type: Annotated[str | None, Query(max_length=40)] = None,
) -> ConflictListResponse:
    rows, total = await container.layer4_repository.list_conflicts(
        principal.organization_id,
        limit=limit,
        offset=offset,
        status=conflict_status,
        conflict_type=conflict_type,
    )
    return ConflictListResponse(
        items=[
            ConflictResponse(
                conflict_id=row.id,
                work_id=row.work_id,
                entity_type=row.entity_type,
                entity_key=row.entity_key,
                conflict_type=row.conflict_type.value,
                detail=row.detail,
                candidates=row.candidates or [],
                status=row.status.value,
                resolved_value=row.resolved_value,
                resolved_by=row.resolved_by,
                resolution_note=row.resolution_note,
                detected_at=row.detected_at,
                resolved_at=row.resolved_at,
            )
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/work/conflicts/{conflict_id}/resolve",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[CSRF, Depends(require_permission(Permission.WORK_MANAGE))],
)
async def resolve_conflict(
    conflict_id: uuid.UUID,
    request: ConflictResolveRequest,
    container: ContainerDep,
    principal: PrincipalDep,
) -> None:
    """Record an explicit, audited resolution of a conflict.

    This is the only place a conflict stops being open, and it requires ``work:manage``. The
    competing candidates remain on the record.
    """
    correlation_id = get_correlation_id() or str(uuid.uuid4())
    await container.work.resolve_conflict(
        organization_id=principal.organization_id,
        conflict_id=conflict_id,
        resolved_value=request.resolved_value,
        resolution_note=request.resolution_note,
        status=request.status,
        actor_id=principal.user_id,
        correlation_id=correlation_id,
    )


@router.get(
    "/work/extractions",
    response_model=ExtractionListResponse,
    dependencies=[Depends(require_permission(Permission.WORK_READ))],
)
async def list_extractions(
    container: ContainerDep,
    principal: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    verification_status: Annotated[str | None, Query(max_length=20)] = None,
) -> ExtractionListResponse:
    rows, total = await container.layer4_repository.list_extractions(
        principal.organization_id,
        limit=limit,
        offset=offset,
        verification_status=verification_status,
    )
    return ExtractionListResponse(
        items=[
            ExtractionResponse(
                extraction_id=row.id,
                extraction_key=row.extraction_key,
                canonical_record_id=row.canonical_record_id,
                source_reference=row.source_reference,
                extraction_method=row.extraction_method,
                extraction_version=row.extraction_version,
                candidate_type=row.candidate_type.value,
                fields=row.fields or {},
                confidence=row.confidence,
                verification_status=row.verification_status.value,
                promoted_work_id=row.promoted_work_id,
                created_at=row.created_at,
            )
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/work/workflows",
    response_model=WorkflowInstanceListResponse,
    dependencies=[Depends(require_permission(Permission.WORK_READ))],
)
async def list_workflows(
    container: ContainerDep,
    principal: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    work_id: Annotated[uuid.UUID | None, Query()] = None,
) -> WorkflowInstanceListResponse:
    results = await container.layer4_repository.list_workflow_instances(
        principal.organization_id, limit=limit, offset=offset, work_id=work_id
    )
    total = await container.layer4_repository.count_workflow_instances(
        principal.organization_id, work_id=work_id
    )
    items: list[WorkflowInstanceResponse] = []
    for instance, definition, steps in results:
        if definition is None:
            continue
        items.append(
            WorkflowInstanceResponse(
                instance_id=instance.id,
                definition_id=instance.definition_id,
                work_id=instance.work_id,
                workflow_key=definition.workflow_key,
                workflow_name=definition.name,
                workflow_type=definition.workflow_type,
                version=definition.version,
                status=instance.status.value,
                current_step_key=instance.current_step_key,
                steps=[
                    WorkflowStepResponse(
                        step_key=step.step_key,
                        step_name=step.step_name,
                        step_order=step.step_order,
                        responsible_role=step.responsible_role,
                        observed_state=step.observed_state.value,
                        depends_on=[str(dep) for dep in (step.depends_on or [])],
                    )
                    for step in steps
                ],
            )
        )
    return WorkflowInstanceListResponse(items=items, total=total)


@router.post(
    "/work/run",
    response_model=WorkRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[CSRF, Depends(require_permission(Permission.WORK_MANAGE))],
)
async def run_work_derivation(
    request: WorkRunRequest,
    container: ContainerDep,
    principal: PrincipalDep,
) -> WorkRunResponse:
    """Run a bounded work derivation over the caller's tenant.

    This re-derives work from canonical records in the window. It is idempotent, so running
    it again over the same window upserts rather than duplicating.
    """
    correlation_id = get_correlation_id() or str(uuid.uuid4())
    result = await container.layer4_engine.run(
        principal.organization_id,
        correlation_id=correlation_id,
        since=request.since,
        until=request.until,
        row_limit=request.row_limit,
    )
    return WorkRunResponse(
        organization_id=result.organization_id,
        correlation_id=result.correlation_id,
        job_type=result.job_type,
        status=result.status,
        counters=result.counters,
        skips=[skip.as_metadata() for skip in result.skips],
    )


@router.get(
    "/work/{work_id}",
    response_model=WorkEntityDetailResponse,
    dependencies=[Depends(require_permission(Permission.WORK_READ))],
)
async def get_work(
    work_id: uuid.UUID, container: ContainerDep, principal: PrincipalDep
) -> WorkEntityDetailResponse:
    """Return a work entity with its references, assignments, commitments, and timeline."""
    detail = await container.work.detail(principal.organization_id, work_id)
    entity = detail.entity
    base = _entity_response(entity)
    return WorkEntityDetailResponse(
        **base.model_dump(),
        source_references=[
            WorkSourceReferenceResponse(
                canonical_record_id=ref.canonical_record_id,
                source_system=ref.source_system,
                source_record_key=ref.source_record_key,
                source_record_type=ref.source_record_type,
                relationship=ref.relationship.value,
                confidence=ref.confidence,
            )
            for ref in detail.references
        ],
        assignments=[
            {
                "participant_type": a.participant_type.value,
                "user_id": a.user_id,
                "department_id": a.department_id,
                "external_name": a.external_name,
                "role": a.role.value,
                "verification": a.verification.value,
                "confidence": a.confidence,
            }
            for a in detail.assignments
        ],
        commitments=[
            {
                "commitment_id": str(c.id),
                "commitment_type": c.commitment_type.value,
                "description": c.description,
                "committing_party": c.committing_party,
                "receiving_party": c.receiving_party,
                "due_at": c.due_at.isoformat() if c.due_at else None,
                "commitment_status": c.commitment_status.value,
                "verification_status": c.verification_status.value,
                "confidence": c.confidence,
            }
            for c in detail.commitments
        ],
        dependencies=[
            {
                "dependency_id": str(d.id),
                "relationship": d.relationship.value,
                "target_entity_type": d.target_entity_type.value,
                "target_entity_ref": d.target_entity_ref,
                "status": d.status.value,
                "verification_status": d.verification_status.value,
            }
            for d in detail.dependencies
        ],
        blockers=[
            {
                "blocker_id": str(b.id),
                "blocker_type": b.blocker_type.value,
                "description": b.description,
                "blocking_entity": b.blocking_entity,
                "blocker_status": b.blocker_status.value,
                "verification_status": b.verification_status.value,
                "evidence_references": b.evidence_references or [],
            }
            for b in detail.blockers
        ],
        priorities=[
            WorkPriorityResponse(
                level=p.level.value,
                source=p.source.value,
                overdue=p.overdue,
                due_soon=p.due_soon,
                blocked=p.blocked,
                dependency_count=p.dependency_count,
                waiting_duration_seconds=p.waiting_duration_seconds,
                commitment_status=p.commitment_status,
                source_completeness=p.source_completeness,
                related_detection_ids=[uuid.UUID(str(x)) for x in (p.related_detection_ids or [])],
                computed_at=p.computed_at,
            )
            for p in detail.priorities
        ],
        timeline=[
            {
                "event_id": str(t.id),
                "event_type": t.event_type,
                "event_at": t.event_at.isoformat(),
                "source_timestamp": t.source_timestamp.isoformat() if t.source_timestamp else None,
                "ingested_at": t.ingested_at.isoformat(),
                "processed_at": t.processed_at.isoformat(),
                "actor_type": t.actor_type,
                "previous_state": t.previous_state,
                "new_state": t.new_state,
                "provenance": t.provenance,
                "correlation_id": t.correlation_id,
            }
            for t in detail.timeline
        ],
    )


@router.get(
    "/work/{work_id}/timeline",
    response_model=WorkTimelineResponse,
    dependencies=[Depends(require_permission(Permission.WORK_READ))],
)
async def get_work_timeline(
    work_id: uuid.UUID,
    container: ContainerDep,
    principal: PrincipalDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> WorkTimelineResponse:
    rows = await container.layer4_repository.list_timeline(
        principal.organization_id, work_id, limit=limit
    )
    return WorkTimelineResponse(
        items=[
            WorkTimelineEventResponse(
                event_id=row.id,
                work_id=row.work_id,
                event_type=row.event_type,
                event_at=row.event_at,
                source_timestamp=row.source_timestamp,
                ingested_at=row.ingested_at,
                processed_at=row.processed_at,
                actor_id=row.actor_id,
                actor_type=row.actor_type,
                previous_state=row.previous_state,
                new_state=row.new_state,
                source_reference=row.source_reference,
                provenance=row.provenance,
                correlation_id=row.correlation_id,
            )
            for row in rows
        ]
    )


@router.post(
    "/work/{work_id}/status",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[CSRF, Depends(require_permission(Permission.WORK_MANAGE))],
)
async def update_work_status(
    work_id: uuid.UUID,
    request: WorkStatusUpdateRequest,
    container: ContainerDep,
    principal: PrincipalDep,
) -> None:
    """Change a work item's status, enforcing the canonical transition graph."""
    correlation_id = get_correlation_id() or str(uuid.uuid4())
    await container.work.change_status(
        organization_id=principal.organization_id,
        work_id=work_id,
        new_status=WorkStatus(request.status),
        actor_id=principal.user_id,
        correlation_id=correlation_id,
        reason=request.reason,
    )


@router.post(
    "/work/{work_id}/assign",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[CSRF, Depends(require_permission(Permission.WORK_MANAGE))],
)
async def assign_work(
    work_id: uuid.UUID,
    request: WorkAssignRequest,
    container: ContainerDep,
    principal: PrincipalDep,
) -> None:
    """Record a verified assignment of a work item."""
    correlation_id = get_correlation_id() or str(uuid.uuid4())
    await container.work.assign(
        organization_id=principal.organization_id,
        work_id=work_id,
        user_id=request.user_id,
        department_id=request.department_id,
        role=AssignmentRole(request.role),
        actor_id=principal.user_id,
        correlation_id=correlation_id,
    )
