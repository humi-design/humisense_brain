"""Statistics core: correctness, edge cases, and the no-silent-NaN rule.

The statistical core is the foundation every detector rests on, so it is tested against
known values and against the cases that produce a fabricated answer if handled carelessly:
empty samples, a single sample, zero variance, and non-finite input.
"""

from __future__ import annotations

import math

import pytest

from humisense.layer3 import stats


def test_mean_median_stddev_match_known_values() -> None:
    values = [2, 4, 4, 4, 5, 5, 7, 9]
    assert stats.mean(values) == pytest.approx(5.0)
    assert stats.median(values) == pytest.approx(4.5)  # even count: midpoint of 4 and 5
    assert stats.stddev(values) == pytest.approx(2.138089935299395)


def test_quantiles_use_linear_interpolation() -> None:
    values = [1, 2, 3, 4, 5]
    assert stats.median(values) == 3
    assert stats.quantile(values, 0.25) == 2
    assert stats.quantile(values, 0.75) == 4
    assert stats.iqr(values) == 2


def test_quantile_clamps_out_of_range() -> None:
    values = [1, 2, 3]
    assert stats.quantile(values, -1) == 1
    assert stats.quantile(values, 2) == 3


def test_empty_and_single_sample_return_none_not_nan() -> None:
    assert stats.mean([]) is None
    assert stats.median([]) is None
    assert stats.stddev([]) is None
    assert stats.variance([1.0]) is None  # sample variance needs n >= 2
    assert stats.stddev([5.0]) is None


def test_stddev_of_constant_series_is_zero() -> None:
    assert stats.stddev([3.0, 3.0, 3.0]) == 0.0


def test_non_finite_values_are_excluded() -> None:
    assert stats.mean([1.0, float("nan"), 3.0]) == 2.0
    assert stats.mean([1.0, float("inf")]) == 1.0
    assert stats.median([float("nan")]) is None


def test_z_score_refuses_zero_variance() -> None:
    assert stats.z_score(9.0, 5.0, 2.0) == pytest.approx(2.0)
    assert stats.z_score(9.0, 5.0, 0.0) is None
    assert stats.z_score(9.0, None, 2.0) is None
    assert stats.z_score(float("nan"), 5.0, 2.0) is None


def test_robust_z_score_uses_the_mad_constant() -> None:
    assert stats.robust_z_score(9.0, 5.0, 2.0) == pytest.approx(0.6745 * 4.0 / 2.0)
    assert stats.robust_z_score(9.0, 5.0, 0.0) is None


def test_median_absolute_deviation() -> None:
    # median is 3; deviations are [2,1,0,1,2]; median deviation is 1.
    assert stats.median_absolute_deviation([1, 2, 3, 4, 5]) == 1


def test_ewma_recursion_and_last_value() -> None:
    series = stats.ewma([1, 2, 3, 4, 5], alpha=0.5)
    assert series is not None
    # y0=1, y1=1.5, y2=2.25, y3=3.125, y4=4.0625
    assert series[-1] == pytest.approx(4.0625)
    assert stats.ewma_last([1, 2, 3, 4, 5], alpha=0.5) == pytest.approx(4.0625)
    assert stats.ewma_last([], alpha=0.5) is None
    assert stats.ewma_last([1, 2], alpha=0.0) is None


def test_cusum_detects_a_sustained_shift() -> None:
    # Target the pre-change mean explicitly. With the default (overall) mean, the low
    # regime already deviates, so the first crossing would signal the start, not the shift.
    values = [1.0] * 40 + [5.0] * 40
    result = stats.cusum(values, target=1.0, slack=0.5, threshold=5.0)
    assert result is not None
    assert result.change_index is not None
    assert result.change_index >= 40


def test_cusum_none_for_zero_variance() -> None:
    assert stats.cusum([2, 2, 2, 2]) is None
    assert stats.cusum([1.0]) is None


def test_linear_regression_recovers_a_line() -> None:
    fit = stats.linear_regression([0, 1, 2, 3], [1, 3, 5, 7])
    assert fit is not None
    assert fit.slope == pytest.approx(2.0)
    assert fit.intercept == pytest.approx(1.0)
    assert fit.r_squared == pytest.approx(1.0)


def test_linear_regression_refuses_vertical_line_and_short_series() -> None:
    assert stats.linear_regression([1, 1, 1], [1, 2, 3]) is None
    assert stats.linear_regression([0], [1]) is None


def test_linear_regression_r_squared_none_for_constant_y() -> None:
    fit = stats.linear_regression([0, 1, 2], [4, 4, 4])
    assert fit is not None
    assert fit.slope == 0.0
    assert fit.r_squared is None


def test_pearson_and_spearman() -> None:
    assert stats.pearson([1, 2, 3, 4, 5], [2, 4, 6, 8, 10]) == pytest.approx(1.0)
    assert stats.pearson([1, 2, 3, 4, 5], [10, 8, 6, 4, 2]) == pytest.approx(-1.0)
    # Spearman is robust to a monotonic non-linear transform.
    assert stats.spearman([1, 2, 3, 4, 5], [1, 4, 9, 16, 25]) == pytest.approx(1.0)


def test_correlation_needs_three_points() -> None:
    assert stats.pearson([1, 2], [1, 2]) is None
    assert stats.spearman([1, 2], [1, 2]) is None


def test_percentage_change_undefined_against_zero() -> None:
    assert stats.percentage_change(0, 5) is None
    assert stats.percentage_change(10, 15) == pytest.approx(50.0)


def test_moving_average_leaves_partial_windows_unfilled() -> None:
    result = stats.moving_average([1, 2, 3, 4], window=2)
    assert result[0] is None
    assert result[1] == pytest.approx(1.5)
    assert result[3] == pytest.approx(3.5)
    assert stats.moving_average([1, 2], window=0) == []


def test_rate_of_change() -> None:
    assert stats.rate_of_change([0, 10], interval_seconds=2) == pytest.approx(5.0)
    assert stats.rate_of_change([0], interval_seconds=2) is None
    assert stats.rate_of_change([0, 10], interval_seconds=0) is None


def test_no_function_returns_nan() -> None:
    """A statistical function must never return NaN: it returns None or a finite value."""
    samples = [[], [1.0], [1.0, 1.0, 1.0], [float("nan"), 1.0, 2.0]]
    for values in samples:
        for result in (
            stats.mean(values),
            stats.median(values),
            stats.stddev(values),
            stats.iqr(values),
            stats.median_absolute_deviation(values),
            stats.quantile(values, 0.5),
        ):
            assert result is None or math.isfinite(result)
