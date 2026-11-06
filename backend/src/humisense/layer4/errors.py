"""Layer 4 error taxonomy.

Layer 4 follows the platform rule: typed exceptions with a stable machine-readable code and
a public message that is safe to display. Internal detail is attached for the log, never
returned to a client.
"""

from __future__ import annotations

from humisense.errors import HumisenseError


class WorkError(HumisenseError):
    """Base for Layer 4 failures."""

    code = "work_error"
    http_status = 500
    public_message = "The work operation could not be completed."


class IllegalWorkTransition(WorkError):
    """Raised when code attempts a work-status transition the state machine forbids.

    Like Layer 2's processing state machine, encoding the legal transitions is what keeps
    a work item's history recoverable: an illegal transition is a bug that raises
    immediately instead of corrupting the record.
    """

    code = "illegal_work_transition"
    http_status = 409
    public_message = "That status change is not permitted."

    def __init__(self, source: str, target: str) -> None:
        self.source = source
        self.target = target
        super().__init__(
            detail=f"illegal work transition {source} -> {target}",
            context={"from_status": source, "to_status": target},
        )


class WorkDerivationError(WorkError):
    """A source record could not be turned into work.

    This is raised only where a derivation cannot proceed at all; a per-record derivation
    failure is normally recorded as a skip so one bad record never fails a run.
    """

    code = "work_derivation_failed"
    http_status = 422
    public_message = "A source record could not be interpreted as work."


class WorkConflictDetected(WorkError):
    """A conflicting value was detected and must not be silently resolved."""

    code = "work_conflict_detected"
    http_status = 409
    public_message = "This change conflicts with an existing value and requires review."


class WorkNotFound(WorkError):
    code = "work_not_found"
    http_status = 404
    public_message = "The requested work item was not found."


class WorkPolicyError(WorkError):
    """The tenant's derivation policy is invalid or unsafe."""

    code = "work_policy_invalid"
    http_status = 422
    public_message = "The work configuration is invalid."


__all__ = [
    "IllegalWorkTransition",
    "WorkConflictDetected",
    "WorkDerivationError",
    "WorkError",
    "WorkNotFound",
    "WorkPolicyError",
]
