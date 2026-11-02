"""Layer 3 statistical primitives.

A small, dependency-free, deterministic statistics core. It is written in pure Python on
the standard library so it is auditable line by line and adds no supply-chain surface to a
security-first platform (see ``docs/LAYER3-ARCHITECTURE.md`` §0.2 for the dependency
decision).

Two rules run through every function:

* **No silent NaN.** A statistic that cannot be computed (too few samples, zero variance,
  non-finite input) returns ``None`` rather than ``nan`` or a fabricated zero, so a caller
  must decide what to do. ``nan`` propagates invisibly through arithmetic and would corrupt
  a downstream comparison.
* **Deterministic.** No randomness here; the ML detectors seed their own RNG explicitly.

Every public function documents its definition, so a reviewer can check the code against the
formula rather than trusting the name.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import fmean


def _finite(values: list[float]) -> list[float]:
    """Return only the finite values, preserving order."""
    return [v for v in values if isinstance(v, (int, float)) and math.isfinite(v)]


def mean(values: list[float]) -> float | None:
    """Arithmetic mean. ``None`` for an empty or non-finite-free sample."""
    clean = _finite(values)
    if not clean:
        return None
    return fmean(clean)


def median(values: list[float]) -> float | None:
    """Median (50th percentile). ``None`` for an empty sample."""
    clean = sorted(_finite(values))
    return _quantile_sorted(clean, 0.5)


def variance(values: list[float], *, sample: bool = True) -> float | None:
    """Variance. ``sample=True`` uses Bessel's correction (n-1).

    ``None`` when there are fewer samples than the divisor requires (n < 2 for the sample
    variance, n < 1 for the population variance).
    """
    clean = _finite(values)
    n = len(clean)
    if n == 0:
        return None
    mu = fmean(clean)
    total = sum((v - mu) ** 2 for v in clean)
    divisor = (n - 1) if sample else n
    if divisor <= 0:
        return None
    return total / divisor


def stddev(values: list[float], *, sample: bool = True) -> float | None:
    """Standard deviation. ``None`` when the variance cannot be computed."""
    var = variance(values, sample=sample)
    if var is None:
        return None
    return math.sqrt(var)


def _quantile_sorted(sorted_values: list[float], q: float) -> float | None:
    """Linear-interpolation quantile (the common "type 7" / numpy-default definition).

    ``q`` is clamped to [0, 1]. ``None`` for an empty sample.
    """
    if not sorted_values:
        return None
    if q <= 0:
        return sorted_values[0]
    if q >= 1:
        return sorted_values[-1]
    position = q * (len(sorted_values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    fraction = position - lower
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction


def quantile(values: list[float], q: float) -> float | None:
    """Quantile of ``values``. ``None`` for an empty sample."""
    clean = sorted(_finite(values))
    return _quantile_sorted(clean, q)


def percentile(values: list[float], p: float) -> float | None:
    """Percentile, where ``p`` is in [0, 100]."""
    return quantile(values, p / 100.0)


def iqr(values: list[float]) -> float | None:
    """Interquartile range (p75 - p25). ``None`` when it cannot be computed."""
    p25 = quantile(values, 0.25)
    p75 = quantile(values, 0.75)
    if p25 is None or p75 is None:
        return None
    return p75 - p25


def median_absolute_deviation(values: list[float]) -> float | None:
    """MAD = median(|x - median(x)|). A robust spread measure."""
    med = median(values)
    if med is None:
        return None
    return median([abs(v - med) for v in _finite(values)])


def z_score(value: float, mu: float | None, sigma: float | None) -> float | None:
    """Standard score: ``(value - mean) / stddev``.

    ``None`` when ``sigma`` is missing or zero: a zero-variance window has no meaningful
    standard score, and dividing would produce infinity or NaN.
    """
    if mu is None or sigma is None or sigma == 0:
        return None
    if not math.isfinite(value):
        return None
    return (value - mu) / sigma


def robust_z_score(value: float, med: float | None, mad: float | None) -> float | None:
    """Robust standard score: ``0.6745 * (value - median) / MAD``.

    The constant 0.6745 makes the MAD comparable to a standard deviation for normally
    distributed data. ``None`` when ``MAD`` is missing or zero.
    """
    if med is None or mad is None or mad == 0:
        return None
    if not math.isfinite(value):
        return None
    return 0.6745 * (value - med) / mad


def ewma(values: list[float], *, alpha: float) -> list[float] | None:
    """Exponentially weighted moving average.

    ``y_0 = x_0`` and ``y_t = alpha * x_t + (1 - alpha) * y_{t-1}``. Returns one value per
    input. ``None`` for an empty sample or an ``alpha`` outside (0, 1].
    """
    clean = _finite(values)
    if not clean or not (0 < alpha <= 1):
        return None
    out = [clean[0]]
    for value in clean[1:]:
        out.append(alpha * value + (1 - alpha) * out[-1])
    return out


def ewma_last(values: list[float], *, alpha: float) -> float | None:
    """The most recent EWMA value, or ``None`` when it cannot be computed."""
    series = ewma(values, alpha=alpha)
    if not series:
        return None
    return series[-1]


@dataclass(frozen=True, slots=True)
class CusumResult:
    """Result of a two-sided CUSUM change detection."""

    change_index: int | None
    """Index of the first sample that breached the decision threshold, or None."""
    positive_stat: list[float]
    negative_stat: list[float]


def cusum(
    values: list[float],
    *,
    target: float | None = None,
    slack: float = 0.5,
    threshold: float = 5.0,
) -> CusumResult | None:
    """Two-sided tabular CUSUM for a shift in the mean.

    ``values`` are standardized against ``target`` (default: the sample mean) and the
    sample standard deviation. With standardized ``z_t``:

        ``S+_t = max(0, S+_{t-1} + z_t - k)``
        ``S-_t = max(0, S-_{t-1} - z_t - k)``

    A change is signalled at the first ``t`` where either statistic exceeds ``h``. ``k`` is
    the slack (half the shift to detect) and ``h`` the decision threshold, both in standard
    deviations. ``None`` when there are too few samples or the standard deviation is zero.
    """
    clean = _finite(values)
    if len(clean) < 2:
        return None
    mu = target if target is not None else fmean(clean)
    sigma = stddev(clean)
    if sigma is None or sigma == 0:
        return None
    pos = 0.0
    neg = 0.0
    pos_series: list[float] = []
    neg_series: list[float] = []
    change_index: int | None = None
    for index, value in enumerate(clean):
        z = (value - mu) / sigma
        pos = max(0.0, pos + z - slack)
        neg = max(0.0, neg - z - slack)
        pos_series.append(pos)
        neg_series.append(neg)
        if change_index is None and (pos > threshold or neg > threshold):
            change_index = index
    return CusumResult(
        change_index=change_index, positive_stat=pos_series, negative_stat=neg_series
    )


@dataclass(frozen=True, slots=True)
class LinearFit:
    """Ordinary least-squares fit of ``y`` on ``x``."""

    slope: float
    intercept: float
    r_squared: float | None
    n: int


def linear_regression(xs: list[float], ys: list[float]) -> LinearFit | None:
    """Least-squares slope and intercept, plus the coefficient of determination.

    ``None`` when there are fewer than two paired points or ``x`` has zero variance (a
    vertical line has no slope). ``r_squared`` is ``None`` when ``y`` has zero variance
    (the fit is the mean and R² is undefined).
    """
    pairs = [
        (x, y)
        for x, y in zip(xs, ys, strict=False)
        if isinstance(x, (int, float))
        and isinstance(y, (int, float))
        and math.isfinite(x)
        and math.isfinite(y)
    ]
    n = len(pairs)
    if n < 2:
        return None
    mean_x = fmean(p[0] for p in pairs)
    mean_y = fmean(p[1] for p in pairs)
    sxx = sum((x - mean_x) ** 2 for x, _ in pairs)
    if sxx == 0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    syy = sum((y - mean_y) ** 2 for _, y in pairs)
    r_squared: float | None = None if syy == 0 else (sxy * sxy) / (sxx * syy)
    return LinearFit(slope=slope, intercept=intercept, r_squared=r_squared, n=n)


def pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson product-moment correlation coefficient, in [-1, 1].

    ``None`` when there are fewer than three paired points, or either series has zero
    variance. Requires a minimum of three points so a two-point "correlation" (which is
    always exactly ±1) is never reported.
    """
    pairs = [
        (x, y)
        for x, y in zip(xs, ys, strict=False)
        if isinstance(x, (int, float))
        and isinstance(y, (int, float))
        and math.isfinite(x)
        and math.isfinite(y)
    ]
    n = len(pairs)
    if n < 3:
        return None
    mean_x = fmean(p[0] for p in pairs)
    mean_y = fmean(p[1] for p in pairs)
    sxx = sum((x - mean_x) ** 2 for x, _ in pairs)
    syy = sum((y - mean_y) ** 2 for _, y in pairs)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    return sxy / math.sqrt(sxx * syy)


def _ranks(values: list[float]) -> list[float]:
    """Average ranks for ties (1-based), the input for Spearman correlation."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        average = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = average
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float | None:
    """Spearman rank correlation: Pearson correlation of the ranks.

    Robust to non-linearity and outliers. Same minimum-sample and variance rules as
    :func:`pearson`.
    """
    pairs = [
        (x, y)
        for x, y in zip(xs, ys, strict=False)
        if isinstance(x, (int, float))
        and isinstance(y, (int, float))
        and math.isfinite(x)
        and math.isfinite(y)
    ]
    if len(pairs) < 3:
        return None
    rank_x = _ranks([p[0] for p in pairs])
    rank_y = _ranks([p[1] for p in pairs])
    return pearson(rank_x, rank_y)


def rate_of_change(values: list[float], *, interval_seconds: float) -> float | None:
    """Per-second rate of change between the first and last sample.

    ``None`` for fewer than two samples or a non-positive interval. This is a first-to-last
    estimate, not a fitted slope; use :func:`linear_regression` when noise matters.
    """
    clean = _finite(values)
    if len(clean) < 2 or interval_seconds <= 0:
        return None
    return (clean[-1] - clean[0]) / interval_seconds


def percentage_change(previous: float, current: float) -> float | None:
    """Percentage change from ``previous`` to ``current``.

    ``None`` when ``previous`` is zero (the change is undefined, not infinite).
    """
    if previous == 0:
        return None
    return (current - previous) / abs(previous) * 100.0


def moving_average(values: list[float], *, window: int) -> list[float | None]:
    """Trailing moving average, one entry per input.

    The first ``window - 1`` entries are ``None`` because a full window is not yet
    available; a partial window is not emitted, so no value is computed from too little
    data. Returns an empty list for a non-positive window.
    """
    if window <= 0:
        return []
    out: list[float | None] = []
    for index in range(len(values)):
        if index + 1 < window:
            out.append(None)
            continue
        chunk = _finite(values[index + 1 - window : index + 1])
        out.append(fmean(chunk) if chunk else None)
    return out


def slope_per_second(xs_seconds: list[float], ys: list[float]) -> float | None:
    """Fitted slope of ``y`` against time in seconds. ``None`` when uncomputable."""
    fit = linear_regression(xs_seconds, ys)
    if fit is None:
        return None
    return fit.slope


__all__ = [
    "CusumResult",
    "LinearFit",
    "cusum",
    "ewma",
    "ewma_last",
    "iqr",
    "linear_regression",
    "mean",
    "median",
    "median_absolute_deviation",
    "moving_average",
    "pearson",
    "percentage_change",
    "percentile",
    "quantile",
    "rate_of_change",
    "robust_z_score",
    "slope_per_second",
    "spearman",
    "stddev",
    "variance",
    "z_score",
]
