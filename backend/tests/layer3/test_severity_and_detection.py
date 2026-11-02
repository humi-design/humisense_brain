"""Severity scoring and detection-event construction.

The severity tests assert the promise that a high score is not automatically CRITICAL, and
that a detection on dirty data is capped. The event tests assert provenance-by-reference,
bounded evidence, separate score/confidence/severity, and that a non-triggered result cannot
become a detection.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from humisense.layer3.contract import (
    ANALYTICS_SCHEMA_VERSION,
    DataQualityStatus,
    DetectionCategory,
    Domain,
    EvidenceReference,
    Severity,
)
from humisense.layer3.detection import (
    DetectionContext,
    GroupingPolicy,
    GroupState,
    build_detection_event,
    build_evidence,
    group_key_for,
    should_emit,
    suppression_until,
)
from humisense.layer3.detectors import DetectorResult
from humisense.layer3.observations import Observation
from humisense.layer3.severity import assess_severity, rarity_from_z

ORG = uuid.uuid4()
BASE = datetime(2026, 1, 1, tzinfo=UTC)


# --- Severity ------------------------------------------------------------------


def test_a_single_large_spike_is_not_automatically_critical() -> None:
    """A one-sample deviation, however large, is not CRITICAL without persistence."""
    assessment = assess_severity(magnitude=6.0, persistence=1, duration_seconds=0.0)
    assert assessment.severity in {Severity.LOW, Severity.MEDIUM}


def test_persistence_raises_severity() -> None:
    from humisense.layer3.contract import SEVERITY_ORDER

    quiet = assess_severity(magnitude=4.5, persistence=1)
    persistent = assess_severity(magnitude=4.5, persistence=25, duration_seconds=7200)
    assert SEVERITY_ORDER.index(persistent.severity) > SEVERITY_ORDER.index(quiet.severity)
    assert "persistence" in persistent.factors


def test_a_very_large_magnitude_reaches_critical_when_persistent() -> None:
    assessment = assess_severity(magnitude=15.0, persistence=30, duration_seconds=7200)
    assert assessment.severity == Severity.CRITICAL


def test_degraded_data_caps_severity_below_critical() -> None:
    assessment = assess_severity(
        magnitude=20.0,
        persistence=30,
        duration_seconds=7200,
        data_quality=DataQualityStatus.DEGRADED,
    )
    assert assessment.severity != Severity.CRITICAL
    assert "quality_cap" in assessment.factors


def test_rarity_raises_severity_for_extreme_values() -> None:
    assessment = assess_severity(magnitude=4.5, persistence=1, rarity=0.0005)
    assert "rarity" in assessment.factors


def test_severity_is_deterministic() -> None:
    first = assess_severity(magnitude=5.0, persistence=10, duration_seconds=1000)
    second = assess_severity(magnitude=5.0, persistence=10, duration_seconds=1000)
    assert first.severity == second.severity


def test_rarity_from_z_is_bounded_and_monotonic() -> None:
    assert rarity_from_z(0.0) == pytest.approx(1.0)
    assert rarity_from_z(3.0) is not None and rarity_from_z(6.0) is not None
    assert rarity_from_z(6.0) < rarity_from_z(3.0)
    assert 0.0 <= rarity_from_z(10.0) <= 1.0
    assert rarity_from_z(None) is None


# --- Detection events ----------------------------------------------------------


def _context(**overrides: object) -> DetectionContext:
    values: dict[str, object] = {
        "organization_id": ORG,
        "domain": Domain.INDUSTRIAL,
        "entity_id": "LINE-01",
        "metric": "temperature",
        "observed_value": 100.0,
        "unit": "degC",
        "detected_at": BASE,
        "observation_window_start": BASE - timedelta(minutes=5),
        "observation_window_end": BASE,
        "correlation_id": "corr",
        "source_system": "historian_sample",
    }
    values.update(overrides)
    return DetectionContext(**values)  # type: ignore[arg-type]


def test_building_an_event_from_a_non_triggered_result_is_refused() -> None:
    result = DetectorResult("z_score", "1.0.0", triggered=False)
    with pytest.raises(ValueError):
        build_detection_event(_context(), result)


def test_event_carries_method_version_and_schema_version() -> None:
    result = DetectorResult(
        "z_score",
        "1.0.0",
        triggered=True,
        observed_value=100.0,
        expected_value=60.0,
        deviation=8.0,
        anomaly_score=8.0,
    )
    event = build_detection_event(_context(), result)
    assert event.algorithm_name == "z_score"
    assert event.algorithm_version == "1.0.0"
    assert event.schema_version == ANALYTICS_SCHEMA_VERSION
    assert event.tenant_id == ORG


def test_event_keeps_score_and_severity_separate() -> None:
    result = DetectorResult(
        "z_score",
        "1.0.0",
        triggered=True,
        observed_value=100.0,
        expected_value=60.0,
        deviation=8.0,
        anomaly_score=8.0,
        confidence=None,
    )
    event = build_detection_event(_context(), result)
    assert event.anomaly_score == 8.0
    assert event.confidence is None  # a z-score is not a calibrated probability


def test_event_category_is_derived_from_the_method() -> None:
    result = DetectorResult("change_point", "1.0.0", triggered=True, anomaly_score=6.0)
    event = build_detection_event(_context(), result)
    assert event.detection_category == DetectionCategory.CHANGE_POINT


def test_event_states_its_limitations() -> None:
    result = DetectorResult(
        "z_score",
        "1.0.0",
        triggered=True,
        anomaly_score=5.0,
        limitations=["Assumes an approximately symmetric distribution."],
    )
    event = build_detection_event(_context(quality_status=DataQualityStatus.DEGRADED), result)
    assert any("symmetric" in limitation for limitation in event.limitations)
    assert any("data quality" in limitation.lower() for limitation in event.limitations)


def test_evidence_is_bounded_to_the_contract_maximum() -> None:
    observations = [
        Observation(
            entity_id="LINE-01",
            entity_type="asset",
            metric="temperature",
            value=float(i),
            unit="degC",
            at=BASE + timedelta(seconds=i),
            data_family="timeseries",
            canonical_record_id=uuid.uuid4(),
            connection_id=None,
            source_system="x",
            quality_code=None,
            correlation_id="c",
        )
        for i in range(120)
    ]
    evidence = build_evidence(observations)
    assert len(evidence) == 50
    # The most recent observations are kept.
    assert evidence[-1].value == 119.0


def test_event_evidence_is_a_reference_not_a_payload() -> None:
    result = DetectorResult("z_score", "1.0.0", triggered=True, anomaly_score=4.0)
    evidence = [EvidenceReference(canonical_record_id=uuid.uuid4(), value=1.0)]
    event = build_detection_event(_context(evidence=evidence), result)
    assert event.evidence_references[0].canonical_record_id == evidence[0].canonical_record_id


# --- Grouping and suppression --------------------------------------------------


def test_group_key_is_stable() -> None:
    assert group_key_for("LINE-01", "temperature", "z_score") == "LINE-01|temperature|z_score"


def test_first_instance_emits() -> None:
    emit, _reason = should_emit(None, now=BASE, policy=GroupingPolicy())
    assert emit is True


def test_repeat_inside_cooldown_does_not_emit() -> None:
    state = GroupState(group_key="k", first_seen=BASE, last_seen=BASE, count=1)
    emit, reason = should_emit(
        state, now=BASE + timedelta(seconds=60), policy=GroupingPolicy(cooldown_seconds=900)
    )
    assert emit is False
    assert "cooldown" in reason


def test_repeat_after_cooldown_emits() -> None:
    state = GroupState(group_key="k", first_seen=BASE, last_seen=BASE, count=1)
    emit, _reason = should_emit(
        state, now=BASE + timedelta(seconds=1000), policy=GroupingPolicy(cooldown_seconds=900)
    )
    assert emit is True


def test_suppressed_group_does_not_emit() -> None:
    policy = GroupingPolicy(suppression_seconds=3600)
    state = GroupState(
        group_key="k",
        first_seen=BASE,
        last_seen=BASE,
        count=1,
        suppressed_until=suppression_until(BASE, policy),
    )
    emit, reason = should_emit(state, now=BASE + timedelta(seconds=1200), policy=policy)
    assert emit is False
    assert "suppressed" in reason
