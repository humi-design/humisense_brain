"""Data readiness: whether a method may run over a window.

Readiness is the precondition gate. A method (a detector, a baseline build, a forecast) runs
only when the window is good enough for it; otherwise the run records a readiness result and
produces no detection. This is what keeps Layer 3 from reporting an anomaly it cannot
support.

Readiness combines:

* the data-quality status of the window, and
* the method's own minimum-sample requirement.

The result is one of ``READY``, ``READY_WITH_WARNINGS``, ``INSUFFICIENT_DATA``,
``INVALID_DATA``, or ``STALE_DATA``, with a machine-readable reason list.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from humisense.layer3.contract import (
    DataQualityResult,
    DataQualityStatus,
    ReadinessResult,
    ReadinessStatus,
)


@dataclass(frozen=True, slots=True)
class MethodRequirement:
    """What a method needs before it may run."""

    method: str
    min_samples: int
    """The minimum number of valid observations the method needs."""
    allow_degraded: bool = False
    """Whether the method tolerates a DEGRADED window (with a warning)."""
    allow_stale: bool = False
    """Whether the method tolerates stale data. Most do not."""


DEFAULT_REQUIREMENTS: dict[str, MethodRequirement] = {
    "z_score": MethodRequirement("z_score", min_samples=10),
    "robust_z_score": MethodRequirement("robust_z_score", min_samples=10),
    "iqr": MethodRequirement("iqr", min_samples=8),
    "ewma": MethodRequirement("ewma", min_samples=5, allow_degraded=True),
    "cusum": MethodRequirement("cusum", min_samples=20),
    "threshold": MethodRequirement("threshold", min_samples=1),
    "baseline_deviation": MethodRequirement("baseline_deviation", min_samples=1),
    "isolation_forest": MethodRequirement("isolation_forest", min_samples=50),
    "trend": MethodRequirement("trend", min_samples=10),
    "change_point": MethodRequirement("change_point", min_samples=20),
    "forecast": MethodRequirement("forecast", min_samples=20),
    "correlation": MethodRequirement("correlation", min_samples=30),
}


@dataclass(slots=True)
class _Reasons:
    items: list[str] = field(default_factory=list)

    def add(self, reason: str) -> None:
        self.items.append(reason)


def evaluate_readiness(
    organization_id: uuid.UUID,
    entity_id: str,
    metric: str,
    quality: DataQualityResult,
    requirement: MethodRequirement,
    *,
    as_of: datetime,
    sample_count: int | None = None,
) -> ReadinessResult:
    """Decide whether ``requirement.method`` may run over the assessed window."""
    reasons = _Reasons()
    samples = quality.evaluated_count if sample_count is None else sample_count
    window_start = quality.window_start
    window_end = quality.window_end

    if quality.status == DataQualityStatus.UNUSABLE:
        reasons.add("The window is unusable for analysis.")
        return _result(
            organization_id,
            entity_id,
            metric,
            requirement,
            ReadinessStatus.INVALID_DATA,
            reasons.items,
            samples,
            window_start,
            window_end,
            as_of,
        )

    stale = any(issue.code == "stale_data" for issue in quality.issues)
    if stale and not requirement.allow_stale:
        reasons.add("The window is stale for this method.")
        return _result(
            organization_id,
            entity_id,
            metric,
            requirement,
            ReadinessStatus.STALE_DATA,
            reasons.items,
            samples,
            window_start,
            window_end,
            as_of,
        )

    if samples < requirement.min_samples:
        reasons.add(
            f"{samples} samples are available; {requirement.method} needs "
            f"{requirement.min_samples}."
        )
        return _result(
            organization_id,
            entity_id,
            metric,
            requirement,
            ReadinessStatus.INSUFFICIENT_DATA,
            reasons.items,
            samples,
            window_start,
            window_end,
            as_of,
        )

    if quality.status == DataQualityStatus.DEGRADED:
        if not requirement.allow_degraded:
            reasons.add("The window is degraded and this method requires a clean window.")
            return _result(
                organization_id,
                entity_id,
                metric,
                requirement,
                ReadinessStatus.READY_WITH_WARNINGS,
                reasons.items,
                samples,
                window_start,
                window_end,
                as_of,
            )
        reasons.add("The window is degraded; results carry a warning.")
        return _result(
            organization_id,
            entity_id,
            metric,
            requirement,
            ReadinessStatus.READY_WITH_WARNINGS,
            reasons.items,
            samples,
            window_start,
            window_end,
            as_of,
        )

    if quality.status == DataQualityStatus.ACCEPTABLE:
        reasons.add("The window is acceptable but carries minor issues.")
        return _result(
            organization_id,
            entity_id,
            metric,
            requirement,
            ReadinessStatus.READY_WITH_WARNINGS,
            reasons.items,
            samples,
            window_start,
            window_end,
            as_of,
        )

    return _result(
        organization_id,
        entity_id,
        metric,
        requirement,
        ReadinessStatus.READY,
        reasons.items,
        samples,
        window_start,
        window_end,
        as_of,
    )


def _result(
    organization_id: uuid.UUID,
    entity_id: str,
    metric: str,
    requirement: MethodRequirement,
    status: ReadinessStatus,
    reasons: list[str],
    samples: int,
    window_start: datetime | None,
    window_end: datetime | None,
    as_of: datetime,
) -> ReadinessResult:
    return ReadinessResult(
        organization_id=organization_id,
        entity_id=entity_id,
        metric=metric,
        method=requirement.method,
        status=status,
        reasons=reasons,
        sample_count=samples,
        min_required=requirement.min_samples,
        window_start=window_start,
        window_end=window_end,
        assessed_at=as_of,
    )


def requirement_for(method: str) -> MethodRequirement:
    """Return the requirement for ``method``, defaulting to a conservative minimum."""
    return DEFAULT_REQUIREMENTS.get(method, MethodRequirement(method, min_samples=10))


__all__ = ["DEFAULT_REQUIREMENTS", "MethodRequirement", "evaluate_readiness", "requirement_for"]
