"""Detection-event construction: from a detector result to a standardized event.

This module owns the rules that keep detections trustworthy and not overwhelming:

* **Provenance by reference.** An event cites the Layer 2 canonical records that support it
  (bounded to :data:`MAX_EVIDENCE_REFS`); it never embeds a payload.
* **Score, confidence, and severity stay separate.** The detector's score is carried as-is;
  severity is computed by :mod:`humisense.layer3.severity` from magnitude, persistence,
  duration, rarity, spread, and data quality.
* **Grouping and cooldown.** Repeated instances of the same condition on the same signal
  within a cooldown window are grouped under one ``group_key`` rather than flooding the
  stream. A suppression window can hold a group quiet without losing the record.
* **Data quality is attached, never ignored.** A detection on a degraded window is created,
  but it carries the quality status and is capped below CRITICAL.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from humisense.layer3.contract import (
    ANALYTICS_SCHEMA_VERSION,
    DataQualityStatus,
    DetectionCategory,
    DetectionEvent,
    Domain,
    EvidenceReference,
)
from humisense.layer3.detectors import DetectorResult
from humisense.layer3.observations import Observation
from humisense.layer3.severity import assess_severity, rarity_from_z

DEFAULT_COOLDOWN_SECONDS = 900
"""Repeated instances of one condition within this window are grouped."""

DEFAULT_SUPPRESSION_SECONDS = 3600
"""How long a suppressed group stays quiet by default."""

CATEGORY_BY_METHOD: dict[str, DetectionCategory] = {
    "z_score": DetectionCategory.POINT_ANOMALY,
    "robust_z_score": DetectionCategory.POINT_ANOMALY,
    "iqr": DetectionCategory.POINT_ANOMALY,
    "ewma": DetectionCategory.COLLECTIVE_ANOMALY,
    "cusum": DetectionCategory.CHANGE_POINT,
    "threshold": DetectionCategory.THRESHOLD_BREACH,
    "baseline_deviation": DetectionCategory.POINT_ANOMALY,
    "time_of_day_deviation": DetectionCategory.CONTEXTUAL_ANOMALY,
    "isolation_forest": DetectionCategory.POINT_ANOMALY,
    "multivariate": DetectionCategory.COLLECTIVE_ANOMALY,
    "change_point": DetectionCategory.CHANGE_POINT,
    "trend": DetectionCategory.TREND,
    "data_quality": DetectionCategory.DATA_QUALITY,
}


@dataclass(frozen=True, slots=True)
class GroupingPolicy:
    """How repeated detections are grouped and held quiet."""

    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS
    suppression_seconds: int = DEFAULT_SUPPRESSION_SECONDS


@dataclass(slots=True)
class DetectionContext:
    """Everything needed to build an event from a detector result."""

    organization_id: uuid.UUID
    domain: Domain
    entity_id: str
    metric: str
    observed_value: float | None
    unit: str | None
    detected_at: datetime
    observation_window_start: datetime | None
    observation_window_end: datetime | None
    correlation_id: str
    source_system: str = ""
    connection_id: uuid.UUID | None = None
    entity_type: str | None = None
    persistence: int = 1
    duration_seconds: float = 0.0
    quality_status: DataQualityStatus = DataQualityStatus.UNKNOWN
    baseline_id: uuid.UUID | None = None
    baseline_version: int | None = None
    feature_versions: dict[str, str] = field(default_factory=dict)
    model_id: uuid.UUID | None = None
    model_version: str | None = None
    evidence: list[EvidenceReference] = field(default_factory=list)


def group_key_for(entity_id: str, metric: str, algorithm_name: str) -> str:
    """A stable key identifying a condition, so repeats group together."""
    return f"{entity_id}|{metric}|{algorithm_name}"


def build_detection_event(
    context: DetectionContext,
    result: DetectorResult,
    *,
    domain: Domain | None = None,
) -> DetectionEvent:
    """Build a standardized detection event from a triggered detector result.

    Raises:
        ValueError: when the result did not trigger, or the context is missing a required
            field. Building an event from a non-triggered result would fabricate a detection.
    """
    if not result.triggered:
        raise ValueError("Cannot build a detection event from a non-triggered result.")

    quality_status = context.quality_status
    severity_assessment = assess_severity(
        magnitude=result.anomaly_score,
        persistence=context.persistence,
        duration_seconds=context.duration_seconds,
        rarity=rarity_from_z(result.anomaly_score),
        spread=result.deviation,
        data_quality=quality_status,
    )

    limitations = list(result.limitations)
    if context.quality_status is not None and getattr(
        context.quality_status, "value", None
    ) not in (
        "good",
        None,
    ):
        limitations.append(
            f"The supporting window's data quality is "
            f"{getattr(context.quality_status, 'value', context.quality_status)}."
        )

    return DetectionEvent(
        detection_id=uuid.uuid4(),
        tenant_id=context.organization_id,
        domain=domain or context.domain,
        source_system=context.source_system,
        connection_id=context.connection_id,
        entity_id=context.entity_id,
        entity_type=context.entity_type,
        metric=context.metric,
        detection_category=CATEGORY_BY_METHOD.get(
            result.algorithm_name, DetectionCategory.POINT_ANOMALY
        ),
        detection_type=result.algorithm_name,
        severity=severity_assessment.severity,
        observed_value=result.observed_value
        if result.observed_value is not None
        else context.observed_value,
        expected_value=result.expected_value,
        deviation=result.deviation,
        unit=context.unit,
        observation_window_start=context.observation_window_start,
        observation_window_end=context.observation_window_end,
        detected_at=context.detected_at,
        algorithm_name=result.algorithm_name,
        algorithm_version=result.algorithm_version,
        model_id=context.model_id,
        model_version=context.model_version,
        baseline_id=context.baseline_id,
        baseline_version=context.baseline_version,
        feature_versions=context.feature_versions,
        anomaly_score=result.anomaly_score,
        confidence=result.confidence,
        data_quality_status=quality_status,
        evidence_references=context.evidence,
        explanation=result.explanation,
        limitations=limitations,
        correlation_id=context.correlation_id,
        schema_version=ANALYTICS_SCHEMA_VERSION,
    )


def build_evidence(
    observations: list[Observation], *, field_name: str | None = None
) -> list[EvidenceReference]:
    """Build bounded evidence references from the observations that support a detection.

    The most recent observations are kept, so the evidence points at the data nearest the
    detection rather than an arbitrary prefix.
    """
    from humisense.layer3.contract import MAX_EVIDENCE_REFS

    recent = observations[-MAX_EVIDENCE_REFS:]
    return [
        EvidenceReference(
            canonical_record_id=o.canonical_record_id,
            field=field_name or o.metric,
            value=o.value,
            at=o.at,
        )
        for o in recent
    ]


@dataclass(slots=True)
class GroupState:
    """The grouping state carried between runs for one condition."""

    group_key: str
    first_seen: datetime
    last_seen: datetime
    count: int
    suppressed_until: datetime | None = None

    def is_suppressed(self, now: datetime) -> bool:
        return self.suppressed_until is not None and now < self.suppressed_until


def should_emit(
    state: GroupState | None, *, now: datetime, policy: GroupingPolicy
) -> tuple[bool, str]:
    """Decide whether a repeated condition should produce a new event now.

    Returns ``(emit, reason)``. A condition inside its cooldown, or inside its suppression
    window, does not emit a new event; the group's count is still updated by the caller.
    """
    if state is None:
        return True, "first instance of this condition"
    if state.is_suppressed(now):
        return False, "the condition's group is suppressed"
    if now - state.last_seen < timedelta(seconds=policy.cooldown_seconds):
        return False, "the condition is inside its cooldown window"
    return True, "the cooldown window has elapsed"


def suppression_until(now: datetime, policy: GroupingPolicy) -> datetime:
    return now + timedelta(seconds=policy.suppression_seconds)


__all__ = [
    "CATEGORY_BY_METHOD",
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_SUPPRESSION_SECONDS",
    "DetectionContext",
    "GroupState",
    "GroupingPolicy",
    "build_detection_event",
    "build_evidence",
    "group_key_for",
    "should_emit",
    "suppression_until",
]
