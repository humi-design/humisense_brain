"""Ownership and responsibility mapping.

Work must have clear accountability, and accountability must come from a source that
actually states it. Two rules are enforced here rather than left to callers:

* **A name in a conversation is not an owner.** Ownership is resolved only from an explicit
  source field (an assignee/owner field on a structured record) or an authorized action.
  Everything else resolves to ``UNRESOLVED``.
* **Ownership changes preserve history.** The caller records the change on the timeline; this
  module only resolves the value.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from humisense.db.models import (
    AssignmentRole,
    ParticipantType,
    VerificationStatus,
)

# Source payload keys that name an assignee or owner. The list is deliberately conservative:
# a key is only here if the source states the field is an assignment, not a mention.
OWNER_KEYS = ("owner", "owner_id", "assignee", "assignee_id", "assigned_to", "responsible")
REQUESTER_KEYS = ("requester", "requester_id", "requested_by", "reported_by", "created_by")
APPROVER_KEYS = ("approver", "approver_id", "approved_by")
# Keys that merely mention a person. These are recorded as references, never as assignments.
MENTION_KEYS = ("from", "sender", "author", "to", "cc", "participants", "attendees")


class UserDirectory(Protocol):
    """Resolves an identity string to a tenant user, or returns None.

    Layer 4 never invents a user. If a directory lookup fails, the assignment stays
    unresolved rather than pointing at a guessed id.
    """

    async def resolve(self, organization_id: uuid.UUID, identity: str) -> uuid.UUID | None: ...


@dataclass(frozen=True, slots=True)
class ResolvedAssignment:
    """An assignment, with an explicit statement of how certain it is."""

    participant_type: ParticipantType
    role: AssignmentRole
    user_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    external_name: str | None = None
    verification: VerificationStatus = VerificationStatus.UNRESOLVED
    confidence: float | None = None


def _first_present(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


async def resolve_owner(
    *,
    organization_id: uuid.UUID,
    payload: dict[str, Any],
    directory: UserDirectory | None,
) -> ResolvedAssignment | None:
    """Resolve an owner from an explicit assignment field.

    Returns None when the source states no owner. A ``None`` return is the ``UNRESOLVED``
    ownership state; callers must not substitute a mentioned name.
    """
    raw = _first_present(payload, OWNER_KEYS)
    if raw is None:
        return None
    return await _resolve_person(
        organization_id=organization_id,
        raw=raw,
        role=AssignmentRole.OWNER,
        directory=directory,
    )


async def resolve_requester(
    *,
    organization_id: uuid.UUID,
    payload: dict[str, Any],
    directory: UserDirectory | None,
) -> ResolvedAssignment | None:
    """Resolve the requester. A requester is not the owner, and is recorded separately."""
    raw = _first_present(payload, REQUESTER_KEYS)
    if raw is None:
        return None
    return await _resolve_person(
        organization_id=organization_id,
        raw=raw,
        role=AssignmentRole.REQUESTER,
        directory=directory,
    )


async def resolve_approver(
    *,
    organization_id: uuid.UUID,
    payload: dict[str, Any],
    directory: UserDirectory | None,
) -> ResolvedAssignment | None:
    raw = _first_present(payload, APPROVER_KEYS)
    if raw is None:
        return None
    return await _resolve_person(
        organization_id=organization_id,
        raw=raw,
        role=AssignmentRole.APPROVER,
        directory=directory,
    )


async def _resolve_person(
    *,
    organization_id: uuid.UUID,
    raw: Any,
    role: AssignmentRole,
    directory: UserDirectory | None,
) -> ResolvedAssignment:
    """Resolve one identity string to a user id, or leave it unresolved.

    An external identity (an email address outside the tenant, a supplier name) is recorded
    as an ``EXTERNAL`` participant with ``SOURCE_REPORTED`` verification: it is a fact the
    source stated, but it is not a tenant user.
    """
    identity = str(raw).strip()
    if directory is not None:
        user_id = await directory.resolve(organization_id, identity)
        if user_id is not None:
            return ResolvedAssignment(
                participant_type=ParticipantType.USER,
                role=role,
                user_id=user_id,
                verification=VerificationStatus.VERIFIED,
                confidence=1.0,
            )
    return ResolvedAssignment(
        participant_type=ParticipantType.EXTERNAL,
        role=role,
        external_name=identity[:200],
        verification=VerificationStatus.SOURCE_REPORTED,
        confidence=0.5,
    )


def mentioned_identities(payload: dict[str, Any]) -> list[str]:
    """Return identities that are only *mentioned* in a record.

    These become ``MENTIONED`` source references, never assignments. The function exists so
    the distinction is made in one place.
    """
    found: list[str] = []
    for key in MENTION_KEYS:
        value = payload.get(key)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, str):
            found.append(value[:200])
        elif isinstance(value, list):
            found.extend(str(item)[:200] for item in value[:20])
    return found


__all__ = [
    "APPROVER_KEYS",
    "MENTION_KEYS",
    "OWNER_KEYS",
    "REQUESTER_KEYS",
    "ResolvedAssignment",
    "UserDirectory",
    "mentioned_identities",
    "resolve_approver",
    "resolve_owner",
    "resolve_requester",
]
