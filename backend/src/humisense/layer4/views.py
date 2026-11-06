"""Layer 4 read models: personal, managerial, and executive.

These are read models over the same tables. None is a separate store, and all are
tenant-scoped and authorization-filtered.

Two rules govern what these views may say:

* **No fabricated metrics.** Every number is a count or a duration computed from stored
  fields. No performance score, productivity ranking, or engagement metric is produced.
* **Record-level authorization.** A view returns a work item only when the principal is
  authorized to see it: it is theirs, it belongs to their department, or their role grants
  organization-wide visibility. A name in a conversation grants nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from humisense.db.models import WorkStatus

# Roles with organization-wide work visibility. A viewer sees only their own work; an
# operator and above see their department's and the organization's, because that is what the
# managerial and executive views exist for. This is a visibility rule, not an authorization
# to change anything: writes are separately gated by ``work:manage``.
ORG_WIDE_ROLES: frozenset[str] = frozenset({"admin", "owner"})


@dataclass(frozen=True, slots=True)
class WorkScope:
    """The visibility scope for a principal."""

    organization_id: uuid.UUID
    user_id: uuid.UUID
    role: str
    department_ids: frozenset[uuid.UUID] = frozenset()

    @property
    def sees_organization(self) -> bool:
        return self.role in ORG_WIDE_ROLES

    @property
    def sees_department(self) -> bool:
        return bool(self.department_ids) or self.role in {"operator", "admin", "owner"}


@dataclass(slots=True)
class WorkItemSummary:
    """A compact work item for a list view."""

    work_id: uuid.UUID
    title: str
    work_type: str
    domain: str
    status: str
    priority: str
    due_at: datetime | None
    overdue: bool
    due_soon: bool
    blocked: bool
    owner_id: uuid.UUID | None
    department_id: uuid.UUID | None
    updated_at: datetime


@dataclass(slots=True)
class PersonalWorkView:
    """The personal work view: what one employee is looking at today."""

    organization_id: uuid.UUID
    user_id: uuid.UUID
    as_of: datetime
    my_work: list[WorkItemSummary] = field(default_factory=list)
    overdue: list[WorkItemSummary] = field(default_factory=list)
    due_soon: list[WorkItemSummary] = field(default_factory=list)
    waiting_for_me: list[WorkItemSummary] = field(default_factory=list)
    waiting_on_others: list[WorkItemSummary] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class DepartmentWorkView:
    """A department's workload for a manager."""

    organization_id: uuid.UUID
    department_id: uuid.UUID | None
    as_of: datetime
    status_counts: dict[str, int] = field(default_factory=dict)
    open_work: int = 0
    overdue: int = 0
    blocked: int = 0
    unassigned: int = 0
    items: list[WorkItemSummary] = field(default_factory=list)


@dataclass(slots=True)
class ExecutiveWorkView:
    """Cross-department critical work for an executive.

    Every entry is a fact with its supporting reference. There is no derived "risk score"
    and no employee ranking.
    """

    organization_id: uuid.UUID
    as_of: datetime
    critical_work: list[WorkItemSummary] = field(default_factory=list)
    overdue_high_priority: list[WorkItemSummary] = field(default_factory=list)
    blocked_items: list[WorkItemSummary] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


def is_overdue(*, due_at: datetime | None, status: str, as_of: datetime) -> bool:
    """Return whether work is overdue at ``as_of``.

    ``as_of`` is passed explicitly so a historical view is judged against the window it
    describes, not the wall clock. Completed and cancelled work is never overdue.
    """
    if due_at is None:
        return False
    if status in {WorkStatus.COMPLETED.value, WorkStatus.CANCELLED.value}:
        return False
    return due_at < as_of


def is_due_soon(
    *, due_at: datetime | None, status: str, as_of: datetime, within_hours: int = 24
) -> bool:
    if due_at is None:
        return False
    if status in {WorkStatus.COMPLETED.value, WorkStatus.CANCELLED.value}:
        return False
    seconds = (due_at - as_of).total_seconds()
    return 0 <= seconds <= within_hours * 3600


def visible_to(
    *,
    owner_id: uuid.UUID | None,
    assigned_user_id: uuid.UUID | None,
    department_id: uuid.UUID | None,
    scope: WorkScope,
) -> bool:
    """Return whether a work item is visible to the principal's scope.

    A viewer sees work they own or are assigned. A manager sees their department's work. An
    administrator or owner sees the whole tenant. Nothing else grants visibility.
    """
    if scope.sees_organization:
        return True
    if owner_id == scope.user_id or assigned_user_id == scope.user_id:
        return True
    return department_id is not None and department_id in scope.department_ids


__all__ = [
    "ORG_WIDE_ROLES",
    "DepartmentWorkView",
    "ExecutiveWorkView",
    "PersonalWorkView",
    "WorkItemSummary",
    "WorkScope",
    "is_due_soon",
    "is_overdue",
    "visible_to",
]
