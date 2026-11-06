"""Layer 4 application service.

Assembles the read models the API returns and performs the few authorized work-state changes
the product supports. The service is the boundary between the HTTP layer and the Layer 4
domain: the router validates input and enforces permissions, the service enforces the domain
rules (legal status transitions, audit, timeline) and never trusts a tenant id from a caller.

Every method takes the principal's ``organization_id`` explicitly and passes it to a
tenant-scoped repository call, so isolation does not depend on the handler remembering to
filter.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog

from humisense.db.models import (
    AssignmentRole,
    ParticipantType,
    VerificationStatus,
    WorkAssignment,
    WorkBlocker,
    WorkCommitment,
    WorkDependency,
    WorkEntity,
    WorkPriority,
    WorkSourceReference,
    WorkStatus,
    WorkTimelineEvent,
)
from humisense.layer4 import dependencies as dependencies_module
from humisense.layer4.errors import IllegalWorkTransition, WorkNotFound
from humisense.layer4.repository import WorkRepository
from humisense.layer4.status import assert_transition, is_terminal
from humisense.layer4.views import (
    DepartmentWorkView,
    ExecutiveWorkView,
    PersonalWorkView,
    WorkItemSummary,
    WorkScope,
    is_due_soon,
    is_overdue,
    visible_to,
)
from humisense.services.audit import (
    OUTCOME_SUCCESS,
    AuditAction,
    AuditActor,
    AuditEntry,
    AuditService,
)

logger = structlog.get_logger(__name__)

DEFAULT_DUE_SOON_HOURS = 24
_OPEN_STATUSES = frozenset(
    {
        WorkStatus.NEW,
        WorkStatus.OPEN,
        WorkStatus.ASSIGNED,
        WorkStatus.IN_PROGRESS,
        WorkStatus.WAITING,
        WorkStatus.BLOCKED,
        WorkStatus.ON_HOLD,
        WorkStatus.UNDER_REVIEW,
        WorkStatus.UNKNOWN,
    }
)


@dataclass(slots=True)
class WorkDetail:
    """A work entity with everything related to it, for the detail view."""

    entity: WorkEntity
    references: list[WorkSourceReference]
    assignments: list[WorkAssignment]
    commitments: list[WorkCommitment]
    dependencies: list[WorkDependency]
    blockers: list[WorkBlocker]
    priorities: list[WorkPriority]
    timeline: list[WorkTimelineEvent]


@dataclass(slots=True)
class WorkStatusChange:
    work_id: uuid.UUID
    from_status: WorkStatus
    to_status: WorkStatus
    actor_id: uuid.UUID | None


class WorkService:
    """Read models and authorized work-state changes for one tenant per call."""

    def __init__(self, repository: WorkRepository, audit: AuditService) -> None:
        self._repository = repository
        self._audit = audit

    # --- Reads ------------------------------------------------------------

    async def personal_view(
        self, scope: WorkScope, *, as_of: datetime | None = None, limit: int = 50
    ) -> PersonalWorkView:
        """Build the personal work view for a principal.

        Only work the principal owns or is assigned, or that belongs to a department they are
        in scope for, is included. A name in a conversation grants nothing.
        """
        reference = as_of or datetime.now(tz=UTC)
        rows, _ = await self._repository.list_work_entities(scope.organization_id, limit=200)
        view = PersonalWorkView(
            organization_id=scope.organization_id, user_id=scope.user_id, as_of=reference
        )
        for row in rows:
            if not visible_to(
                owner_id=row.owner_id,
                assigned_user_id=row.assigned_user_id,
                department_id=row.department_id,
                scope=scope,
            ):
                continue
            summary = _summarize(row, as_of=reference)
            if row.owner_id == scope.user_id or row.assigned_user_id == scope.user_id:
                view.my_work.append(summary)
                if summary.overdue:
                    view.overdue.append(summary)
                if summary.due_soon:
                    view.due_soon.append(summary)
                if summary.status == WorkStatus.WAITING.value:
                    view.waiting_on_others.append(summary)
            if summary.status == WorkStatus.BLOCKED.value and (
                row.owner_id == scope.user_id or row.assigned_user_id == scope.user_id
            ):
                view.waiting_for_me.append(summary)
        view.my_work = view.my_work[:limit]
        view.overdue = view.overdue[:limit]
        view.due_soon = view.due_soon[:limit]
        view.waiting_for_me = view.waiting_for_me[:limit]
        view.waiting_on_others = view.waiting_on_others[:limit]
        view.counts = {
            "my_work": len(view.my_work),
            "overdue": len(view.overdue),
            "due_soon": len(view.due_soon),
            "waiting_for_me": len(view.waiting_for_me),
            "waiting_on_others": len(view.waiting_on_others),
        }
        return view

    async def department_view(
        self,
        scope: WorkScope,
        *,
        department_id: uuid.UUID | None = None,
        as_of: datetime | None = None,
        limit: int = 50,
    ) -> DepartmentWorkView:
        """Build a department's workload view.

        A non-administrator may only request a department they are in scope for; requesting
        another department yields an empty view rather than an error, so the endpoint does
        not disclose the existence of other departments.
        """
        reference = as_of or datetime.now(tz=UTC)
        effective_department = department_id
        if not scope.sees_organization:
            if department_id is not None and department_id not in scope.department_ids:
                return DepartmentWorkView(
                    organization_id=scope.organization_id,
                    department_id=department_id,
                    as_of=reference,
                )
            if department_id is None and len(scope.department_ids) == 1:
                effective_department = next(iter(scope.department_ids))

        rows, _ = await self._repository.list_work_entities(
            scope.organization_id, limit=500, department_id=effective_department
        )
        view = DepartmentWorkView(
            organization_id=scope.organization_id,
            department_id=effective_department,
            as_of=reference,
        )
        status_counts: dict[str, int] = {}
        for row in rows:
            summary = _summarize(row, as_of=reference)
            status_counts[row.status.value] = status_counts.get(row.status.value, 0) + 1
            if row.status in _OPEN_STATUSES:
                view.open_work += 1
            if summary.overdue:
                view.overdue += 1
            if summary.blocked:
                view.blocked += 1
            if row.owner_id is None and row.assigned_user_id is None:
                view.unassigned += 1
        view.status_counts = status_counts
        view.items = [_summarize(row, as_of=reference) for row in rows[:limit]]
        return view

    async def executive_view(
        self, scope: WorkScope, *, as_of: datetime | None = None, limit: int = 50
    ) -> ExecutiveWorkView:
        """Build the cross-department executive view.

        Only an administrator or owner may see the whole organization's critical work; for
        anyone else the view is scoped to what they may already see. No risk score and no
        employee ranking is produced.
        """
        reference = as_of or datetime.now(tz=UTC)
        rows, _ = await self._repository.list_work_entities(scope.organization_id, limit=500)
        view = ExecutiveWorkView(organization_id=scope.organization_id, as_of=reference)
        counts = {"critical": 0, "overdue": 0, "blocked": 0}
        for row in rows:
            if not visible_to(
                owner_id=row.owner_id,
                assigned_user_id=row.assigned_user_id,
                department_id=row.department_id,
                scope=scope,
            ):
                continue
            summary = _summarize(row, as_of=reference)
            if row.priority.value in {"urgent", "critical"}:
                view.critical_work.append(summary)
                counts["critical"] += 1
            if summary.overdue and row.priority.value in {"high", "urgent", "critical"}:
                view.overdue_high_priority.append(summary)
                counts["overdue"] += 1
            if summary.blocked:
                view.blocked_items.append(summary)
                counts["blocked"] += 1
        view.critical_work = view.critical_work[:limit]
        view.overdue_high_priority = view.overdue_high_priority[:limit]
        view.blocked_items = view.blocked_items[:limit]
        view.counts = counts
        return view

    async def detail(self, organization_id: uuid.UUID, work_id: uuid.UUID) -> WorkDetail:
        """Return a work entity and everything related to it."""
        entity = await self._repository.get_work_entity(organization_id, work_id)
        if entity is None:
            raise WorkNotFound(detail="work entity not found")
        commitments, _ = await self._repository.list_commitments(
            organization_id, work_id=work_id, limit=100
        )
        blockers, _ = await self._repository.list_blockers(
            organization_id, work_id=work_id, limit=100
        )
        return WorkDetail(
            entity=entity,
            references=await self._repository.list_source_references(organization_id, work_id),
            assignments=await self._repository.list_assignments(organization_id, work_id),
            commitments=commitments,
            dependencies=await self._repository.list_dependencies(organization_id, work_id=work_id),
            blockers=blockers,
            priorities=await self._repository.list_priorities(organization_id, work_id),
            timeline=await self._repository.list_timeline(organization_id, work_id),
        )

    async def dependency_cycles(
        self, organization_id: uuid.UUID, *, work_id: uuid.UUID | None = None
    ) -> list[list[uuid.UUID]]:
        """Return dependency cycles, for the conflict surface."""
        rows = await self._repository.list_dependencies(organization_id, work_id=work_id)
        edges = [
            dependencies_module.DependencyEdge(
                source_work_id=row.source_work_id,
                target_work_id=row.target_work_id,
                relationship=row.relationship,
            )
            for row in rows
            if row.target_work_id is not None
        ]
        return dependencies_module.find_cycles(edges)

    # --- Authorized writes -----------------------------------------------

    async def change_status(
        self,
        *,
        organization_id: uuid.UUID,
        work_id: uuid.UUID,
        new_status: WorkStatus,
        actor_id: uuid.UUID,
        correlation_id: str,
        reason: str | None = None,
    ) -> WorkStatusChange:
        """Change a work item's status, enforcing the transition graph.

        An illegal transition raises :class:`IllegalWorkTransition`; the change is recorded on
        the append-only status history and the timeline, and audited. Completion is never
        inferred: it happens only when a caller requests it deliberately.
        """
        entity = await self._repository.get_work_entity(organization_id, work_id)
        if entity is None:
            raise WorkNotFound(detail="work entity not found")
        previous = entity.status
        assert_transition(previous, new_status)

        values: dict[str, object] = {"status": new_status}
        if new_status == WorkStatus.COMPLETED:
            values["completed_at"] = datetime.now(tz=UTC)
        await self._repository.update_work_entity(organization_id, work_id, values)

        await self._repository.append_status_history(
            organization_id,
            {
                "work_id": work_id,
                "from_status": previous.value,
                "to_status": new_status.value,
                "actor_id": actor_id,
                "reason": reason,
                "correlation_id": correlation_id,
            },
        )
        await self._repository.append_timeline_event(
            organization_id,
            {
                "work_id": work_id,
                "event_type": "work.status_changed",
                "event_at": datetime.now(tz=UTC),
                "ingested_at": datetime.now(tz=UTC),
                "processed_at": datetime.now(tz=UTC),
                "actor_id": actor_id,
                "actor_type": "user",
                "previous_state": {"status": previous.value},
                "new_state": {"status": new_status.value},
                "provenance": "manual",
                "correlation_id": correlation_id,
            },
        )
        await self._repository.enqueue_event(
            organization_id,
            {
                "work_id": work_id,
                "event_type": "work.status_changed",
                "idempotency_key": (
                    f"work.status_changed:{work_id}:{new_status.value}:{correlation_id}"
                ),
                "payload": {"from": previous.value, "to": new_status.value},
                "correlation_id": correlation_id,
            },
        )
        await self._audit.record(
            AuditActor(
                organization_id=organization_id, actor_id=actor_id, correlation_id=correlation_id
            ),
            AuditEntry(
                action=AuditAction.WORK_STATUS_CHANGED,
                outcome=OUTCOME_SUCCESS,
                target_type="work_entity",
                target_id=str(work_id),
                metadata={"from": previous.value, "to": new_status.value},
            ),
        )
        return WorkStatusChange(
            work_id=work_id, from_status=previous, to_status=new_status, actor_id=actor_id
        )

    async def assign(
        self,
        *,
        organization_id: uuid.UUID,
        work_id: uuid.UUID,
        user_id: uuid.UUID | None,
        department_id: uuid.UUID | None,
        role: AssignmentRole,
        actor_id: uuid.UUID,
        correlation_id: str,
    ) -> None:
        """Record a verified assignment.

        A manual assignment is ``VERIFIED`` because an authorized user made it explicitly;
        this is the one path that establishes ownership without a source field.
        """
        entity = await self._repository.get_work_entity(organization_id, work_id)
        if entity is None:
            raise WorkNotFound(detail="work entity not found")
        participant_type = (
            ParticipantType.USER if user_id is not None else ParticipantType.DEPARTMENT
        )
        await self._repository.upsert_assignment(
            organization_id,
            {
                "work_id": work_id,
                "participant_type": participant_type,
                "user_id": user_id,
                "department_id": department_id,
                "role": role,
                "verification": VerificationStatus.VERIFIED,
                "confidence": 1.0,
                "correlation_id": correlation_id,
            },
        )
        values: dict[str, object] = {}
        if role == AssignmentRole.OWNER and user_id is not None:
            values["owner_id"] = user_id
        if role == AssignmentRole.ASSIGNEE and user_id is not None:
            values["assigned_user_id"] = user_id
        if values:
            await self._repository.update_work_entity(organization_id, work_id, values)
        await self._repository.append_timeline_event(
            organization_id,
            {
                "work_id": work_id,
                "event_type": "work.assigned",
                "event_at": datetime.now(tz=UTC),
                "ingested_at": datetime.now(tz=UTC),
                "processed_at": datetime.now(tz=UTC),
                "actor_id": actor_id,
                "actor_type": "user",
                "new_state": {"role": role.value, "user_id": str(user_id) if user_id else None},
                "provenance": "manual",
                "correlation_id": correlation_id,
            },
        )
        await self._audit.record(
            AuditActor(
                organization_id=organization_id, actor_id=actor_id, correlation_id=correlation_id
            ),
            AuditEntry(
                action=AuditAction.WORK_ASSIGNED,
                outcome=OUTCOME_SUCCESS,
                target_type="work_entity",
                target_id=str(work_id),
                metadata={"role": role.value},
            ),
        )

    async def resolve_conflict(
        self,
        *,
        organization_id: uuid.UUID,
        conflict_id: uuid.UUID,
        resolved_value: dict[str, object] | None,
        resolution_note: str | None,
        status: str,
        actor_id: uuid.UUID,
        correlation_id: str,
    ) -> None:
        """Record an explicit conflict resolution.

        Resolution is a deliberate, audited act. The candidates remain on the record, so the
        disagreement that was reviewed is still visible afterwards.
        """
        row = await self._repository.resolve_conflict(
            organization_id,
            conflict_id,
            resolved_value=resolved_value,
            resolved_by=actor_id,
            resolution_note=resolution_note,
            status=status,
        )
        if row is None:
            raise WorkNotFound(detail="conflict not found")
        await self._audit.record(
            AuditActor(
                organization_id=organization_id, actor_id=actor_id, correlation_id=correlation_id
            ),
            AuditEntry(
                action=AuditAction.WORK_CONFLICT_RESOLVED,
                outcome=OUTCOME_SUCCESS,
                target_type="work_conflict",
                target_id=str(conflict_id),
                metadata={"status": status},
            ),
        )


def _summarize(row: WorkEntity, *, as_of: datetime) -> WorkItemSummary:
    status_value = row.status.value
    return WorkItemSummary(
        work_id=row.id,
        title=row.title,
        work_type=row.work_type.value,
        domain=row.domain.value,
        status=status_value,
        priority=row.priority.value,
        due_at=row.due_at,
        overdue=is_overdue(due_at=row.due_at, status=status_value, as_of=as_of),
        due_soon=is_due_soon(
            due_at=row.due_at,
            status=status_value,
            as_of=as_of,
            within_hours=DEFAULT_DUE_SOON_HOURS,
        ),
        blocked=status_value in {WorkStatus.BLOCKED.value, WorkStatus.WAITING.value},
        owner_id=row.owner_id,
        department_id=row.department_id,
        updated_at=row.updated_at,
    )


__all__ = [
    "IllegalWorkTransition",
    "WorkDetail",
    "WorkService",
    "WorkStatusChange",
    "is_terminal",
]
