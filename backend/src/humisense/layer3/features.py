"""Feature engineering: reproducible, versioned features with leakage guards.

A feature is a named, versioned function of a *bounded, causal* window of observations. Two
guards run through every feature:

* **No leakage.** A feature computed at time ``t`` may use only observations at or before
  ``t``. There is no forward-looking window anywhere: a feature that saw the future would
  make every downstream evaluation (forecast accuracy, anomaly precision) dishonest.
* **Reproducibility.** ``name`` + ``version`` + ``window_seconds`` + the input window fully
  determine the value. The same inputs must produce the same output, so a detection that
  cites a feature version can be re-derived.

Features that cannot be computed (too few points, zero variance) yield ``None`` and are not
emitted, rather than a sentinel value.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from humisense.layer3 import stats
from humisense.layer3.observations import Observation

FEATURE_VERSION = "1.0"
"""Version of the built-in feature set. Bumped when a definition changes."""


@dataclass(frozen=True, slots=True)
class FeatureDefinitionSpec:
    """A registered feature's identity and computation window."""

    name: str
    version: str
    method: str
    window_seconds: int
    unit: str | None = None


@dataclass(frozen=True, slots=True)
class ComputedFeature:
    """A feature value computed over a window, with its provenance."""

    name: str
    version: str
    value: float
    method: str
    window_seconds: int
    at: datetime
    sample_count: int
    sources: tuple[uuid.UUID, ...]


def _window(
    observations: list[Observation], at: datetime, window_seconds: int
) -> list[Observation]:
    """Return the observations in ``(at - window_seconds, at]``, causal by construction."""
    if window_seconds <= 0:
        return [o for o in observations if o.at <= at]
    lower = at - timedelta(seconds=window_seconds)
    return [o for o in observations if lower < o.at <= at]


def compute_features(
    observations: list[Observation],
    *,
    at: datetime,
    windows: tuple[int, ...] = (300, 3600),
) -> list[ComputedFeature]:
    """Compute the built-in feature set at time ``at`` over the given windows.

    ``observations`` must be the full series for one entity/metric, sorted ascending. Only
    observations at or before ``at`` are used.
    """
    results: list[ComputedFeature] = []
    series = [o for o in observations if o.at <= at]
    if not series:
        return results
    values_all = [o.value for o in series]

    for window_seconds in windows:
        window = _window(series, at, window_seconds)
        if not window:
            continue
        values = [o.value for o in window]
        sources = tuple(o.canonical_record_id for o in window)
        suffix = f"{window_seconds}s"
        at_value = window[-1].at

        def emit(
            name: str,
            value: float | None,
            method: str,
            *,
            _suffix: str = suffix,
            _window_seconds: int = window_seconds,
            _at: datetime = at_value,
            _count: int = len(window),
            _sources: tuple[uuid.UUID, ...] = sources,
        ) -> None:
            if value is None or not math.isfinite(value):
                return
            results.append(
                ComputedFeature(
                    name=f"{name}_{_suffix}",
                    version=FEATURE_VERSION,
                    value=float(value),
                    method=method,
                    window_seconds=_window_seconds,
                    at=_at,
                    sample_count=_count,
                    sources=_sources,
                )
            )

        emit("count", float(len(window)), "count")
        emit("mean", stats.mean(values), "mean")
        emit("median", stats.median(values), "median")
        emit("stddev", stats.stddev(values), "stddev")
        emit("min", min(values), "min")
        emit("max", max(values), "max")
        emit("range", max(values) - min(values), "range")
        emit("iqr", stats.iqr(values), "iqr")
        emit("p25", stats.quantile(values, 0.25), "quantile")
        emit("p75", stats.quantile(values, 0.75), "quantile")
        emit("last", values[-1], "last")

        elapsed = (window[-1].at - window[0].at).total_seconds()
        emit("roc", stats.rate_of_change(values, interval_seconds=elapsed), "rate_of_change")
        emit("pct_change", stats.percentage_change(values[0], values[-1]), "percentage_change")
        emit("ewma", stats.ewma_last(values, alpha=0.3), "ewma")

        fit = stats.linear_regression(
            [(o.at - window[0].at).total_seconds() for o in window], values
        )
        if fit is not None:
            emit("slope", fit.slope, "linear_regression")
            if fit.r_squared is not None:
                emit("r_squared", fit.r_squared, "linear_regression")

    # Robust centre and spread over the whole available history (still causal: series is
    # already truncated to at or before ``at``).
    med = stats.median(values_all)
    mad = stats.median_absolute_deviation(values_all)
    if med is not None:
        results.append(
            ComputedFeature(
                name="hist_median",
                version=FEATURE_VERSION,
                value=float(med),
                method="median",
                window_seconds=0,
                at=at,
                sample_count=len(series),
                sources=tuple(o.canonical_record_id for o in series),
            )
        )
    if mad is not None:
        results.append(
            ComputedFeature(
                name="hist_mad",
                version=FEATURE_VERSION,
                value=float(mad),
                method="median_absolute_deviation",
                window_seconds=0,
                at=at,
                sample_count=len(series),
                sources=tuple(o.canonical_record_id for o in series),
            )
        )

    return results


def builtin_feature_specs(windows: tuple[int, ...] = (300, 3600)) -> list[FeatureDefinitionSpec]:
    """Return the registered definitions of the built-in feature set."""
    specs: list[FeatureDefinitionSpec] = []
    names = (
        "count",
        "mean",
        "median",
        "stddev",
        "min",
        "max",
        "range",
        "iqr",
        "p25",
        "p75",
        "last",
        "roc",
        "pct_change",
        "ewma",
        "slope",
        "r_squared",
    )
    for window_seconds in windows:
        for name in names:
            specs.append(
                FeatureDefinitionSpec(
                    name=f"{name}_{window_seconds}s",
                    version=FEATURE_VERSION,
                    method=name,
                    window_seconds=window_seconds,
                )
            )
    for name, method in (("hist_median", "median"), ("hist_mad", "median_absolute_deviation")):
        specs.append(
            FeatureDefinitionSpec(
                name=name, version=FEATURE_VERSION, method=method, window_seconds=0
            )
        )
    return specs


__all__ = [
    "FEATURE_VERSION",
    "ComputedFeature",
    "FeatureDefinitionSpec",
    "builtin_feature_specs",
    "compute_features",
]
