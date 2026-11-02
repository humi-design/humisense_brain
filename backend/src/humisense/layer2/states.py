"""Layer 2 processing state machine.

A record's lifecycle is a small, closed state machine. Encoding the legal transitions here
— rather than allowing any state to be written at any time — is what makes the pipeline
recoverable: an illegal transition is a bug that raises immediately instead of corrupting
the record's history.

Terminal states have no outgoing transitions. ``FAILED`` and ``QUARANTINED`` are
deliberately terminal *by the automated pipeline*; a record leaves them only through an
explicit, authorized replay, which starts a fresh cycle at ``VALIDATING``.
"""

from __future__ import annotations

from enum import Enum


class ProcessingState(str, Enum):
    RECEIVED = "received"
    VALIDATING = "validating"
    VALIDATED = "validated"
    NORMALIZING = "normalizing"
    NORMALIZED = "normalized"
    STORED = "stored"
    PUBLISHED = "published"
    RETRY_PENDING = "retry_pending"
    FAILED = "failed"
    QUARANTINED = "quarantined"
    DUPLICATE = "duplicate"
    """A redelivery of a record already processed. Terminal, and not a failure."""


TERMINAL_STATES: frozenset[ProcessingState] = frozenset(
    {
        ProcessingState.PUBLISHED,
        ProcessingState.FAILED,
        ProcessingState.QUARANTINED,
        ProcessingState.DUPLICATE,
    }
)

_TRANSITIONS: dict[ProcessingState, frozenset[ProcessingState]] = {
    ProcessingState.RECEIVED: frozenset(
        {ProcessingState.VALIDATING, ProcessingState.DUPLICATE, ProcessingState.QUARANTINED}
    ),
    ProcessingState.VALIDATING: frozenset({ProcessingState.VALIDATED, ProcessingState.QUARANTINED}),
    ProcessingState.VALIDATED: frozenset(
        {ProcessingState.NORMALIZING, ProcessingState.QUARANTINED}
    ),
    ProcessingState.NORMALIZING: frozenset(
        {
            ProcessingState.NORMALIZED,
            ProcessingState.RETRY_PENDING,
            ProcessingState.QUARANTINED,
        }
    ),
    ProcessingState.NORMALIZED: frozenset(
        {ProcessingState.STORED, ProcessingState.RETRY_PENDING, ProcessingState.QUARANTINED}
    ),
    ProcessingState.STORED: frozenset(
        {ProcessingState.PUBLISHED, ProcessingState.RETRY_PENDING, ProcessingState.FAILED}
    ),
    ProcessingState.RETRY_PENDING: frozenset(
        {
            ProcessingState.VALIDATING,
            ProcessingState.NORMALIZING,
            ProcessingState.STORED,
            ProcessingState.QUARANTINED,
            ProcessingState.FAILED,
        }
    ),
    ProcessingState.PUBLISHED: frozenset(),
    ProcessingState.FAILED: frozenset(),
    ProcessingState.QUARANTINED: frozenset(),
    ProcessingState.DUPLICATE: frozenset(),
}


class IllegalTransition(Exception):
    """Raised when code attempts a transition the state machine does not permit."""

    def __init__(self, source: ProcessingState, target: ProcessingState) -> None:
        self.source = source
        self.target = target
        super().__init__(f"illegal processing transition {source.value} -> {target.value}")


def can_transition(source: ProcessingState, target: ProcessingState) -> bool:
    return target in _TRANSITIONS.get(source, frozenset())


def assert_transition(source: ProcessingState, target: ProcessingState) -> None:
    """Raise :class:`IllegalTransition` unless ``source -> target`` is legal."""
    if not can_transition(source, target):
        raise IllegalTransition(source, target)


def is_terminal(state: ProcessingState) -> bool:
    return state in TERMINAL_STATES


def allowed_targets(source: ProcessingState) -> frozenset[ProcessingState]:
    return _TRANSITIONS.get(source, frozenset())
