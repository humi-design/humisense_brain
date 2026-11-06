"""Layer 4 event contracts and outbox.

Layer 2's outbox is keyed to a canonical record by a NOT NULL foreign key, so it cannot carry
a work event; Layer 3 hit the same wall and documented it. Layer 4 therefore owns
``work_outbox_events``, whose aggregate reference is the work entity.

Delivery discipline matches Layer 2:

* **Durable first.** The event row is written in the same transaction as the state change, so
  an event exists if and only if the change committed.
* **At-least-once.** Consumers must be idempotent; every event carries an ``idempotency_key``.
* **No event loop.** Publication never writes a work entity, so a published event cannot
  trigger re-derivation.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass


class WorkEventType:
    """Canonical Layer 4 output event names."""

    WORK_ENTITY_CREATED = "work.entity_created"
    WORK_ENTITY_UPDATED = "work.entity_updated"
    WORK_ASSIGNED = "work.assigned"
    WORK_STATUS_CHANGED = "work.status_changed"
    COMMITMENT_IDENTIFIED = "commitment.identified"
    COMMITMENT_UPDATED = "commitment.updated"
    DEADLINE_APPROACHING = "deadline.approaching"
    WORK_OVERDUE = "work.overdue"
    DEPENDENCY_IDENTIFIED = "dependency.identified"
    WORK_BLOCKED = "work.blocked"
    FOLLOWUP_REQUIRED = "followup.required"
    WORK_COMPLETED = "work.completed"
    WORK_CONFLICT_DETECTED = "work.conflict_detected"


ALL_EVENT_TYPES: tuple[str, ...] = (
    WorkEventType.WORK_ENTITY_CREATED,
    WorkEventType.WORK_ENTITY_UPDATED,
    WorkEventType.WORK_ASSIGNED,
    WorkEventType.WORK_STATUS_CHANGED,
    WorkEventType.COMMITMENT_IDENTIFIED,
    WorkEventType.COMMITMENT_UPDATED,
    WorkEventType.DEADLINE_APPROACHING,
    WorkEventType.WORK_OVERDUE,
    WorkEventType.DEPENDENCY_IDENTIFIED,
    WorkEventType.WORK_BLOCKED,
    WorkEventType.FOLLOWUP_REQUIRED,
    WorkEventType.WORK_COMPLETED,
    WorkEventType.WORK_CONFLICT_DETECTED,
)


@dataclass(frozen=True, slots=True)
class WorkEventDraft:
    """An event to write to the outbox in the caller's transaction."""

    event_type: str
    work_id: uuid.UUID | None
    payload: dict[str, object]
    correlation_id: str

    def idempotency_key(self, *, suffix: str = "") -> str:
        """Return a stable idempotency key for this event.

        Keyed on the work id, the event type, and an optional suffix (for example a
        commitment id) so a re-run produces the same key and the unique constraint makes
        the second write a no-op rather than a duplicate delivery.
        """
        base = f"{self.event_type}:{self.work_id or 'none'}"
        return f"{base}:{suffix}"[:300] if suffix else base[:300]


__all__ = [
    "ALL_EVENT_TYPES",
    "WorkEventDraft",
    "WorkEventType",
]
