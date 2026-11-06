"""Canonical work status model and the legal transition graph.

Two rules are encoded here rather than left to application discipline:

* **Illegal transitions raise.** The graph is closed and explicit, so an accidental
  ``COMPLETED -> NEW`` is a bug that surfaces immediately.
* **Completion is never inferred.** The only path into ``COMPLETED`` is a transition the
  caller makes deliberately from a working state; nothing here advances a status because
  time passed or a source record disappeared.

Source systems do not share status names, so a per-tenant :class:`SourceStatusMapping`
translates them. An unmapped source status becomes ``UNKNOWN`` — never a guess.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from humisense.db.models import WorkStatus
from humisense.layer4.errors import IllegalWorkTransition

TERMINAL_STATUSES: frozenset[WorkStatus] = frozenset({WorkStatus.COMPLETED, WorkStatus.CANCELLED})
"""Statuses with no automated outgoing transition."""

ACTIVE_STATUSES: frozenset[WorkStatus] = frozenset(
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

_TRANSITIONS: dict[WorkStatus, frozenset[WorkStatus]] = {
    WorkStatus.NEW: frozenset(
        {WorkStatus.OPEN, WorkStatus.ASSIGNED, WorkStatus.CANCELLED, WorkStatus.UNKNOWN}
    ),
    WorkStatus.OPEN: frozenset(
        {
            WorkStatus.ASSIGNED,
            WorkStatus.IN_PROGRESS,
            WorkStatus.WAITING,
            WorkStatus.BLOCKED,
            WorkStatus.ON_HOLD,
            WorkStatus.UNDER_REVIEW,
            WorkStatus.CANCELLED,
            WorkStatus.UNKNOWN,
        }
    ),
    WorkStatus.ASSIGNED: frozenset(
        {
            WorkStatus.IN_PROGRESS,
            WorkStatus.WAITING,
            WorkStatus.BLOCKED,
            WorkStatus.ON_HOLD,
            WorkStatus.UNDER_REVIEW,
            WorkStatus.COMPLETED,
            WorkStatus.CANCELLED,
            WorkStatus.UNKNOWN,
        }
    ),
    WorkStatus.IN_PROGRESS: frozenset(
        {
            WorkStatus.WAITING,
            WorkStatus.BLOCKED,
            WorkStatus.ON_HOLD,
            WorkStatus.UNDER_REVIEW,
            WorkStatus.COMPLETED,
            WorkStatus.CANCELLED,
            WorkStatus.UNKNOWN,
        }
    ),
    WorkStatus.WAITING: frozenset(
        {
            WorkStatus.IN_PROGRESS,
            WorkStatus.BLOCKED,
            WorkStatus.ON_HOLD,
            WorkStatus.UNDER_REVIEW,
            WorkStatus.COMPLETED,
            WorkStatus.CANCELLED,
            WorkStatus.UNKNOWN,
        }
    ),
    WorkStatus.BLOCKED: frozenset(
        {
            WorkStatus.IN_PROGRESS,
            WorkStatus.WAITING,
            WorkStatus.ON_HOLD,
            WorkStatus.UNDER_REVIEW,
            WorkStatus.COMPLETED,
            WorkStatus.CANCELLED,
            WorkStatus.UNKNOWN,
        }
    ),
    WorkStatus.ON_HOLD: frozenset(
        {
            WorkStatus.IN_PROGRESS,
            WorkStatus.WAITING,
            WorkStatus.BLOCKED,
            WorkStatus.UNDER_REVIEW,
            WorkStatus.COMPLETED,
            WorkStatus.CANCELLED,
            WorkStatus.UNKNOWN,
        }
    ),
    WorkStatus.UNDER_REVIEW: frozenset(
        {
            WorkStatus.IN_PROGRESS,
            WorkStatus.WAITING,
            WorkStatus.BLOCKED,
            WorkStatus.COMPLETED,
            WorkStatus.CANCELLED,
            WorkStatus.UNKNOWN,
        }
    ),
    # A completed item may only be reopened deliberately, by an authorized user.
    WorkStatus.COMPLETED: frozenset({WorkStatus.IN_PROGRESS}),
    WorkStatus.CANCELLED: frozenset(),
    WorkStatus.UNKNOWN: frozenset(
        {
            WorkStatus.OPEN,
            WorkStatus.ASSIGNED,
            WorkStatus.IN_PROGRESS,
            WorkStatus.CANCELLED,
        }
    ),
}


def can_transition(source: WorkStatus, target: WorkStatus) -> bool:
    """Return whether ``source -> target`` is legal. A self-transition is not a change."""
    if source == target:
        return False
    return target in _TRANSITIONS.get(source, frozenset())


def assert_transition(source: WorkStatus, target: WorkStatus) -> None:
    """Raise :class:`IllegalWorkTransition` unless ``source -> target`` is legal."""
    if not can_transition(source, target):
        raise IllegalWorkTransition(source.value, target.value)


def allowed_targets(source: WorkStatus) -> frozenset[WorkStatus]:
    return _TRANSITIONS.get(source, frozenset())


def is_terminal(status: WorkStatus) -> bool:
    return status in TERMINAL_STATUSES


# --- Source status mapping -------------------------------------------------------

# A conservative, source-agnostic default mapping. It recognises the vocabulary the
# platform's own connectors and normalizers actually emit, plus common synonyms. Anything
# not present maps to UNKNOWN, because a wrong status is worse than an honest "unknown".
#
# The mapping is intentionally narrow: it never maps a source status to COMPLETED unless
# the source explicitly says the work is done. That is what keeps "the email thread stopped"
# from being read as "the task is complete".
DEFAULT_SOURCE_STATUS_MAP: dict[str, WorkStatus] = {
    # New / not started
    "new": WorkStatus.NEW,
    "created": WorkStatus.NEW,
    "received": WorkStatus.NEW,
    "submitted": WorkStatus.NEW,
    "draft": WorkStatus.NEW,
    "planned": WorkStatus.NEW,
    "not_started": WorkStatus.NEW,
    # Open
    "open": WorkStatus.OPEN,
    "todo": WorkStatus.OPEN,
    "to_do": WorkStatus.OPEN,
    "pending": WorkStatus.OPEN,
    "backlog": WorkStatus.OPEN,
    # Assigned
    "assigned": WorkStatus.ASSIGNED,
    "allocated": WorkStatus.ASSIGNED,
    # In progress
    "in_progress": WorkStatus.IN_PROGRESS,
    "in-progress": WorkStatus.IN_PROGRESS,
    "started": WorkStatus.IN_PROGRESS,
    "active": WorkStatus.IN_PROGRESS,
    "working": WorkStatus.IN_PROGRESS,
    "processing": WorkStatus.IN_PROGRESS,
    # Waiting
    "waiting": WorkStatus.WAITING,
    "waiting_for": WorkStatus.WAITING,
    "on_hold_external": WorkStatus.WAITING,
    "awaiting": WorkStatus.WAITING,
    "awaiting_response": WorkStatus.WAITING,
    # Blocked
    "blocked": WorkStatus.BLOCKED,
    "impeded": WorkStatus.BLOCKED,
    "stuck": WorkStatus.BLOCKED,
    # On hold
    "on_hold": WorkStatus.ON_HOLD,
    "on-hold": WorkStatus.ON_HOLD,
    "paused": WorkStatus.ON_HOLD,
    "suspended": WorkStatus.ON_HOLD,
    # Under review
    "under_review": WorkStatus.UNDER_REVIEW,
    "in_review": WorkStatus.UNDER_REVIEW,
    "review": WorkStatus.UNDER_REVIEW,
    "approval_pending": WorkStatus.UNDER_REVIEW,
    "verifying": WorkStatus.UNDER_REVIEW,
    # Completed — only an explicit completion status.
    "completed": WorkStatus.COMPLETED,
    "complete": WorkStatus.COMPLETED,
    "done": WorkStatus.COMPLETED,
    "closed": WorkStatus.COMPLETED,
    "finished": WorkStatus.COMPLETED,
    "resolved": WorkStatus.COMPLETED,
    # Cancelled
    "cancelled": WorkStatus.CANCELLED,
    "canceled": WorkStatus.CANCELLED,
    "rejected": WorkStatus.CANCELLED,
    "abandoned": WorkStatus.CANCELLED,
    "void": WorkStatus.CANCELLED,
}


def normalize_source_status(value: str | None) -> str:
    """Return a lookup key for a source status: lowercase, trimmed, spaces to underscores."""
    if value is None:
        return ""
    return value.strip().lower().replace(" ", "_").replace("-", "_")


@dataclass(frozen=True, slots=True)
class SourceStatusMapping:
    """Maps a source system's status vocabulary onto canonical statuses.

    A tenant may override the default for a given ``(source_system, source_status)`` pair.
    The original source status is always preserved on the work entity; this only decides the
    canonical interpretation.
    """

    overrides: dict[tuple[str, str], WorkStatus] = field(default_factory=dict)
    defaults: dict[str, WorkStatus] = field(default_factory=lambda: dict(DEFAULT_SOURCE_STATUS_MAP))

    def map(self, *, source_system: str, source_status: str | None) -> WorkStatus:
        """Return the canonical status for a source status.

        Unknown or missing statuses map to ``UNKNOWN`` rather than a guess.
        """
        key = normalize_source_status(source_status)
        if not key:
            return WorkStatus.UNKNOWN
        override = self.overrides.get((source_system, key))
        if override is not None:
            return override
        return self.defaults.get(key, WorkStatus.UNKNOWN)


__all__ = [
    "ACTIVE_STATUSES",
    "DEFAULT_SOURCE_STATUS_MAP",
    "TERMINAL_STATUSES",
    "SourceStatusMapping",
    "allowed_targets",
    "assert_transition",
    "can_transition",
    "is_terminal",
    "normalize_source_status",
]
