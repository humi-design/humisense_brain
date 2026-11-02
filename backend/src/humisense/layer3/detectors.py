"""Anomaly detectors: pure-Python, deterministic, explainable.

Every detector takes an explicit, bounded input and returns a :class:`DetectorResult` that
names its method, its version, the value it observed, the value it expected, and why it
reached its conclusion. None of them reads the clock, the database, or the network, so each
is reproducible and unit-testable in isolation.

The set covers the families the Layer 3 specification asks for:

* univariate statistical: z-score, robust z-score, IQR, EWMA residual, CUSUM;
* baseline-relative: deviation from a versioned baseline, and time-of-day deviation;
* threshold: an explicit operator comparison;
* multivariate: Mahalanobis distance over a correlated feature vector;
* machine-learning-style: a dependency-free isolation forest;
* trend and change point.

When a detector cannot reach a conclusion (too few samples, zero variance, singular
covariance) it returns ``triggered=False`` with an explanation, never a fabricated score.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime

from humisense.layer3 import stats

DETECTOR_VERSIONS: dict[str, str] = {
    "z_score": "1.0.0",
    "robust_z_score": "1.0.0",
    "iqr": "1.0.0",
    "ewma": "1.0.0",
    "cusum": "1.0.0",
    "threshold": "1.0.0",
    "baseline_deviation": "1.0.0",
    "time_of_day_deviation": "1.0.0",
    "isolation_forest": "1.0.0",
    "multivariate": "1.0.0",
    "change_point": "1.0.0",
    "trend": "1.0.0",
}
"""The version of each detector's algorithm. Part of a result's reproducibility contract."""


@dataclass(slots=True)
class DetectorResult:
    """The output of one detector over one signal.

    ``anomaly_score`` is how unusual the value is under the method; it is not a probability
    and it is not a severity. ``confidence`` is set only where the method yields a
    calibrated figure (the isolation forest does; a threshold comparison does not).
    """

    algorithm_name: str
    algorithm_version: str
    triggered: bool
    observed_value: float | None = None
    expected_value: float | None = None
    deviation: float | None = None
    anomaly_score: float | None = None
    confidence: float | None = None
    explanation: str = ""
    limitations: list[str] = field(default_factory=list)
    extra: dict[str, float] = field(default_factory=dict)


def _version(name: str) -> str:
    return DETECTOR_VERSIONS.get(name, "1.0.0")


# --- Univariate statistical detectors -----------------------------------------


def detect_z_score(
    value: float, *, mean: float | None, stddev: float | None, threshold: float = 3.0
) -> DetectorResult:
    """Flag a value that is more than ``threshold`` standard deviations from the mean."""
    score = stats.z_score(value, mean, stddev)
    if score is None:
        return DetectorResult(
            "z_score",
            _version("z_score"),
            False,
            observed_value=value,
            expected_value=mean,
            explanation="The window has no usable variance; a z-score is undefined.",
            limitations=["A zero-variance window cannot produce a z-score."],
        )
    triggered = abs(score) > threshold
    return DetectorResult(
        "z_score",
        _version("z_score"),
        triggered,
        observed_value=value,
        expected_value=mean,
        deviation=score,
        anomaly_score=score,
        explanation=(
            f"value {value:.4g} is {score:.2f} standard deviations from the mean {mean:.4g}; "
            f"the threshold is {threshold:.2f}."
        ),
        limitations=["Assumes an approximately symmetric distribution."],
        extra={"threshold": threshold},
    )


def detect_robust_z_score(
    value: float, *, median: float | None, mad: float | None, threshold: float = 3.5
) -> DetectorResult:
    """Flag a value using the median and MAD, so outliers do not mask the anomaly."""
    score = stats.robust_z_score(value, median, mad)
    if score is None:
        return DetectorResult(
            "robust_z_score",
            _version("robust_z_score"),
            False,
            observed_value=value,
            expected_value=median,
            explanation="The window has no usable MAD; a robust z-score is undefined.",
            limitations=["A zero-MAD window cannot produce a robust z-score."],
        )
    triggered = abs(score) > threshold
    return DetectorResult(
        "robust_z_score",
        _version("robust_z_score"),
        triggered,
        observed_value=value,
        expected_value=median,
        deviation=score,
        anomaly_score=score,
        explanation=(
            f"value {value:.4g} is {score:.2f} robust deviations from the median {median:.4g}; "
            f"the threshold is {threshold:.2f}."
        ),
        limitations=["Robust to a minority of outliers, not to a shifted baseline."],
        extra={"threshold": threshold},
    )


def detect_iqr(
    value: float, *, p25: float | None, p75: float | None, k: float = 1.5
) -> DetectorResult:
    """Flag a value outside the Tukey fences ``[p25 - k*IQR, p75 + k*IQR]``."""
    if p25 is None or p75 is None:
        return DetectorResult(
            "iqr",
            _version("iqr"),
            False,
            observed_value=value,
            explanation="Quartiles are unavailable; the IQR test cannot run.",
        )
    iqr = p75 - p25
    lower = p25 - k * iqr
    upper = p75 + k * iqr
    triggered = value < lower or value > upper
    # Score as distance beyond the nearest fence, in IQR units.
    if value > upper:
        score = (value - upper) / iqr if iqr > 0 else None
    elif value < lower:
        score = (lower - value) / iqr if iqr > 0 else None
    else:
        score = 0.0
    return DetectorResult(
        "iqr",
        _version("iqr"),
        triggered,
        observed_value=value,
        expected_value=(p25 + p75) / 2.0,
        anomaly_score=score,
        explanation=(
            f"value {value:.4g} against fences [{lower:.4g}, {upper:.4g}] "
            f"from p25={p25:.4g}, p75={p75:.4g}."
        ),
        limitations=["An IQR of zero collapses the fences onto the quartiles."],
        extra={"lower": lower, "upper": upper, "iqr": iqr},
    )


def detect_ewma(
    series: list[float], *, alpha: float = 0.3, threshold: float = 3.0
) -> DetectorResult:
    """Flag the latest value when its residual against the EWMA is large.

    The residual is standardized by the residual's own standard deviation, so the threshold
    is comparable across signals of different scale.
    """
    if len(series) < 5 or not 0.0 < alpha < 1.0:
        return DetectorResult(
            "ewma",
            _version("ewma"),
            False,
            explanation="At least five samples and an alpha in (0, 1) are required.",
        )
    smoothed = stats.ewma(series, alpha=alpha)
    if smoothed is None:
        return DetectorResult("ewma", _version("ewma"), False, explanation="EWMA is undefined.")
    residuals = [series[i] - smoothed[i] for i in range(len(series))]
    residual_stddev = stats.stddev(residuals)
    latest_residual = residuals[-1]
    if residual_stddev in (None, 0):
        return DetectorResult(
            "ewma",
            _version("ewma"),
            False,
            observed_value=series[-1],
            expected_value=smoothed[-1],
            explanation="The residuals have no variance; the EWMA test cannot run.",
        )
    assert residual_stddev is not None  # guarded above
    score = latest_residual / residual_stddev
    triggered = abs(score) > threshold
    return DetectorResult(
        "ewma",
        _version("ewma"),
        triggered,
        observed_value=series[-1],
        expected_value=smoothed[-1],
        deviation=latest_residual,
        anomaly_score=score,
        explanation=(
            f"the latest residual {latest_residual:.4g} is {score:.2f} residual deviations "
            f"from the EWMA {smoothed[-1]:.4g}."
        ),
        limitations=[
            "Sensitive to the choice of alpha; detects sustained shifts better than spikes."
        ],
        extra={"alpha": alpha, "threshold": threshold},
    )


def detect_cusum(
    series: list[float], *, target: float | None = None, slack: float = 0.5, threshold: float = 5.0
) -> DetectorResult:
    """Flag a sustained shift in the mean using a two-sided CUSUM."""
    result = stats.cusum(series, target=target, slack=slack, threshold=threshold)
    if result is None:
        return DetectorResult(
            "cusum",
            _version("cusum"),
            False,
            explanation="CUSUM needs at least two samples with non-zero variance.",
        )
    triggered = result.change_index is not None
    return DetectorResult(
        "cusum",
        _version("cusum"),
        triggered,
        observed_value=series[-1],
        expected_value=target,
        anomaly_score=max(result.positive_stat[-1], result.negative_stat[-1]),
        explanation=(
            "a sustained shift was detected."
            if triggered
            else "no sustained shift exceeded the decision threshold."
        ),
        limitations=["Reports the first crossing, which is the shift's onset, not its peak."],
        extra={"slack": slack, "threshold": threshold},
    )


def detect_threshold(value: float, *, threshold: float, operator: str = "gt") -> DetectorResult:
    """Flag an explicit threshold breach. This is an operator rule, not a statistic."""
    if operator == "gt":
        triggered = value > threshold
    elif operator == "gte":
        triggered = value >= threshold
    elif operator == "lt":
        triggered = value < threshold
    elif operator == "lte":
        triggered = value <= threshold
    else:
        return DetectorResult(
            "threshold",
            _version("threshold"),
            False,
            observed_value=value,
            explanation=f"Unsupported threshold operator {operator!r}.",
        )
    return DetectorResult(
        "threshold",
        _version("threshold"),
        triggered,
        observed_value=value,
        expected_value=threshold,
        deviation=value - threshold,
        explanation=f"value {value:.4g} {operator} threshold {threshold:.4g} is {triggered}.",
        limitations=["A threshold rule carries no notion of how unusual a value is."],
    )


# --- Baseline-relative detectors ----------------------------------------------


@dataclass(frozen=True, slots=True)
class BaselineReference:
    """The baseline statistics a baseline-relative detector compares against."""

    baseline_id: str | None
    version: int
    median: float | None
    mean: float | None
    stddev: float | None
    mad: float | None = None
    p25: float | None = None
    p75: float | None = None


def detect_baseline_deviation(
    value: float, baseline: BaselineReference, *, n_sigma: float = 3.0
) -> DetectorResult:
    """Flag a value that departs from a versioned baseline by more than ``n_sigma``."""
    scale = baseline.stddev
    centre = baseline.mean if baseline.mean is not None else baseline.median
    if centre is None or scale in (None, 0):
        return DetectorResult(
            "baseline_deviation",
            _version("baseline_deviation"),
            False,
            observed_value=value,
            expected_value=centre,
            explanation="The baseline has no usable centre or scale.",
            limitations=["A baseline with no spread cannot yield a deviation."],
        )
    assert scale is not None  # guarded above
    deviation = value - centre
    sigma = deviation / scale
    triggered = abs(sigma) > n_sigma
    return DetectorResult(
        "baseline_deviation",
        _version("baseline_deviation"),
        triggered,
        observed_value=value,
        expected_value=centre,
        deviation=sigma,
        anomaly_score=sigma,
        explanation=(
            f"value {value:.4g} deviates {sigma:.2f} baseline deviations from {centre:.4g} "
            f"(baseline v{baseline.version}); the threshold is {n_sigma:.2f}."
        ),
        limitations=["A stale baseline produces confident but wrong deviations."],
        extra={"n_sigma": n_sigma, "baseline_version": float(baseline.version)},
    )


def detect_time_of_day_deviation(
    value: float,
    *,
    hour_mean: float | None,
    hour_stddev: float | None,
    hour: int,
    n_sigma: float = 3.0,
) -> DetectorResult:
    """Flag a value that is unusual for its time of day, ignoring the daily cycle."""
    score = stats.z_score(value, hour_mean, hour_stddev)
    if score is None:
        return DetectorResult(
            "time_of_day_deviation",
            _version("time_of_day_deviation"),
            False,
            observed_value=value,
            expected_value=hour_mean,
            explanation=f"No usable baseline for hour {hour:02d}.",
        )
    triggered = abs(score) > n_sigma
    return DetectorResult(
        "time_of_day_deviation",
        _version("time_of_day_deviation"),
        triggered,
        observed_value=value,
        expected_value=hour_mean,
        deviation=score,
        anomaly_score=score,
        explanation=(
            f"value {value:.4g} is {score:.2f} deviations from the hour-{hour:02d} mean "
            f"{hour_mean:.4g}."
        ),
        limitations=["A contextual anomaly: the value may be normal in absolute terms."],
        extra={"hour": float(hour), "n_sigma": n_sigma},
    )


# --- Multivariate -------------------------------------------------------------


def _mahalanobis(
    point: list[float], centre: list[float], inverse_covariance: list[list[float]]
) -> float | None:
    diff = [point[i] - centre[i] for i in range(len(point))]
    total = 0.0
    for i, di in enumerate(diff):
        row_sum = sum(inverse_covariance[i][j] * diff[j] for j in range(len(diff)))
        total += di * row_sum
    if total < 0 or not math.isfinite(total):
        return None
    return math.sqrt(total)


def _invert_matrix(matrix: list[list[float]]) -> list[list[float]] | None:
    """Gauss-Jordan inversion for a small square matrix. ``None`` when singular."""
    n = len(matrix)
    augmented = [
        row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(matrix)
    ]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(augmented[r][col]))
        if abs(augmented[pivot][col]) < 1e-12:
            return None
        augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        pivot_value = augmented[col][col]
        augmented[col] = [v / pivot_value for v in augmented[col]]
        for row in range(n):
            if row == col:
                continue
            factor = augmented[row][col]
            if factor != 0.0:
                augmented[row] = [
                    augmented[row][k] - factor * augmented[col][k] for k in range(2 * n)
                ]
    return [row[n:] for row in augmented]


def detect_multivariate(
    points: list[list[float]], *, threshold: float | None = None
) -> DetectorResult:
    """Flag the latest point by its Mahalanobis distance from the window's centre.

    The distance accounts for correlation between dimensions, so two individually normal
    values that are unusual *together* are flagged. The threshold defaults to a chi-square
    approximation for the dimension count.
    """
    dimensions = len(points[0]) if points else 0
    if len(points) < max(dimensions + 2, 5):
        return DetectorResult(
            "multivariate",
            _version("multivariate"),
            False,
            explanation=(
                f"At least {max(dimensions + 2, 5)} points are needed to estimate the "
                f"covariance of {dimensions} dimensions."
            ),
        )
    centre = [stats.mean([p[d] for p in points]) or 0.0 for d in range(dimensions)]
    covariance = _covariance(points, centre)
    inverse = _invert_matrix(covariance)
    if inverse is None:
        return DetectorResult(
            "multivariate",
            _version("multivariate"),
            False,
            explanation="The covariance matrix is singular; the dimensions are collinear.",
            limitations=["Collinear dimensions cannot be separated by Mahalanobis distance."],
        )
    distance = _mahalanobis(points[-1], centre, inverse)
    if distance is None:
        return DetectorResult(
            "multivariate",
            _version("multivariate"),
            False,
            explanation="The Mahalanobis distance is undefined for this window.",
        )
    cutoff = threshold if threshold is not None else _chi_square_cutoff(dimensions)
    triggered = distance > cutoff
    return DetectorResult(
        "multivariate",
        _version("multivariate"),
        triggered,
        anomaly_score=distance,
        explanation=(
            f"the latest point's Mahalanobis distance {distance:.3f} is compared with the "
            f"cutoff {cutoff:.3f} for {dimensions} dimensions."
        ),
        limitations=["Assumes an approximately elliptical distribution; a mixture defeats it."],
        extra={"cutoff": cutoff, "dimensions": float(dimensions)},
    )


def _covariance(points: list[list[float]], centre: list[float]) -> list[list[float]]:
    n = len(points)
    dimensions = len(centre)
    covariance = [[0.0] * dimensions for _ in range(dimensions)]
    for point in points:
        for i in range(dimensions):
            for j in range(dimensions):
                covariance[i][j] += (point[i] - centre[i]) * (point[j] - centre[j])
    divisor = max(n - 1, 1)
    return [[covariance[i][j] / divisor for j in range(dimensions)] for i in range(dimensions)]


def _chi_square_cutoff(dimensions: int) -> float:
    """The Mahalanobis *distance* cutoff at roughly the 99.5th percentile.

    The squared distance is chi-square distributed with ``dimensions`` degrees of freedom.
    The chi-square quantile is approximated by the Wilson-Hilferty transform, which is
    accurate for the small dimension counts this detector sees and needs no SciPy. The
    square root converts the squared-distance quantile to a distance cutoff.
    """
    z = 2.5758293035489004  # standard-normal 99.5th percentile
    df = float(dimensions)
    wilson_hilferty = df * (1.0 - 2.0 / (9.0 * df) + z * math.sqrt(2.0 / (9.0 * df))) ** 3
    return math.sqrt(max(wilson_hilferty, 0.0))


# --- Isolation forest ---------------------------------------------------------


@dataclass(slots=True)
class _IsolationNode:
    size: int
    split_feature: int | None = None
    split_value: float | None = None
    left: _IsolationNode | None = None
    right: _IsolationNode | None = None


def _harmonic(number: int) -> float:
    return math.log(number) + 0.5772156649  # Euler-Mascheroni constant


def _average_path_length(n: int) -> float:
    if n <= 1:
        return 0.0
    if n == 2:
        return 1.0
    return 2.0 * _harmonic(n - 1) - 2.0 * (n - 1) / n


def _build_tree(
    points: list[list[float]], depth: int, max_depth: int, rng: random.Random
) -> _IsolationNode:
    if depth >= max_depth or len(points) <= 1:
        return _IsolationNode(size=len(points))
    dimensions = len(points[0])
    feature = rng.randrange(dimensions)
    values = [p[feature] for p in points]
    low, high = min(values), max(values)
    if low == high:
        return _IsolationNode(size=len(points))
    split = rng.uniform(low, high)
    left = [p for p in points if p[feature] < split]
    right = [p for p in points if p[feature] >= split]
    if not left or not right:
        return _IsolationNode(size=len(points))
    return _IsolationNode(
        size=len(points),
        split_feature=feature,
        split_value=split,
        left=_build_tree(left, depth + 1, max_depth, rng),
        right=_build_tree(right, depth + 1, max_depth, rng),
    )


def _path_length(point: list[float], node: _IsolationNode, depth: int) -> float:
    if node.split_feature is None or node.left is None or node.right is None:
        return depth + _average_path_length(node.size)
    if point[node.split_feature] < (node.split_value or 0.0):
        return _path_length(point, node.left, depth + 1)
    return _path_length(point, node.right, depth + 1)


def detect_isolation_forest(
    points: list[list[float]],
    *,
    n_trees: int = 50,
    sample_size: int = 64,
    seed: int = 20260101,
) -> DetectorResult:
    """Flag the latest point using a dependency-free isolation forest.

    The score is ``2 ** (-E[h] / c(n))``, in [0, 1]; values above 0.5 are conventionally
    anomalous. The forest is seeded, so the result is reproducible for the same input.
    """
    if len(points) < 10:
        return DetectorResult(
            "isolation_forest",
            _version("isolation_forest"),
            False,
            explanation="An isolation forest needs at least ten points.",
        )
    dimensions = len(points[0])
    rng = random.Random(seed)  # noqa: S311 - reproducibility, not security, is the goal
    effective_sample = min(sample_size, len(points))
    max_depth = max(1, math.ceil(math.log2(max(effective_sample, 2))))
    trees: list[_IsolationNode] = []
    for _ in range(n_trees):
        sample = rng.sample(points, effective_sample)
        trees.append(_build_tree(sample, 0, max_depth, rng))
    normalizer = _average_path_length(effective_sample)
    if normalizer <= 0:
        return DetectorResult(
            "isolation_forest",
            _version("isolation_forest"),
            False,
            explanation="The forest's normalizing constant is degenerate for this sample.",
        )
    path = sum(_path_length(points[-1], tree, 0) for tree in trees) / len(trees)
    score = 2.0 ** (-path / normalizer)
    triggered = score > 0.5
    return DetectorResult(
        "isolation_forest",
        _version("isolation_forest"),
        triggered,
        anomaly_score=score,
        confidence=score,
        explanation=(
            f"the latest point's isolation score is {score:.4f} over {n_trees} trees "
            f"(above 0.5 is anomalous)."
        ),
        limitations=[
            "The score is a heuristic, not a calibrated probability.",
            "Deterministic only for the same seed and sample size.",
        ],
        extra={"dimensions": float(dimensions), "n_trees": float(n_trees)},
    )


# --- Trend and change point ---------------------------------------------------


def detect_trend(
    series: list[float], timestamps: list[datetime], *, min_r_squared: float = 0.5
) -> DetectorResult:
    """Flag a monotonic trend in the series by the fit of a least-squares line."""
    if len(series) < 5 or len(series) != len(timestamps):
        return DetectorResult(
            "trend",
            _version("trend"),
            False,
            explanation="A trend needs at least five aligned samples.",
        )
    base = timestamps[0]
    xs = [(t - base).total_seconds() for t in timestamps]
    fit = stats.linear_regression(xs, series)
    if fit is None or fit.r_squared is None:
        return DetectorResult(
            "trend",
            _version("trend"),
            False,
            explanation="The series is too flat or too short to fit a trend.",
        )
    direction = "increasing" if fit.slope > 0 else "decreasing" if fit.slope < 0 else "stable"
    triggered = fit.r_squared >= min_r_squared and fit.slope != 0.0
    return DetectorResult(
        "trend",
        _version("trend"),
        triggered,
        observed_value=series[-1],
        expected_value=fit.intercept + fit.slope * xs[-1],
        anomaly_score=fit.r_squared,
        explanation=(
            f"a {direction} trend of {fit.slope:.4g} per second fits the window with "
            f"R^2={fit.r_squared:.3f}."
        ),
        limitations=["A linear fit misses non-linear trends and saturating signals."],
        extra={"slope": fit.slope, "r_squared": fit.r_squared},
    )


def detect_change_point(
    series: list[float], *, slack: float = 0.5, threshold: float = 5.0
) -> DetectorResult:
    """Flag a change point using a two-sided CUSUM over the series."""
    result = stats.cusum(series, slack=slack, threshold=threshold)
    if result is None or result.change_index is None:
        return DetectorResult(
            "change_point",
            _version("change_point"),
            False,
            explanation="No change point exceeded the decision threshold.",
        )
    index = result.change_index
    previous = stats.mean(series[:index]) if index > 0 else None
    new = stats.mean(series[index:])
    magnitude = (new - previous) if (new is not None and previous is not None) else None
    return DetectorResult(
        "change_point",
        _version("change_point"),
        True,
        observed_value=new,
        expected_value=previous,
        deviation=magnitude,
        anomaly_score=max(result.positive_stat[-1], result.negative_stat[-1]),
        explanation=(
            f"a change point was detected at index {index}: the mean moved from "
            f"{previous:.4g} to {new:.4g}."
            if previous is not None
            else f"a change point was detected at index {index}."
        ),
        limitations=[
            "CUSUM finds the first sustained shift; several shifts need segmentation.",
        ],
        extra={
            "change_index": float(index),
            "previous_mean": previous or 0.0,
            "new_mean": new or 0.0,
        },
    )


__all__ = [
    "DETECTOR_VERSIONS",
    "BaselineReference",
    "DetectorResult",
    "detect_baseline_deviation",
    "detect_change_point",
    "detect_cusum",
    "detect_ewma",
    "detect_iqr",
    "detect_isolation_forest",
    "detect_multivariate",
    "detect_robust_z_score",
    "detect_threshold",
    "detect_time_of_day_deviation",
    "detect_trend",
    "detect_z_score",
]
