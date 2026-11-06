"""Follow-up intelligence.

Follow-ups are produced by **documented deterministic rules**, each with a stable ``rule_id``
so a follow-up can be explained and reproduced. The rules use facts a reader can check: the
last activity time, an explicit response deadline, a commitment date, a work status, and a
dependency state.

Two distinctions are preserved:

* A ``SUGGESTED`` follow-up (a rule fired but nothing is late yet) is distinct from an
  ``OVERDUE`` one.
* Layer 4 never sends anything. It identifies that a follow-up is warranted; acting on it
  belongs to later layers and requires authorization.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from humisense.db.models import FollowUpStatus, FollowUpTargetType, Urgency
from humisense.layer4.deadlines import DEFAULT_DUE_SOON_HOURS

# Stable rule identifiers. A follow-up records which of these produced it.
RULE_NO_RESPONSE = "no_response_within_window"
RULE_RESPONSE_OVERDUE = "response_deadline_passed"
RULE_COMMITMENT_APPROACHING = "commitment_due_soon"
RULE_COMMITMENT_MISSED = "commitment_past_due"
RULE_WORK_UNRESOLVED = "work_unresolved_too_long"
RULE_BLOCKED_TOO_LONG = "blocked_too_long"


@dataclass(frozen=True, slots=True)
class FollowUpCandidate:
    """A follow-up the rules determined is warranted."""

    rule_id: str
    reason: str
    target_type: FollowUpTargetType
    target_ref: str
    urgency: Urgency
    next_due_at: datetime | None
    last_activity_at: datetime | None
    status: FollowUpStatus = FollowUpStatus.SUGGESTED


@dataclass(frozen=True, slots=True)
class FollowUpInputs:
    """The deterministic inputs to the follow-up rules."""

    related_work_id: uuid.UUID
    target_type: FollowUpTargetType
    target_ref: str
    last_activity_at: datetime | None = None
    response_due_at: datetime | None = None
    commitment_due_at: datetime | None = None
    work_status: str = "unknown"
    blocked_since: datetime | None = None
    as_of: datetime | None = None


def evaluate_followups(
    inputs: FollowUpInputs,
    *,
    response_window_hours: int = 72,
    due_soon_hours: int = DEFAULT_DUE_SOON_HOURS,
    unresolved_window_hours: int = 336,
) -> list[FollowUpCandidate]:
    """Return the follow-ups the deterministic rules determine are warranted.

    Every rule is documented in its ``reason`` and keyed by a stable ``rule_id``. No rule
    depends on text or a model.
    """
    as_of = inputs.as_of
    if as_of is None:
        raise ValueError("evaluate_followups requires an explicit as_of reference time")

    candidates: list[FollowUpCandidate] = []

    if inputs.response_due_at is not None:
        seconds = (inputs.response_due_at - as_of).total_seconds()
        if seconds < 0:
            candidates.append(
                FollowUpCandidate(
                    rule_id=RULE_RESPONSE_OVERDUE,
                    reason="The response deadline has passed and no response is recorded.",
                    target_type=inputs.target_type,
                    target_ref=inputs.target_ref,
                    urgency=Urgency.URGENT,
                    next_due_at=inputs.response_due_at,
                    last_activity_at=inputs.last_activity_at,
                    status=FollowUpStatus.OVERDUE,
                )
            )
        elif seconds <= due_soon_hours * 3600:
            candidates.append(
                FollowUpCandidate(
                    rule_id=RULE_RESPONSE_OVERDUE,
                    reason="The response deadline is approaching.",
                    target_type=inputs.target_type,
                    target_ref=inputs.target_ref,
                    urgency=Urgency.HIGH,
                    next_due_at=inputs.response_due_at,
                    last_activity_at=inputs.last_activity_at,
                )
            )
    elif inputs.last_activity_at is not None:
        idle_seconds = (as_of - inputs.last_activity_at).total_seconds()
        if idle_seconds >= response_window_hours * 3600:
            candidates.append(
                FollowUpCandidate(
                    rule_id=RULE_NO_RESPONSE,
                    reason=(
                        f"No activity recorded for more than {response_window_hours} hours "
                        "while the work is still open."
                    ),
                    target_type=inputs.target_type,
                    target_ref=inputs.target_ref,
                    urgency=Urgency.NORMAL,
                    next_due_at=None,
                    last_activity_at=inputs.last_activity_at,
                )
            )

    if inputs.commitment_due_at is not None:
        seconds = (inputs.commitment_due_at - as_of).total_seconds()
        if seconds < 0:
            candidates.append(
                FollowUpCandidate(
                    rule_id=RULE_COMMITMENT_MISSED,
                    reason="A commitment's due date has passed and it is not recorded as met.",
                    target_type=inputs.target_type,
                    target_ref=inputs.target_ref,
                    urgency=Urgency.URGENT,
                    next_due_at=inputs.commitment_due_at,
                    last_activity_at=inputs.last_activity_at,
                    status=FollowUpStatus.OVERDUE,
                )
            )
        elif seconds <= due_soon_hours * 3600:
            candidates.append(
                FollowUpCandidate(
                    rule_id=RULE_COMMITMENT_APPROACHING,
                    reason="A commitment's due date is approaching.",
                    target_type=inputs.target_type,
                    target_ref=inputs.target_ref,
                    urgency=Urgency.HIGH,
                    next_due_at=inputs.commitment_due_at,
                    last_activity_at=inputs.last_activity_at,
                )
            )

    if inputs.work_status in {"new", "open"} and inputs.last_activity_at is not None:
        idle_seconds = (as_of - inputs.last_activity_at).total_seconds()
        if idle_seconds >= unresolved_window_hours * 3600:
            candidates.append(
                FollowUpCandidate(
                    rule_id=RULE_WORK_UNRESOLVED,
                    reason=(
                        f"Work has remained unstarted for more than "
                        f"{unresolved_window_hours // 24} days."
                    ),
                    target_type=inputs.target_type,
                    target_ref=inputs.target_ref,
                    urgency=Urgency.NORMAL,
                    next_due_at=None,
                    last_activity_at=inputs.last_activity_at,
                )
            )

    if inputs.blocked_since is not None:
        blocked_seconds = (as_of - inputs.blocked_since).total_seconds()
        if blocked_seconds >= unresolved_window_hours * 3600:
            candidates.append(
                FollowUpCandidate(
                    rule_id=RULE_BLOCKED_TOO_LONG,
                    reason=(
                        f"Work has been blocked for more than {unresolved_window_hours // 24} days."
                    ),
                    target_type=inputs.target_type,
                    target_ref=inputs.target_ref,
                    urgency=Urgency.HIGH,
                    next_due_at=None,
                    last_activity_at=inputs.blocked_since,
                )
            )

    return candidates


__all__ = [
    "RULE_BLOCKED_TOO_LONG",
    "RULE_COMMITMENT_APPROACHING",
    "RULE_COMMITMENT_MISSED",
    "RULE_NO_RESPONSE",
    "RULE_RESPONSE_OVERDUE",
    "RULE_WORK_UNRESOLVED",
    "FollowUpCandidate",
    "FollowUpInputs",
    "evaluate_followups",
]
