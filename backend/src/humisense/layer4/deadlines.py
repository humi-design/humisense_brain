"""Deadline and due-date intelligence.

Overdue status is computed deterministically from verified timestamps. The layer does not
invent holidays, working hours, or SLA definitions: those are configuration hooks, and the
absence of a configured business calendar is stated rather than papered over.

The critical rule mirrors Layer 3's freshness rule: **the reference time is passed in
explicitly**. A historical query is judged against the window end it asks about, not the
wall clock, so a backfill is not reported as universally overdue.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

DEFAULT_DUE_SOON_HOURS = 24


@dataclass(frozen=True, slots=True)
class BusinessCalendar:
    """A placeholder for an organization-specific business calendar.

    Layer 4 does **not** invent holidays or working hours. When no calendar is configured,
    ``has_calendar`` is False and overdue arithmetic is plain wall-clock arithmetic, which
    the API surfaces as a limitation rather than hiding it.

    A future policy layer may populate ``non_working_days`` and ``working_hours``; the
    dataclass exists so the call sites do not have to change when it does.
    """

    non_working_days: frozenset[date] = frozenset()
    working_hours: tuple[int, int] | None = None

    @property
    def has_calendar(self) -> bool:
        return bool(self.non_working_days) or self.working_hours is not None


@dataclass(frozen=True, slots=True)
class DeadlineAssessment:
    """The deterministic deadline indicators for one due date."""

    due_at: datetime | None
    overdue: bool
    due_today: bool
    due_soon: bool
    seconds_until_due: int | None
    is_terminal: bool
    """Whether the work is already completed or cancelled, which suppresses overdue."""


def assess_deadline(
    *,
    due_at: datetime | None,
    as_of: datetime,
    is_terminal: bool,
    due_soon_hours: int = DEFAULT_DUE_SOON_HOURS,
) -> DeadlineAssessment:
    """Return the deterministic deadline indicators.

    Args:
        due_at: The verified due timestamp, or None when no deadline is known.
        as_of: The reference time. Callers pass the window end for a historical query.
        is_terminal: True when the work is completed or cancelled.
        due_soon_hours: The policy window for the "due soon" indicator.
    """
    if due_at is None:
        return DeadlineAssessment(
            due_at=None,
            overdue=False,
            due_today=False,
            due_soon=False,
            seconds_until_due=None,
            is_terminal=is_terminal,
        )

    reference = as_of.astimezone(UTC)
    due = due_at.astimezone(UTC)
    delta = due - reference
    seconds = int(delta.total_seconds())

    overdue = (not is_terminal) and due < reference
    due_today = (not is_terminal) and due.date() == reference.date()
    due_soon = (not is_terminal) and 0 <= seconds <= due_soon_hours * 3600

    return DeadlineAssessment(
        due_at=due,
        overdue=overdue,
        due_today=due_today,
        due_soon=due_soon,
        seconds_until_due=seconds,
        is_terminal=is_terminal,
    )


def is_approaching(
    *, due_at: datetime | None, as_of: datetime, within_hours: int = DEFAULT_DUE_SOON_HOURS
) -> bool:
    """Return whether a due date is within ``within_hours`` of ``as_of`` and not past."""
    if due_at is None:
        return False
    reference = as_of.astimezone(UTC)
    seconds = int((due_at.astimezone(UTC) - reference).total_seconds())
    return 0 <= seconds <= within_hours * 3600


def waiting_duration(*, since: datetime | None, as_of: datetime) -> int | None:
    """Return the whole seconds a work item has been waiting, or None when unknown."""
    if since is None:
        return None
    seconds = int((as_of.astimezone(UTC) - since.astimezone(UTC)).total_seconds())
    return max(seconds, 0)


def coerce_aware(value: datetime | None) -> datetime | None:
    """Return a timezone-aware UTC datetime, or None.

    Naive datetimes are refused rather than assumed to be UTC, because assuming would
    silently shift every deadline in a deployment whose source omits the offset.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("naive datetime refused; source timestamps must carry an offset")
    return value.astimezone(UTC)


def shift(due_at: datetime, *, hours: int = 0, days: int = 0) -> datetime:
    """Return ``due_at`` shifted by a relative amount, for RELATIVE deadlines."""
    return due_at.astimezone(UTC) + timedelta(hours=hours, days=days)


__all__ = [
    "DEFAULT_DUE_SOON_HOURS",
    "BusinessCalendar",
    "DeadlineAssessment",
    "assess_deadline",
    "coerce_aware",
    "is_approaching",
    "shift",
    "waiting_duration",
]
