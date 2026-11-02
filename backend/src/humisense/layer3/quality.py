"""Data-quality intelligence.

Before any statistic is trusted, the window it was computed from is assessed. Layer 3 never
reports an anomaly on data it has not first judged fit for analysis: a detection carries the
quality status of its window, and a window that is unusable produces a data-quality issue
rather than a detection.

The checks are deliberately conservative and each one is explainable:

* **missing / non-finite** — a value that is not a finite number cannot be analyzed.
* **stale** — the newest observation is older than the expected cadence allows.
* **out-of-order** — observations whose timestamps are not monotonic.
* **irregular / frequency change** — the observed cadence departs from the baseline cadence.
* **implausible** — a value far outside the robust range of the window (a possible unit or
  sensor fault, reported as a quality issue, never silently clipped).
* **duplicate** — two observations at the same timestamp and value.
* **unit inconsistency** — more than one unit within a single entity/metric window.

The assessment returns a status and a list of issues; it does not mutate the data.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from humisense.layer3 import stats
from humisense.layer3.contract import (
    DataQualityResult,
    DataQualityStatus,
    QualityIssue,
    Severity,
)
from humisense.layer3.observations import Observation

DEFAULT_STALE_FACTOR = 3.0
"""The newest observation may be this many cadence intervals old before it is stale."""

DEFAULT_IMPLAUSIBLE_ROBUST_Z = 8.0
"""A value beyond this robust z-score is reported as implausible, not analyzed as an anomaly."""


@dataclass(slots=True)
class QualityThresholds:
    """Tunable quality thresholds. Defaults are conservative and documented."""

    stale_factor: float = DEFAULT_STALE_FACTOR
    implausible_robust_z: float = DEFAULT_IMPLAUSIBLE_ROBUST_Z
    max_irregularity_ratio: float = 0.5
    """Fraction of intervals that may deviate from the median cadence before it is irregular."""
    min_samples_for_shape_checks: int = 5


@dataclass(slots=True)
class _Issues:
    items: list[QualityIssue] = field(default_factory=list)

    def add(
        self,
        code: str,
        severity: Severity,
        detail: str,
        *,
        field_: str | None = None,
        count: int = 1,
    ) -> None:
        self.items.append(
            QualityIssue(code=code, severity=severity, detail=detail, field=field_, count=count)
        )


def _cadence_seconds(observations: list[Observation]) -> float | None:
    if len(observations) < 2:
        return None
    deltas = [
        (observations[i].at - observations[i - 1].at).total_seconds()
        for i in range(1, len(observations))
    ]
    positive = [d for d in deltas if d > 0]
    if not positive:
        return None
    return stats.median(positive)


def assess_quality(
    organization_id: uuid.UUID,
    entity_id: str,
    metric: str,
    observations: list[Observation],
    *,
    as_of: datetime,
    thresholds: QualityThresholds | None = None,
) -> DataQualityResult:
    """Assess the quality of one entity/metric window.

    ``as_of`` is the reference time for the staleness check; it is passed in rather than
    read from the clock so the assessment is deterministic and testable.
    """
    limits = thresholds or QualityThresholds()
    issues = _Issues()
    now = as_of

    if not observations:
        issues.add(
            "no_data",
            Severity.HIGH,
            "The window contains no observations for this signal.",
        )
        return DataQualityResult(
            organization_id=organization_id,
            entity_id=entity_id,
            metric=metric,
            status=DataQualityStatus.UNUSABLE,
            issues=issues.items,
            evaluated_count=0,
            assessed_at=now,
        )

    values = [o.value for o in observations]
    missing = sum(1 for v in values if v is None)
    non_finite = sum(1 for v in values if v is not None and not _is_finite(v))
    valid_values = [v for v in values if v is not None and _is_finite(v)]

    window_start = observations[0].at
    window_end = observations[-1].at

    # --- out-of-order -----------------------------------------------------
    out_of_order = sum(
        1 for i in range(1, len(observations)) if observations[i].at < observations[i - 1].at
    )
    if out_of_order:
        issues.add(
            "out_of_order",
            Severity.MEDIUM,
            f"{out_of_order} observations are earlier than their predecessor.",
            count=out_of_order,
        )

    # --- duplicates -------------------------------------------------------
    seen: Counter[tuple[datetime, float]] = Counter()
    for observation in observations:
        seen[(observation.at, observation.value)] += 1
    duplicates = sum(count - 1 for count in seen.values() if count > 1)
    if duplicates:
        issues.add(
            "duplicate_observation",
            Severity.LOW,
            f"{duplicates} duplicate (timestamp, value) observations.",
            count=duplicates,
        )

    # --- unit consistency -------------------------------------------------
    units = {o.unit for o in observations if o.unit}
    if len(units) > 1:
        issues.add(
            "unit_inconsistent",
            Severity.HIGH,
            f"The window mixes units: {sorted(units)}.",
        )

    # --- staleness and cadence -------------------------------------------
    cadence = _cadence_seconds(observations)
    if cadence is not None:
        staleness = (now - window_end).total_seconds()
        if staleness > cadence * limits.stale_factor:
            issues.add(
                "stale_data",
                Severity.HIGH,
                f"The newest observation is {staleness:.0f}s old; the cadence is {cadence:.0f}s.",
            )
        deltas = [
            (observations[i].at - observations[i - 1].at).total_seconds()
            for i in range(1, len(observations))
        ]
        if len(observations) >= limits.min_samples_for_shape_checks and cadence > 0:
            irregular = sum(1 for d in deltas if abs(d - cadence) > 0.5 * cadence)
            if irregular / max(len(deltas), 1) > limits.max_irregularity_ratio:
                issues.add(
                    "irregular_sampling",
                    Severity.MEDIUM,
                    f"{irregular} of {len(deltas)} intervals deviate from the "
                    f"{cadence:.0f}s cadence.",
                    count=irregular,
                )

    # --- implausible values ----------------------------------------------
    if len(valid_values) >= limits.min_samples_for_shape_checks:
        med = stats.median(valid_values)
        mad = stats.median_absolute_deviation(valid_values)
        if med is not None and mad not in (None, 0):
            outliers = sum(
                1
                for v in valid_values
                if abs(stats.robust_z_score(v, med, mad) or 0.0) > limits.implausible_robust_z
            )
            if outliers:
                issues.add(
                    "implausible_value",
                    Severity.MEDIUM,
                    f"{outliers} values are implausibly far from the window centre.",
                    field_="value",
                    count=outliers,
                )

    status = _status_for(issues.items)
    return DataQualityResult(
        organization_id=organization_id,
        entity_id=entity_id,
        metric=metric,
        status=status,
        issues=issues.items,
        evaluated_count=len(observations),
        missing_count=missing + non_finite,
        invalid_count=non_finite,
        duplicate_count=duplicates,
        window_start=window_start,
        window_end=window_end,
        assessed_at=now,
    )


def _status_for(issues: list[QualityIssue]) -> DataQualityStatus:
    """Map the issues found to an overall status.

    A HIGH issue makes the window UNUSABLE; a MEDIUM issue makes it DEGRADED; a LOW issue
    makes it ACCEPTABLE; no issue makes it GOOD.
    """
    if any(issue.severity == Severity.HIGH for issue in issues):
        return DataQualityStatus.UNUSABLE
    if any(issue.severity == Severity.MEDIUM for issue in issues):
        return DataQualityStatus.DEGRADED
    if any(issue.severity == Severity.LOW for issue in issues):
        return DataQualityStatus.ACCEPTABLE
    return DataQualityStatus.GOOD


def _is_finite(value: float) -> bool:
    import math

    return isinstance(value, (int, float)) and math.isfinite(value)


def expected_staleness_window(
    cadence_seconds: float | None, *, factor: float = DEFAULT_STALE_FACTOR
) -> timedelta:
    """The maximum age an observation may reach before it is considered stale."""
    base = cadence_seconds if cadence_seconds and cadence_seconds > 0 else 60.0
    return timedelta(seconds=base * factor)


__all__ = [
    "DEFAULT_IMPLAUSIBLE_ROBUST_Z",
    "DEFAULT_STALE_FACTOR",
    "QualityThresholds",
    "assess_quality",
    "expected_staleness_window",
]
