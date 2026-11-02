"""Safe rule evaluation.

A rule is a small, validated expression tree over a named operand, not a script. There is no
``eval``, no ``exec``, and no SQL: the operators are a fixed set and the operand is looked up
in a bounded mapping. This is what makes a tenant-authored rule safe to store and run.

The operand map is deliberately small and explicit. A rule may reference:

* ``value`` — the latest observation value;
* ``mean``, ``median``, ``stddev``, ``min``, ``max``, ``p25``, ``p75``, ``count`` — window
  statistics;
* ``roc`` — the per-second rate of change over the window;
* ``pct_change`` — the percentage change over the window;
* any feature name in the provided feature map (for example ``mean_300s``).

An unknown operand is a rule error, not a silent pass.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from humisense.layer3 import stats
from humisense.layer3.contract import RuleCondition
from humisense.layer3.observations import Observation

MAX_CONDITION_DEPTH = 5
MAX_CONDITION_CHILDREN = 10
"""Bounds so a deeply nested or wide condition cannot become a denial-of-service vector."""


@dataclass(frozen=True, slots=True)
class RuleEvaluation:
    """The outcome of evaluating one rule."""

    triggered: bool
    matched_operand: str | None
    observed: float | None
    threshold: float | None
    detail: str


class RuleError(ValueError):
    """Raised when a rule references an unknown operand or is structurally invalid."""


def build_operands(observations: list[Observation], features: dict[str, float]) -> dict[str, float]:
    """Build the bounded operand map a rule may reference."""
    values = [o.value for o in observations if _finite(o.value)]
    operands: dict[str, float] = dict(features)
    if not values:
        return operands
    operands.setdefault("value", values[-1])
    operands.setdefault("mean", stats.mean(values) or 0.0)
    operands.setdefault("median", stats.median(values) or 0.0)
    stddev = stats.stddev(values)
    operands.setdefault("stddev", stddev if stddev is not None else 0.0)
    operands.setdefault("min", min(values))
    operands.setdefault("max", max(values))
    operands.setdefault("p25", stats.quantile(values, 0.25) or 0.0)
    operands.setdefault("p75", stats.quantile(values, 0.75) or 0.0)
    operands.setdefault("count", float(len(values)))
    elapsed = (observations[-1].at - observations[0].at).total_seconds()
    roc = stats.rate_of_change(values, interval_seconds=elapsed)
    operands.setdefault("roc", roc if roc is not None else 0.0)
    pct = stats.percentage_change(values[0], values[-1])
    operands.setdefault("pct_change", pct if pct is not None else 0.0)
    return operands


def validate_condition(condition: RuleCondition, *, depth: int = 0) -> None:
    """Validate a condition tree's shape and operands.

    Raises:
        RuleError: when the tree is too deep or wide, or a leaf is malformed.
    """
    if depth > MAX_CONDITION_DEPTH:
        raise RuleError("The rule condition is nested too deeply.")
    if condition.operator in {"and", "or"}:
        if not condition.conditions:
            raise RuleError(f"Operator {condition.operator!r} needs at least one child condition.")
        if len(condition.conditions) > MAX_CONDITION_CHILDREN:
            raise RuleError("The rule condition has too many children.")
        for child in condition.conditions:
            validate_condition(child, depth=depth + 1)
        return
    if condition.operator == "not":
        if len(condition.conditions) != 1:
            raise RuleError("Operator 'not' needs exactly one child condition.")
        validate_condition(condition.conditions[0], depth=depth + 1)
        return
    # Leaf comparison.
    if condition.operand is None:
        raise RuleError(f"Operator {condition.operator!r} needs an operand.")
    if condition.threshold is None:
        raise RuleError(f"Operator {condition.operator!r} needs a threshold.")


def evaluate_rule(condition: RuleCondition, operands: dict[str, float]) -> RuleEvaluation:
    """Evaluate a validated condition against the operand map.

    Raises:
        RuleError: when a referenced operand is not present.
    """
    return _evaluate(condition, operands)


def _evaluate(condition: RuleCondition, operands: dict[str, float]) -> RuleEvaluation:
    operator = condition.operator
    if operator == "and":
        for child in condition.conditions:
            result = _evaluate(child, operands)
            if not result.triggered:
                return RuleEvaluation(
                    triggered=False,
                    matched_operand=None,
                    observed=None,
                    threshold=None,
                    detail="A required sub-condition did not match.",
                )
        return RuleEvaluation(True, None, None, None, "All sub-conditions matched.")
    if operator == "or":
        for child in condition.conditions:
            result = _evaluate(child, operands)
            if result.triggered:
                return RuleEvaluation(
                    True, result.matched_operand, result.observed, result.threshold, result.detail
                )
        return RuleEvaluation(False, None, None, None, "No sub-condition matched.")
    if operator == "not":
        result = _evaluate(condition.conditions[0], operands)
        return RuleEvaluation(
            triggered=not result.triggered,
            matched_operand=result.matched_operand,
            observed=result.observed,
            threshold=result.threshold,
            detail="Negated a sub-condition.",
        )

    operand_name = condition.operand
    assert operand_name is not None
    if operand_name not in operands:
        raise RuleError(f"Unknown rule operand: {operand_name!r}")
    observed = operands[operand_name]
    threshold = condition.threshold
    assert threshold is not None

    triggered, detail = _compare(operator, observed, threshold, operand_name)
    return RuleEvaluation(
        triggered=triggered,
        matched_operand=operand_name,
        observed=observed,
        threshold=threshold,
        detail=detail,
    )


def _compare(operator: str, observed: float, threshold: float, name: str) -> tuple[bool, str]:
    if operator == "gt":
        return observed > threshold, f"{name}={observed:.4g} > {threshold:.4g}"
    if operator == "gte":
        return observed >= threshold, f"{name}={observed:.4g} >= {threshold:.4g}"
    if operator == "lt":
        return observed < threshold, f"{name}={observed:.4g} < {threshold:.4g}"
    if operator == "lte":
        return observed <= threshold, f"{name}={observed:.4g} <= {threshold:.4g}"
    if operator == "eq":
        return observed == threshold, f"{name}={observed:.4g} == {threshold:.4g}"
    if operator == "ne":
        return observed != threshold, f"{name}={observed:.4g} != {threshold:.4g}"
    if operator == "abs_gt":
        return (
            abs(observed) > abs(threshold),
            f"|{name}|={abs(observed):.4g} > {abs(threshold):.4g}",
        )
    if operator == "pct_gt":
        # The operand is a percentage-valued metric (for example ``pct_change``); the
        # threshold is a percentage. The comparison is on magnitude so a large fall triggers
        # as readily as a large rise.
        return (
            abs(observed) > abs(threshold),
            f"|{name}|={abs(observed):.4g}% > {abs(threshold):.4g}%",
        )
    raise RuleError(f"Unknown rule operator: {operator!r}")


def _finite(value: float) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


__all__ = [
    "MAX_CONDITION_CHILDREN",
    "MAX_CONDITION_DEPTH",
    "RuleError",
    "RuleEvaluation",
    "build_operands",
    "evaluate_rule",
    "validate_condition",
]
