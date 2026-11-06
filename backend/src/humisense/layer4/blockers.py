"""Blocker and waiting-state intelligence.

The most important rule in this module is what it refuses to do: **it does not claim to know
why something is blocked unless supporting evidence exists.** When a work item is waiting on
an unresolved dependency, the blocker is real but its cause is ``UNKNOWN`` until a source
states one.

A blocker's evidence is references, never payload values, matching the platform's
provenance-not-payloads rule.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from humisense.db.models import (
    BlockerStatus,
    BlockerType,
    DependencyRelationship,
    VerificationStatus,
    WorkOrigin,
)


@dataclass(frozen=True, slots=True)
class BlockerCandidate:
    """A detected reason work is not progressing."""

    blocker_type: BlockerType
    description: str
    blocking_entity: str | None = None
    responsible_department_id: uuid.UUID | None = None
    expected_resolution_at: datetime | None = None
    evidence_references: list[dict[str, object]] = field(default_factory=list)
    provenance: WorkOrigin = WorkOrigin.INFERRED
    verification_status: VerificationStatus = VerificationStatus.UNRESOLVED
    status: BlockerStatus = BlockerStatus.OPEN


# Which dependency relationships imply a waiting/blocked state, and what kind of blocker
# they imply when nothing more specific is known.
_RELATIONSHIP_BLOCKER: dict[DependencyRelationship, BlockerType] = {
    DependencyRelationship.BLOCKED_BY: BlockerType.DEPENDENCY,
    DependencyRelationship.WAITING_FOR: BlockerType.DEPENDENCY,
    DependencyRelationship.REQUIRES: BlockerType.DEPENDENCY,
    DependencyRelationship.MATERIAL_DEPENDENCY: BlockerType.MATERIAL,
    DependencyRelationship.APPROVAL_REQUIRED: BlockerType.APPROVAL,
    DependencyRelationship.INFORMATION_REQUIRED: BlockerType.INFORMATION,
}


def blocker_from_dependency(
    *,
    relationship: DependencyRelationship,
    dependency_status: str,
    target_ref: str | None,
    evidence: list[dict[str, object]],
    detected_at: datetime,
) -> BlockerCandidate | None:
    """Derive a blocker from an open dependency, or return None.

    A satisfied dependency is not a blocker. The cause is recorded as the dependency *type*,
    which is a fact the source stated; the specific reason remains ``UNRESOLVED`` unless the
    source named it.
    """
    if dependency_status == "satisfied":
        return None
    blocker_type = _RELATIONSHIP_BLOCKER.get(relationship)
    if blocker_type is None:
        return None
    description = f"Work is waiting on a dependency ({relationship.value})" + (
        f" on {target_ref}" if target_ref else ""
    )
    return BlockerCandidate(
        blocker_type=blocker_type,
        description=description,
        blocking_entity=target_ref,
        evidence_references=evidence,
        provenance=WorkOrigin.INFERRED,
        # The dependency is known, but the reason behind it is not: the blocker's cause is
        # unresolved until a source states it.
        verification_status=VerificationStatus.UNRESOLVED,
        status=BlockerStatus.OPEN,
    )


def unknown_blocker(
    *, detected_at: datetime, evidence: list[dict[str, object]]
) -> BlockerCandidate:
    """Return the honest blocker for "work is waiting and no reason is evidenced"."""
    return BlockerCandidate(
        blocker_type=BlockerType.UNKNOWN,
        description="Work is waiting; the reason is not evidenced in the source records.",
        evidence_references=evidence,
        provenance=WorkOrigin.INFERRED,
        verification_status=VerificationStatus.UNRESOLVED,
        status=BlockerStatus.OPEN,
    )


def is_blocked_status(status: str) -> bool:
    """Whether a canonical work status implies a blocked or waiting state."""
    return status in {"blocked", "waiting"}


__all__ = [
    "BlockerCandidate",
    "blocker_from_dependency",
    "is_blocked_status",
    "unknown_blocker",
]
