"""Baseline construction: a versioned model of normal behaviour.

A baseline is what "normal" looks like for one entity/metric, so a later deviation has
something to be a deviation *from*. Two commitments:

* **Versioned, immutable, explainable.** A baseline is a set of robust summary statistics
  (mean, median, stddev, quantiles, min, max) over a window. It is not a black box. Each
  build is a new immutable version, so a detection can cite the exact baseline it used.
* **Drift-guarded.** A baseline is only replaced by a new version when the new window is
  both well-populated and statistically comparable to the current baseline. A sudden shift
  in the data must be *detected*, not silently absorbed by re-baselining, so a build is
  blocked when the shift is implausibly large unless the operator forces it.

Types supported: ``static`` (an operator-supplied value), ``rolling`` (a recent window),
``historical`` (the whole available history), ``time_of_day`` and ``day_of_week`` (a bucket
of the clock), ``entity`` (per-asset), and ``business`` (counts and business metrics).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from humisense.layer3 import stats
from humisense.layer3.contract import BaselineType, DataQualityStatus
from humisense.layer3.observations import Observation


@dataclass(frozen=True, slots=True)
class BaselineStatistics:
    """The summary statistics that define a baseline version."""

    mean: float | None
    median: float | None
    stddev: float | None
    minimum: float | None
    maximum: float | None
    p10: float | None
    p25: float | None
    p75: float | None
    p90: float | None
    sample_count: int
    quality_status: DataQualityStatus

    def as_columns(self) -> dict[str, object]:
        return {
            "mean": self.mean,
            "median": self.median,
            "stddev": self.stddev,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "p10": self.p10,
            "p25": self.p25,
            "p75": self.p75,
            "p90": self.p90,
            "sample_count": self.sample_count,
            "quality_status": self.quality_status.value,
        }


def compute_statistics(
    observations: list[Observation], *, quality_status: DataQualityStatus
) -> BaselineStatistics:
    """Compute the summary statistics for a baseline over a window."""
    values = [o.value for o in observations if _finite(o.value)]
    if not values:
        return BaselineStatistics(
            mean=None,
            median=None,
            stddev=None,
            minimum=None,
            maximum=None,
            p10=None,
            p25=None,
            p75=None,
            p90=None,
            sample_count=0,
            quality_status=quality_status,
        )
    return BaselineStatistics(
        mean=stats.mean(values),
        median=stats.median(values),
        stddev=stats.stddev(values),
        minimum=min(values),
        maximum=max(values),
        p10=stats.quantile(values, 0.10),
        p25=stats.quantile(values, 0.25),
        p75=stats.quantile(values, 0.75),
        p90=stats.quantile(values, 0.90),
        sample_count=len(values),
        quality_status=quality_status,
    )


def select_window(
    observations: list[Observation],
    *,
    baseline_type: BaselineType,
    at: datetime,
    window_seconds: int,
) -> list[Observation]:
    """Select the observations a baseline of ``baseline_type`` is built from.

    The selection is causal (never uses observations after ``at``) and type-aware:

    * ``historical`` uses the whole available history up to ``at``.
    * ``rolling`` uses the most recent ``window_seconds``.
    * ``time_of_day`` uses observations within ±1 hour of ``at``'s wall-clock time across days.
    * ``day_of_week`` uses observations on the same weekday.
    * ``entity`` / ``business`` / ``static`` fall back to the most recent window.
    """
    series = [o for o in observations if o.at <= at]
    if baseline_type == BaselineType.HISTORICAL:
        return series
    if baseline_type == BaselineType.TIME_OF_DAY:
        target_minute = at.hour * 60 + at.minute
        return [o for o in series if abs((o.at.hour * 60 + o.at.minute) - target_minute) <= 60]
    if baseline_type == BaselineType.DAY_OF_WEEK:
        return [o for o in series if o.at.weekday() == at.weekday()]
    from datetime import timedelta

    lower = at - timedelta(seconds=window_seconds)
    return [o for o in series if lower < o.at <= at]


@dataclass(frozen=True, slots=True)
class DriftAssessment:
    """Whether a new baseline window differs too much from the current baseline."""

    blocked: bool
    reason: str | None
    relative_shift: float | None


def assess_drift(
    previous_median: float | None,
    previous_stddev: float | None,
    new_statistics: BaselineStatistics,
    *,
    max_relative_shift: float = 0.5,
) -> DriftAssessment:
    """Block a re-baseline when the new centre has shifted implausibly far.

    The shift is measured in standard deviations of the *previous* baseline when a standard
    deviation is available, and as a relative change otherwise. A large shift is not
    automatically wrong, but it must not silently rewrite the definition of normal, so it is
    blocked for an operator to confirm.
    """
    if previous_median is None or new_statistics.median is None:
        return DriftAssessment(blocked=False, reason=None, relative_shift=None)

    relative = None
    if previous_median != 0:
        relative = abs(new_statistics.median - previous_median) / abs(previous_median)

    if previous_stddev is not None and previous_stddev != 0:
        shift_in_sigma = abs(new_statistics.median - previous_median) / previous_stddev
        if shift_in_sigma > 6.0:
            return DriftAssessment(
                blocked=True,
                reason=(
                    f"The new window's centre shifted {shift_in_sigma:.1f} standard deviations; "
                    "confirm before re-baselining."
                ),
                relative_shift=relative,
            )
        return DriftAssessment(blocked=False, reason=None, relative_shift=relative)

    if relative is not None and relative > max_relative_shift:
        return DriftAssessment(
            blocked=True,
            reason=(
                f"The new window's centre shifted {relative * 100:.0f}%; "
                "confirm before re-baselining."
            ),
            relative_shift=relative,
        )
    return DriftAssessment(blocked=False, reason=None, relative_shift=relative)


def _finite(value: float) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


__all__ = [
    "BaselineStatistics",
    "DriftAssessment",
    "assess_drift",
    "compute_statistics",
    "select_window",
]
