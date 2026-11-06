"""Layer 4 derivation policy.

Every knob here has a documented default and an explicit reason. Nothing in the policy
invents business facts: a policy selects *how* Layer 4 interprets data it already has, never
*what* the data says.

The policy is per tenant and may be loaded from configuration. It is deliberately small and
serializable so it can be stored and audited.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from humisense.db.models import DeadlineType, WorkConflictType, WorkType
from humisense.layer4.deadlines import DEFAULT_DUE_SOON_HOURS

# Layer 2 record types (``canonical_records.record_type``) that are explicitly a task, and so
# derive an ``EXPLICIT`` work item. The list is narrow on purpose: a record type is only here
# if the source stated it is a task.
EXPLICIT_TASK_RECORD_TYPES: frozenset[str] = frozenset(
    {"task", "work_item", "activity", "todo", "action_item"}
)

# Record types that map to a specific work type when derived. A record type not present here
# derives a ``GENERAL_WORK_ITEM``.
RECORD_TYPE_WORK_TYPE: dict[str, WorkType] = {
    "task": WorkType.TASK,
    "work_item": WorkType.GENERAL_WORK_ITEM,
    "activity": WorkType.GENERAL_WORK_ITEM,
    "todo": WorkType.TASK,
    "action_item": WorkType.TASK,
    "purchase_order": WorkType.PURCHASE_REQUIREMENT,
    "purchase_requirement": WorkType.PURCHASE_REQUIREMENT,
    "production_record": WorkType.PRODUCTION_REQUIREMENT,
    "production_requirement": WorkType.PRODUCTION_REQUIREMENT,
    "maintenance_event": WorkType.MAINTENANCE_ACTIVITY,
    "maintenance_activity": WorkType.MAINTENANCE_ACTIVITY,
    "quality_issue": WorkType.QUALITY_ISSUE,
    "non_conformance": WorkType.QUALITY_ISSUE,
    "approval": WorkType.APPROVAL,
    "document_request": WorkType.DOCUMENT_REQUEST,
    "invoice": WorkType.FINANCIAL_REVIEW,
    "payment": WorkType.FINANCIAL_REVIEW,
    "payment_obligation": WorkType.FINANCIAL_REVIEW,
    "delivery": WorkType.DELIVERY_OBLIGATION,
    "delivery_obligation": WorkType.DELIVERY_OBLIGATION,
    "supplier_commitment": WorkType.SUPPLIER_COMMITMENT,
    "customer_commitment": WorkType.CUSTOMER_COMMITMENT,
    "follow_up": WorkType.FOLLOW_UP,
    "followup": WorkType.FOLLOW_UP,
    "operational_issue": WorkType.OPERATIONAL_ISSUE,
    "project_activity": WorkType.PROJECT_ACTIVITY,
    "project_task": WorkType.PROJECT_ACTIVITY,
}

# Payload keys a structured record may use to state a due date, in priority order. Only an
# explicit due/required/committed key is treated as a deadline; a generic ``date`` is not.
DUE_DATE_KEYS: tuple[tuple[str, DeadlineType], ...] = (
    ("due_at", DeadlineType.SOURCE_PROVIDED),
    ("due_date", DeadlineType.SOURCE_PROVIDED),
    ("deadline", DeadlineType.SOURCE_PROVIDED),
    ("required_by", DeadlineType.SOURCE_PROVIDED),
    ("contractual_due_at", DeadlineType.CONTRACTUAL),
    ("promised_at", DeadlineType.ABSOLUTE),
    ("requested_due_at", DeadlineType.REQUESTED),
    ("estimated_completion_at", DeadlineType.ESTIMATED),
)

# A record type is only derived into work when it is one of these, unless the tenant widens
# the set. This is what stops every email and document from becoming a task.
DERIVABLE_RECORD_TYPES: frozenset[str] = (
    frozenset(RECORD_TYPE_WORK_TYPE) | EXPLICIT_TASK_RECORD_TYPES
)


@dataclass(slots=True)
class WorkPolicy:
    """Per-tenant derivation policy."""

    due_soon_hours: int = DEFAULT_DUE_SOON_HOURS
    """The window for the ``due_soon`` indicator and approaching-deadline events."""

    response_window_hours: int = 72
    """How long a communication may go unanswered before a follow-up is suggested."""

    unresolved_window_hours: int = 336
    """How long work may stay unstarted or blocked before a follow-up is suggested."""

    derivable_record_types: frozenset[str] = field(default_factory=lambda: DERIVABLE_RECORD_TYPES)
    """Layer 2 record types eligible for derivation into work entities."""

    explicit_task_record_types: frozenset[str] = field(
        default_factory=lambda: EXPLICIT_TASK_RECORD_TYPES
    )
    """Record types that produce an ``EXPLICIT`` work item rather than an ``INFERRED`` one."""

    auto_promote_extractions: bool = False
    """Whether an extractor's candidates may be promoted without review. Off by default."""

    extraction_promotion_threshold: float | None = None
    """When set with ``auto_promote_extractions``, the minimum confidence to promote."""

    source_authority: dict[WorkConflictType, str] = field(default_factory=dict)
    """Explicit per-conflict-type source authority. Empty means "never auto-resolve"."""

    max_derivations_per_run: int = 5000
    """A bound on how many source records one derivation run will process."""

    def is_derivable(self, record_type: str) -> bool:
        return record_type.lower() in self.derivable_record_types

    def work_type_for(self, record_type: str) -> WorkType:
        return RECORD_TYPE_WORK_TYPE.get(record_type.lower(), WorkType.GENERAL_WORK_ITEM)

    def origin_for(self, record_type: str) -> str:
        if record_type.lower() in self.explicit_task_record_types:
            return "explicit"
        return "inferred"


__all__ = [
    "DERIVABLE_RECORD_TYPES",
    "DUE_DATE_KEYS",
    "EXPLICIT_TASK_RECORD_TYPES",
    "RECORD_TYPE_WORK_TYPE",
    "WorkPolicy",
]
