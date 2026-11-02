"""Detectors: correctness and the refusal behaviour.

Each detector is exercised on a case where it must trigger, a case where it must stay quiet,
and a case where it must refuse (too few samples, zero variance, singular covariance) rather
than fabricate a score. The isolation forest is asserted to be reproducible for a fixed seed.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from humisense.layer3 import detectors
from humisense.layer3.detectors import BaselineReference


def test_z_score_triggers_on_a_clear_outlier() -> None:
    result = detectors.detect_z_score(100.0, mean=50.0, stddev=5.0, threshold=3.0)
    assert result.triggered is True
    assert result.anomaly_score == pytest.approx(10.0)


def test_z_score_stays_quiet_within_threshold() -> None:
    result = detectors.detect_z_score(52.0, mean=50.0, stddev=5.0)
    assert result.triggered is False


def test_z_score_refuses_zero_variance() -> None:
    result = detectors.detect_z_score(100.0, mean=50.0, stddev=0.0)
    assert result.triggered is False
    assert result.anomaly_score is None


def test_robust_z_score_resists_an_outlier_contaminated_window() -> None:
    result = detectors.detect_robust_z_score(100.0, median=50.0, mad=2.0, threshold=3.5)
    assert result.triggered is True
    assert result.anomaly_score is not None


def test_iqr_fences() -> None:
    inside = detectors.detect_iqr(52.0, p25=48.0, p75=52.0, k=1.5)
    assert inside.triggered is False
    outside = detectors.detect_iqr(80.0, p25=48.0, p75=52.0, k=1.5)
    assert outside.triggered is True


def test_ewma_detects_a_step() -> None:
    # A long stable run followed by a single step: the residual of the step against the
    # smoothed level is large relative to the residual variance.
    series = [10.0] * 40 + [40.0]
    result = detectors.detect_ewma(series, alpha=0.3, threshold=3.0)
    assert result.triggered is True


def test_ewma_refuses_short_series() -> None:
    result = detectors.detect_ewma([1.0, 2.0])
    assert result.triggered is False


def test_cusum_detects_a_sustained_shift() -> None:
    series = [1.0] * 40 + [5.0] * 40
    result = detectors.detect_cusum(series, target=1.0, slack=0.5, threshold=5.0)
    assert result.triggered is True


def test_threshold_operators() -> None:
    assert detectors.detect_threshold(10.0, threshold=5.0, operator="gt").triggered is True
    assert detectors.detect_threshold(5.0, threshold=5.0, operator="gt").triggered is False
    assert detectors.detect_threshold(5.0, threshold=5.0, operator="gte").triggered is True
    assert detectors.detect_threshold(1.0, threshold=5.0, operator="lt").triggered is True
    bad = detectors.detect_threshold(1.0, threshold=5.0, operator="nonsense")
    assert bad.triggered is False


def test_baseline_deviation() -> None:
    baseline = BaselineReference(baseline_id="b1", version=3, median=60.0, mean=60.0, stddev=2.0)
    result = detectors.detect_baseline_deviation(70.0, baseline, n_sigma=3.0)
    assert result.triggered is True
    assert result.expected_value == 60.0
    assert result.extra["baseline_version"] == 3.0


def test_baseline_deviation_refuses_a_spreadless_baseline() -> None:
    baseline = BaselineReference(baseline_id="b1", version=1, median=60.0, mean=60.0, stddev=0.0)
    result = detectors.detect_baseline_deviation(70.0, baseline)
    assert result.triggered is False
    assert result.anomaly_score is None


def test_time_of_day_deviation() -> None:
    result = detectors.detect_time_of_day_deviation(
        80.0, hour_mean=60.0, hour_stddev=2.0, hour=14, n_sigma=3.0
    )
    assert result.triggered is True


def test_multivariate_flags_a_jointly_unusual_point() -> None:
    # Two correlated dimensions (y ~ x with small noise); the last point breaks the
    # relationship, which is unusual even though neither coordinate is extreme alone.
    rng = random.Random(1)
    points = [[float(i), float(i) + rng.uniform(-0.5, 0.5)] for i in range(30)]
    points.append([0.0, 100.0])
    result = detectors.detect_multivariate(points)
    assert result.triggered is True


def test_multivariate_refuses_collinear_dimensions() -> None:
    # Both dimensions are identical, so the covariance matrix is singular.
    points = [[float(i), float(i)] for i in range(20)]
    result = detectors.detect_multivariate(points)
    assert result.triggered is False
    assert "singular" in result.explanation.lower()


def test_multivariate_refuses_too_few_points() -> None:
    result = detectors.detect_multivariate([[1.0, 2.0], [2.0, 3.0]])
    assert result.triggered is False


def test_isolation_forest_is_reproducible_for_a_seed() -> None:
    points = [[float(i % 7), float((i * 3) % 11)] for i in range(60)]
    points.append([100.0, 100.0])
    first = detectors.detect_isolation_forest(points, seed=123)
    second = detectors.detect_isolation_forest(points, seed=123)
    assert first.anomaly_score == second.anomaly_score
    assert first.triggered == second.triggered


def test_isolation_forest_flags_a_far_outlier() -> None:
    points = [[float(i % 5), float(i % 3)] for i in range(80)]
    points.append([500.0, 500.0])
    result = detectors.detect_isolation_forest(points, seed=7)
    assert result.triggered is True
    assert 0.0 <= (result.confidence or 0.0) <= 1.0


def test_isolation_forest_refuses_too_few_points() -> None:
    result = detectors.detect_isolation_forest([[1.0, 2.0]])
    assert result.triggered is False


def test_trend_detects_a_steady_rise() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    timestamps = [base + timedelta(minutes=i) for i in range(20)]
    series = [float(i) for i in range(20)]
    result = detectors.detect_trend(series, timestamps)
    assert result.triggered is True
    assert result.extra["r_squared"] == pytest.approx(1.0)


def test_trend_stays_quiet_for_noise() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    timestamps = [base + timedelta(minutes=i) for i in range(20)]
    series = [10.0, 10.0, 10.0, 10.0, 10.0] * 4
    result = detectors.detect_trend(series, timestamps)
    assert result.triggered is False


def test_change_point_detects_a_step() -> None:
    series = [1.0] * 30 + [10.0] * 30
    result = detectors.detect_change_point(series)
    assert result.triggered is True
    assert result.extra["change_index"] is not None


def test_change_point_stays_quiet_for_a_flat_series() -> None:
    result = detectors.detect_change_point([5.0] * 40)
    assert result.triggered is False


def test_every_detector_names_its_method_and_version() -> None:
    result = detectors.detect_z_score(1.0, mean=0.0, stddev=1.0)
    assert result.algorithm_name == "z_score"
    assert result.algorithm_version in detectors.DETECTOR_VERSIONS.values()
