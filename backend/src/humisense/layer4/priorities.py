"""Work prioritization metadata.

Layer 4 computes deterministic operational indicators. It does not decide what matters: that
is Layer 9's job. Two rules keep this honest:

* A rule-derived level **never** overwrites an explicit human-assigned level. Both are
  stored, keyed by ``source``, and the API returns them side by side.
* A deterministic urgency score is presented as an indicator, never as a business decision.

The rule is deliberately small and explainable. It is not a model and it is not tuned; every
input is a field a reader can check.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from humisense.db.models import PriorityLevel

# How many of the canonical fields must be present for the work to be considered complete
# from a data standpoint. Used for the source-completeness indicator only.
_COMPLETENESS_FIELDS = (
    "title",
    "domain",
    "status",
    "work_type",
    "owner_id",
    "due_at",
    "department_id",
)


@dataclass(frozen=True, slots=True)
class PriorityInputs:
    """The deterministic inputs to the rule. Every one is a checkable fact."""

    overdue: bool = False
    due_soon: bool = False
    blocked: bool = False
    dependency_count: int = 0
    source_completeness: float = 0.0
    source_priority: PriorityLevel = PriorityLevel.NORMAL
    user_priority: PriorityLevel | None = None
    commitment_at_risk: bool = False


@dataclass(frozen=True, slots=True)
class PriorityResult:
    """The rule-derived level plus the indicators that produced it."""

    level: PriorityLevel
    indicators: dict[str, bool] = field(default_factory=dict)


def compute_source_completeness(present: dict[str, object]) -> float:
    """Return the fraction of canonical fields that are populated.

    ``present`` maps a field name to its value; ``None`` and empty strings count as absent.
    """
    populated = sum(
        1 for name in _COMPLETENESS_FIELDS if present.get(name) not in (None, "", [], {})
    )
    return round(populated / len(_COMPLETENESS_FIELDS), 4)


def derive_rule_priority(inputs: PriorityInputs) -> PriorityResult:
    """Derive a rule-based priority level from deterministic indicators.

    The ordering is intentional: a blocked or overdue item outranks a merely incomplete one.
    The result is an indicator, not a decision, and it never overrides a user-assigned level.
    """
    indicators = {
        "overdue": inputs.overdue,
        "due_soon": inputs.due_soon,
        "blocked": inputs.blocked,
        "commitment_at_risk": inputs.commitment_at_risk,
        "has_dependencies": inputs.dependency_count > 0,
    }

    if inputs.overdue or inputs.commitment_at_risk:
        level = PriorityLevel.URGENT
    elif inputs.blocked or inputs.due_soon:
        level = PriorityLevel.HIGH
    elif inputs.dependency_count >= 3:
        level = PriorityLevel.NORMAL
    elif inputs.source_completeness < 0.5:
        # Incomplete data is not urgency; it is a data-quality signal. It is reported at
        # NORMAL so it does not masquerade as a priority.
        level = PriorityLevel.NORMAL
    else:
        level = PriorityLevel.NORMAL

    return PriorityResult(level=level, indicators=indicators)


def effective_priority(inputs: PriorityInputs) -> PriorityLevel:
    """Return the level a UI should present as authoritative.

    A user-assigned level wins. Otherwise the source system's own priority wins over the
    rule, because the source knows the business context the rule does not.
    """
    if inputs.user_priority is not None:
        return inputs.user_priority
    if inputs.source_priority != PriorityLevel.NORMAL:
        return inputs.source_priority
    return derive_rule_priority(inputs).level


__all__ = [
    "PriorityInputs",
    "PriorityResult",
    "compute_source_completeness",
    "derive_rule_priority",
    "effective_priority",
]
