"""Work data-quality and conflict management.

Work intelligence depends on correct interpretation of source records, so conflicts are a
first-class concept rather than an error to be smoothed over. The governing rule:

    **A conflict is never silently resolved.** The latest timestamp is not assumed to be the
    truth unless an explicit source-authority policy says so for that conflict type.

Detection is deterministic and cheap: a conflict exists when two sources state different
values for the same field of the same work item. The competing values are preserved verbatim
so a human can review them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from humisense.db.models import WorkConflictType

# A tenant may declare that one source system is authoritative for a given conflict type.
# Absent such a policy, no automatic resolution is performed.
SourceAuthorityPolicy = dict[WorkConflictType, str]


@dataclass(frozen=True, slots=True)
class ConflictingValue:
    """One competing value for a field, with the source that stated it."""

    source_system: str
    source_record_key: str
    value: Any
    observed_at: str | None = None

    def as_candidate(self) -> dict[str, Any]:
        return {
            "source_system": self.source_system,
            "source_record_key": self.source_record_key,
            "value": self.value,
            "observed_at": self.observed_at,
        }


@dataclass(frozen=True, slots=True)
class ConflictCandidate:
    """A detected conflict, ready to persist."""

    conflict_type: WorkConflictType
    entity_type: str
    entity_key: str
    detail: str
    candidates: list[ConflictingValue] = field(default_factory=list)

    def candidate_payload(self) -> list[dict[str, Any]]:
        return [candidate.as_candidate() for candidate in self.candidates]


def detect_value_conflict(
    *,
    conflict_type: WorkConflictType,
    entity_type: str,
    entity_key: str,
    field_name: str,
    values: list[ConflictingValue],
) -> ConflictCandidate | None:
    """Return a conflict when two or more sources disagree, else None.

    Comparison is on the value itself. Two sources reporting the *same* value is agreement,
    not a conflict. Ordering is preserved so the review shows who said what first.
    """
    distinct = {_hashable(value.value) for value in values}
    if len(distinct) <= 1:
        return None
    rendered = ", ".join(f"{v.source_system}={v.value!r}" for v in values)
    return ConflictCandidate(
        conflict_type=conflict_type,
        entity_type=entity_type,
        entity_key=entity_key,
        detail=f"Sources disagree on {field_name}: {rendered}",
        candidates=values,
    )


def resolve_with_policy(
    conflict: ConflictCandidate,
    *,
    policy: SourceAuthorityPolicy | None,
) -> ConflictingValue | None:
    """Return the value an explicit source-authority policy selects, or None.

    Returning None means "do not auto-resolve", which is the default. A policy must name the
    authoritative source system for the conflict type; even then, the resolution is recorded
    as an explicit, auditable act rather than a silent overwrite.
    """
    if not policy:
        return None
    authoritative = policy.get(conflict.conflict_type)
    if not authoritative:
        return None
    for candidate in conflict.candidates:
        if candidate.source_system == authoritative:
            return candidate
    return None


def _hashable(value: Any) -> Any:
    """Return a hashable representation of a value for comparison."""
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    if isinstance(value, list):
        return tuple(_hashable(item) for item in value)
    return value


__all__ = [
    "ConflictCandidate",
    "ConflictingValue",
    "SourceAuthorityPolicy",
    "detect_value_conflict",
    "resolve_with_policy",
]
