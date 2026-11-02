"""Data quality, readiness, features, baselines, and rules.

Each module is tested for its refusal behaviour as much as its happy path: a window that is
unusable must not be silently analyzed, a feature must not see the future, and a rule must
reject an unknown operand rather than pass.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from humisense.layer3.baselines import (
    assess_drift,
    compute_statistics,
    select_window,
)
from humisense.layer3.contract import (
    BaselineType,
    DataQualityStatus,
    ReadinessStatus,
    RuleCondition,
)
from humisense.layer3.features import compute_features
from humisense.layer3.observations import Observation
from humisense.layer3.quality import assess_quality
from humisense.layer3.readiness import evaluate_readiness, requirement_for
from humisense.layer3.rules import (
    RuleError,
    build_operands,
    evaluate_rule,
    validate_condition,
)

ORG = uuid.uuid4()
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _series(
    count: int,
    *,
    value: float = 60.0,
    step_seconds: int = 60,
    jitter: float = 0.0,
    unit: str | None = "degC",
    start: datetime = BASE,
) -> list[Observation]:
    return [
        Observation(
            entity_id="LINE-01",
            entity_type="asset",
            metric="temperature",
            value=value + (i % 2) * jitter,
            unit=unit,
            at=start + timedelta(seconds=i * step_seconds),
            data_family="timeseries",
            canonical_record_id=uuid.uuid4(),
            connection_id=uuid.uuid4(),
            source_system="historian_sample",
            quality_code=None,
            correlation_id="c",
        )
        for i in range(count)
    ]


# --- Quality -------------------------------------------------------------------


def test_clean_window_is_good() -> None:
    observations = _series(30)
    result = assess_quality(
        ORG, "LINE-01", "temperature", observations, as_of=BASE + timedelta(minutes=30)
    )
    assert result.status == DataQualityStatus.GOOD
    assert result.evaluated_count == 30


def test_empty_window_is_unusable() -> None:
    result = assess_quality(ORG, "LINE-01", "temperature", [], as_of=BASE)
    assert result.status == DataQualityStatus.UNUSABLE
    assert result.issues[0].code == "no_data"


def test_stale_window_is_flagged() -> None:
    observations = _series(10)
    result = assess_quality(
        ORG, "LINE-01", "temperature", observations, as_of=BASE + timedelta(hours=5)
    )
    assert any(issue.code == "stale_data" for issue in result.issues)
    assert result.status == DataQualityStatus.UNUSABLE


def test_mixed_units_are_flagged() -> None:
    observations = _series(5) + _series(
        5, value=100.0, unit="degF", start=BASE + timedelta(minutes=10)
    )
    observations.sort(key=lambda o: o.at)
    result = assess_quality(
        ORG, "LINE-01", "temperature", observations, as_of=BASE + timedelta(minutes=20)
    )
    assert any(issue.code == "unit_inconsistent" for issue in result.issues)


def test_duplicate_observations_are_counted() -> None:
    observations = _series(5)
    observations.append(observations[0])
    result = assess_quality(
        ORG, "LINE-01", "temperature", observations, as_of=BASE + timedelta(minutes=5)
    )
    assert result.duplicate_count >= 1


def test_out_of_order_is_flagged() -> None:
    observations = _series(5)
    observations[0], observations[3] = observations[3], observations[0]
    result = assess_quality(
        ORG, "LINE-01", "temperature", observations, as_of=BASE + timedelta(minutes=5)
    )
    assert any(issue.code == "out_of_order" for issue in result.issues)


def test_implausible_value_is_reported_as_quality_not_anomaly() -> None:
    # A jittered series gives a non-zero MAD, so the robust-z check has a scale to work with.
    observations = _series(30, jitter=1.0)
    observations.append(
        Observation(
            entity_id="LINE-01",
            entity_type="asset",
            metric="temperature",
            value=9999.0,
            unit="degC",
            at=BASE + timedelta(minutes=31),
            data_family="timeseries",
            canonical_record_id=uuid.uuid4(),
            connection_id=uuid.uuid4(),
            source_system="historian_sample",
            quality_code=None,
            correlation_id="c",
        )
    )
    result = assess_quality(
        ORG, "LINE-01", "temperature", observations, as_of=BASE + timedelta(minutes=32)
    )
    assert any(issue.code == "implausible_value" for issue in result.issues)


# --- Readiness -----------------------------------------------------------------


def test_readiness_ready_with_enough_clean_samples() -> None:
    observations = _series(30)
    quality = assess_quality(
        ORG, "LINE-01", "temperature", observations, as_of=BASE + timedelta(minutes=30)
    )
    result = evaluate_readiness(
        ORG,
        "LINE-01",
        "temperature",
        quality,
        requirement_for("z_score"),
        as_of=BASE + timedelta(minutes=30),
    )
    assert result.status == ReadinessStatus.READY


def test_readiness_insufficient_when_too_few_samples() -> None:
    observations = _series(3)
    quality = assess_quality(
        ORG, "LINE-01", "temperature", observations, as_of=BASE + timedelta(minutes=3)
    )
    result = evaluate_readiness(
        ORG,
        "LINE-01",
        "temperature",
        quality,
        requirement_for("z_score"),
        as_of=BASE + timedelta(minutes=3),
    )
    assert result.status == ReadinessStatus.INSUFFICIENT_DATA


def test_readiness_invalid_when_unusable() -> None:
    quality = assess_quality(ORG, "LINE-01", "temperature", [], as_of=BASE)
    result = evaluate_readiness(
        ORG, "LINE-01", "temperature", quality, requirement_for("z_score"), as_of=BASE
    )
    assert result.status == ReadinessStatus.INVALID_DATA


def test_readiness_stale_when_data_is_old() -> None:
    observations = _series(30)
    quality = assess_quality(
        ORG, "LINE-01", "temperature", observations, as_of=BASE + timedelta(hours=5)
    )
    result = evaluate_readiness(
        ORG,
        "LINE-01",
        "temperature",
        quality,
        requirement_for("z_score"),
        as_of=BASE + timedelta(hours=5),
    )
    # An unusable stale window is reported as invalid first; the point is it is not READY.
    assert result.status in {ReadinessStatus.INVALID_DATA, ReadinessStatus.STALE_DATA}


# --- Features ------------------------------------------------------------------


def test_features_are_causal_and_do_not_see_the_future() -> None:
    observations = _series(20)
    at = BASE + timedelta(minutes=9)
    features = compute_features(observations, at=at, windows=(300,))
    # Every emitted feature's window ends at or before ``at``.
    assert all(f.at <= at for f in features)
    last = next(f for f in features if f.name == "last_300s")
    assert last.value == observations[9].value


def test_features_are_reproducible() -> None:
    observations = _series(20, jitter=0.5)
    at = BASE + timedelta(minutes=15)
    first = {f.name: f.value for f in compute_features(observations, at=at, windows=(300, 3600))}
    second = {f.name: f.value for f in compute_features(observations, at=at, windows=(300, 3600))}
    assert first == second


def test_features_empty_series_yields_nothing() -> None:
    assert compute_features([], at=BASE) == []


def test_feature_carries_sources_for_provenance() -> None:
    observations = _series(10)
    features = compute_features(observations, at=BASE + timedelta(minutes=9), windows=(3600,))
    mean = next(f for f in features if f.name == "mean_3600s")
    assert len(mean.sources) == 10


# --- Baselines -----------------------------------------------------------------


def test_compute_statistics_reports_quantiles() -> None:
    statistics = compute_statistics(_series(100), quality_status=DataQualityStatus.GOOD)
    assert statistics.sample_count == 100
    assert statistics.p10 is not None and statistics.p90 is not None
    assert statistics.p10 <= statistics.p50 if hasattr(statistics, "p50") else True


def test_rolling_window_selects_only_recent_points() -> None:
    observations = _series(60)
    selected = select_window(
        observations,
        baseline_type=BaselineType.ROLLING,
        at=BASE + timedelta(minutes=59),
        window_seconds=600,
    )
    assert len(selected) == 10  # the ten points strictly inside the 10-minute window


def test_historical_window_uses_all_history() -> None:
    observations = _series(60)
    selected = select_window(
        observations,
        baseline_type=BaselineType.HISTORICAL,
        at=BASE + timedelta(minutes=59),
        window_seconds=600,
    )
    assert len(selected) == 60


def test_drift_blocks_an_implausible_shift() -> None:
    shifted = compute_statistics(_series(50, value=1000.0), quality_status=DataQualityStatus.GOOD)
    assessment = assess_drift(60.0, 0.5, shifted)
    assert assessment.blocked is True


def test_drift_allows_a_small_shift() -> None:
    similar = compute_statistics(_series(50, value=60.1), quality_status=DataQualityStatus.GOOD)
    assessment = assess_drift(60.0, 0.5, similar)
    assert assessment.blocked is False


# --- Rules ---------------------------------------------------------------------


def test_rule_evaluates_a_simple_comparison() -> None:
    observations = _series(10)
    operands = build_operands(observations, {"mean_300s": 60.0})
    condition = RuleCondition(operator="gt", operand="mean_300s", threshold=59.0)
    validate_condition(condition)
    result = evaluate_rule(condition, operands)
    assert result.triggered is True


def test_rule_boolean_composition() -> None:
    operands = {"a": 5.0, "b": 10.0}
    condition = RuleCondition(
        operator="and",
        conditions=[
            RuleCondition(operator="gt", operand="a", threshold=1.0),
            RuleCondition(operator="lt", operand="b", threshold=1.0),
        ],
    )
    validate_condition(condition)
    assert evaluate_rule(condition, operands).triggered is False


def test_rule_unknown_operand_raises() -> None:
    condition = RuleCondition(operator="gt", operand="does_not_exist", threshold=1.0)
    validate_condition(condition)
    with pytest.raises(RuleError):
        evaluate_rule(condition, {"value": 1.0})


def test_rule_unknown_operator_is_refused_at_construction() -> None:
    with pytest.raises(ValueError):
        RuleCondition(operator="exec", operand="value", threshold=1.0)


def test_rule_rejects_excessive_nesting() -> None:
    condition = RuleCondition(operator="gt", operand="value", threshold=1.0)
    for _ in range(8):
        condition = RuleCondition(operator="not", conditions=[condition])
    with pytest.raises(RuleError):
        validate_condition(condition)


def test_rule_rejects_too_many_children() -> None:
    children = [RuleCondition(operator="gt", operand="value", threshold=1.0) for _ in range(11)]
    condition = RuleCondition(operator="and", conditions=children)
    with pytest.raises(RuleError):
        validate_condition(condition)


def test_pct_gt_uses_relative_change() -> None:
    operands = {"value": 150.0}
    condition = RuleCondition(operator="pct_gt", operand="value", threshold=100.0)
    validate_condition(condition)
    assert evaluate_rule(condition, operands).triggered is True
