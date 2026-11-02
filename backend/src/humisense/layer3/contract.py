"""Layer 3's stable, versioned contracts.

These are the shapes every later layer reads. They are versioned
(``ANALYTICS_SCHEMA_VERSION``) so a consumer can pin what it understands and Layer 3 can
evolve without breaking it.

Design rules encoded here:

* **Provenance, not payloads.** A detection references the Layer 2 canonical records that
  support it (``evidence_references``); it never copies a raw source payload. A detection
  row is therefore small and cannot become a second copy of the source data.
* **Score, confidence, and severity are separate.** ``anomaly_score`` is how unusual a
  value is under a method; ``confidence`` is present only when a method produces a
  calibrated figure; ``severity`` is a business judgement from magnitude, duration,
  persistence, rarity, spread, and quality. A high score is never automatically CRITICAL.
* **Every result names its method and version.** ``algorithm_name``/``algorithm_version``,
  ``baseline_version``, and ``feature_versions`` make a result reproducible and auditable.
* **Limitations are first-class.** A result states what it does not claim, so a downstream
  layer cannot mistake a statistical relationship for a confirmed cause.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

ANALYTICS_SCHEMA_VERSION = "3.0"
"""Version of Layer 3's detection-event and analytical-result contracts."""


class DataQualityStatus(str, enum.Enum):
    """The suitability of a window for analysis."""

    GOOD = "good"
    ACCEPTABLE = "acceptable"
    DEGRADED = "degraded"
    UNUSABLE = "unusable"
    UNKNOWN = "unknown"


class ReadinessStatus(str, enum.Enum):
    """Whether a method may run over a window."""

    READY = "ready"
    READY_WITH_WARNINGS = "ready_with_warnings"
    INSUFFICIENT_DATA = "insufficient_data"
    INVALID_DATA = "invalid_data"
    STALE_DATA = "stale_data"


class Severity(str, enum.Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


SEVERITY_ORDER: tuple[Severity, ...] = (
    Severity.INFO,
    Severity.LOW,
    Severity.MEDIUM,
    Severity.HIGH,
    Severity.CRITICAL,
)


class Domain(str, enum.Enum):
    """The analytical domain a detection belongs to."""

    INDUSTRIAL = "industrial"
    BUSINESS = "business"
    DATA_QUALITY = "data_quality"
    GENERIC = "generic"


class DetectionCategory(str, enum.Enum):
    """The broad family of a detection."""

    POINT_ANOMALY = "point_anomaly"
    CONTEXTUAL_ANOMALY = "contextual_anomaly"
    COLLECTIVE_ANOMALY = "collective_anomaly"
    CHANGE_POINT = "change_point"
    TREND = "trend"
    THRESHOLD_BREACH = "threshold_breach"
    DATA_QUALITY = "data_quality"


class DetectionLifecycle(str, enum.Enum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    INVESTIGATING = "investigating"
    RESOLVED = "resolved"
    SUPPRESSED = "suppressed"
    RECOVERED = "recovered"


class BaselineType(str, enum.Enum):
    STATIC = "static"
    ROLLING = "rolling"
    HISTORICAL = "historical"
    TIME_OF_DAY = "time_of_day"
    DAY_OF_WEEK = "day_of_week"
    SEASONAL = "seasonal"
    MACHINE_STATE = "machine_state"
    ENTITY = "entity"
    BUSINESS = "business"


class TrendDirection(str, enum.Enum):
    INCREASING = "increasing"
    DECREASING = "decreasing"
    STABLE = "stable"
    FLUCTUATING = "fluctuating"


class AnomalyMethod(str, enum.Enum):
    """The detection methods Layer 3 supports.

    The string value is persisted in ``algorithm_name`` and is part of the contract: a
    consumer keys its interpretation of a detection on it.
    """

    Z_SCORE = "z_score"
    ROBUST_Z_SCORE = "robust_z_score"
    IQR = "iqr"
    EWMA = "ewma"
    CUSUM = "cusum"
    THRESHOLD = "threshold"
    BASELINE_DEVIATION = "baseline_deviation"
    TIME_OF_DAY_DEVIATION = "time_of_day_deviation"
    ISOLATION_FOREST = "isolation_forest"
    SEASONAL_RESIDUAL = "seasonal_residual"
    CHANGE_POINT = "change_point"
    MULTIVARIATE = "multivariate"


MAX_EVIDENCE_REFS = 50
"""A detection references at most this many supporting records."""


class EvidenceReference(BaseModel):
    """A pointer to the Layer 2 record that supports a detection.

    A reference, never a payload: enough for a later layer to retrieve the record and
    reconstruct the observation, without duplicating source data into Layer 3.
    """

    model_config = ConfigDict(extra="forbid")

    canonical_record_id: uuid.UUID | None = None
    source_record_key: str | None = Field(default=None, max_length=512)
    field: str | None = Field(default=None, max_length=120)
    value: float | None = None
    at: datetime | None = None


class QualityIssue(BaseModel):
    """One data-quality problem found in a window."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=80)
    """A stable machine-readable code, for example ``missing_value`` or ``out_of_order``."""
    severity: Severity
    field: str | None = Field(default=None, max_length=120)
    detail: str = Field(max_length=500)
    count: int = Field(default=1, ge=0)


class DataQualityResult(BaseModel):
    """The quality assessment of one entity/metric window."""

    model_config = ConfigDict(extra="forbid")

    organization_id: uuid.UUID
    entity_id: str = Field(min_length=1, max_length=200)
    metric: str = Field(min_length=1, max_length=120)
    status: DataQualityStatus
    issues: list[QualityIssue] = Field(default_factory=list)
    evaluated_count: int = Field(default=0, ge=0)
    missing_count: int = Field(default=0, ge=0)
    invalid_count: int = Field(default=0, ge=0)
    duplicate_count: int = Field(default=0, ge=0)
    window_start: datetime | None = None
    window_end: datetime | None = None
    assessed_at: datetime


class ReadinessResult(BaseModel):
    """Whether a method may run, and why."""

    model_config = ConfigDict(extra="forbid")

    organization_id: uuid.UUID
    entity_id: str = Field(min_length=1, max_length=200)
    metric: str = Field(min_length=1, max_length=120)
    method: str = Field(min_length=1, max_length=80)
    status: ReadinessStatus
    reasons: list[str] = Field(default_factory=list)
    sample_count: int = Field(default=0, ge=0)
    min_required: int = Field(default=0, ge=0)
    window_start: datetime | None = None
    window_end: datetime | None = None
    assessed_at: datetime


class FeatureValue(BaseModel):
    """One reproducible feature observation.

    Reproducibility comes from ``name`` + ``version`` + ``method`` + ``window_seconds`` +
    the input window. Two runs over the same window and version must produce the same value.
    """

    model_config = ConfigDict(extra="forbid")

    organization_id: uuid.UUID
    entity_id: str = Field(min_length=1, max_length=200)
    metric: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=20)
    value: float
    unit: str | None = Field(default=None, max_length=40)
    at: datetime
    method: str = Field(min_length=1, max_length=120)
    window_seconds: int = Field(ge=0)
    sample_count: int = Field(default=0, ge=0)
    quality_status: DataQualityStatus = DataQualityStatus.UNKNOWN
    sources: list[uuid.UUID] = Field(default_factory=list)
    """The canonical records the feature was computed from, for provenance."""


class BaselineVersion(BaseModel):
    """An immutable, versioned model of normal behaviour."""

    model_config = ConfigDict(extra="forbid")

    organization_id: uuid.UUID
    baseline_id: uuid.UUID
    version: int = Field(ge=1)
    entity_id: str = Field(min_length=1, max_length=200)
    metric: str = Field(min_length=1, max_length=120)
    baseline_type: BaselineType
    window_seconds: int = Field(ge=0)
    mean: float | None = None
    median: float | None = None
    stddev: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    p10: float | None = None
    p25: float | None = None
    p75: float | None = None
    p90: float | None = None
    unit: str | None = Field(default=None, max_length=40)
    sample_count: int = Field(default=0, ge=0)
    quality_status: DataQualityStatus = DataQualityStatus.UNKNOWN
    dataset_ref: str | None = Field(default=None, max_length=200)
    """A reference to the window the baseline was computed from, for traceability."""
    created_at: datetime
    created_by: uuid.UUID | None = None


class RuleCondition(BaseModel):
    """A bounded, validated rule condition.

    This is a small expression tree, not a script. There is no ``eval``, no ``exec``, and
    no SQL: a condition is a comparison or a boolean combination of comparisons over a
    named feature or the raw value.
    """

    model_config = ConfigDict(extra="forbid")

    operator: str = Field(min_length=1, max_length=20)
    """One of: ``gt``, ``gte``, ``lt``, ``lte``, ``eq``, ``ne``, ``abs_gt``, ``pct_gt``,
    ``and``, ``or``, ``not``."""
    threshold: float | None = None
    operand: str | None = Field(default=None, max_length=120)
    """The feature or value to compare, for example ``value`` or ``rolling_mean_30m``."""
    conditions: list[RuleCondition] = Field(default_factory=list)
    """Child conditions for ``and``/``or``/``not``. Bounded in depth and width."""

    @field_validator("operator")
    @classmethod
    def _known_operator(cls, value: str) -> str:
        allowed = {
            "gt",
            "gte",
            "lt",
            "lte",
            "eq",
            "ne",
            "abs_gt",
            "pct_gt",
            "and",
            "or",
            "not",
        }
        if value not in allowed:
            raise ValueError(f"unknown rule operator: {value!r}")
        return value


class RuleDefinition(BaseModel):
    """A tenant-configurable rule."""

    model_config = ConfigDict(extra="forbid")

    organization_id: uuid.UUID
    rule_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    domain: Domain = Domain.GENERIC
    scope: str = Field(default="entity", max_length=40)
    entity_id: str | None = Field(default=None, max_length=200)
    metric: str = Field(min_length=1, max_length=120)
    condition: RuleCondition
    window_seconds: int = Field(default=0, ge=0)
    severity: Severity = Severity.MEDIUM
    enabled: bool = True
    version: int = Field(default=1, ge=1)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class DetectionEvent(BaseModel):
    """The standardized, versioned detection output.

    Field names follow the Layer 3 specification exactly. A detection references its
    evidence rather than embedding it, and states its own limitations.
    """

    model_config = ConfigDict(extra="forbid")

    detection_id: uuid.UUID
    tenant_id: uuid.UUID
    domain: Domain
    source_system: str = Field(max_length=120)
    connection_id: uuid.UUID | None = None
    entity_id: str = Field(min_length=1, max_length=200)
    entity_type: str | None = Field(default=None, max_length=120)
    metric: str = Field(min_length=1, max_length=120)
    detection_category: DetectionCategory
    detection_type: str = Field(min_length=1, max_length=80)
    severity: Severity
    observed_value: float | None = None
    expected_value: float | None = None
    deviation: float | None = None
    unit: str | None = Field(default=None, max_length=40)
    observation_window_start: datetime | None = None
    observation_window_end: datetime | None = None
    detected_at: datetime
    algorithm_name: str = Field(min_length=1, max_length=80)
    algorithm_version: str = Field(min_length=1, max_length=20)
    model_id: uuid.UUID | None = None
    model_version: str | None = Field(default=None, max_length=40)
    baseline_id: uuid.UUID | None = None
    baseline_version: int | None = None
    feature_versions: dict[str, str] = Field(default_factory=dict)
    anomaly_score: float | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    """Present only when a method produces a calibrated figure. Not every score is one."""
    data_quality_status: DataQualityStatus = DataQualityStatus.UNKNOWN
    evidence_references: list[EvidenceReference] = Field(default_factory=list)
    explanation: str = Field(default="", max_length=2000)
    limitations: list[str] = Field(default_factory=list)
    correlation_id: str = Field(min_length=1, max_length=64)
    schema_version: str

    @field_validator("evidence_references")
    @classmethod
    def _bound_evidence(cls, value: list[EvidenceReference]) -> list[EvidenceReference]:
        if len(value) > MAX_EVIDENCE_REFS:
            raise ValueError(f"evidence_references must not exceed {MAX_EVIDENCE_REFS}")
        return value


class AnalyticsRun(BaseModel):
    """One execution of the analytics engine, recorded for observability and recovery."""

    model_config = ConfigDict(extra="forbid")

    run_id: uuid.UUID
    organization_id: uuid.UUID
    job_type: str = Field(min_length=1, max_length=80)
    entity_id: str | None = Field(default=None, max_length=200)
    metric: str | None = Field(default=None, max_length=120)
    status: str = Field(min_length=1, max_length=40)
    detections: int = Field(default=0, ge=0)
    quality_issues: int = Field(default=0, ge=0)
    skipped_reason: str | None = Field(default=None, max_length=200)
    error_code: str | None = Field(default=None, max_length=80)
    started_at: datetime
    finished_at: datetime | None = None
    counters: dict[str, Any] = Field(default_factory=dict)


__all__ = [
    "SEVERITY_ORDER",
    "AnalyticsRun",
    "AnomalyMethod",
    "BaselineType",
    "BaselineVersion",
    "DataQualityResult",
    "DataQualityStatus",
    "DetectionCategory",
    "DetectionEvent",
    "DetectionLifecycle",
    "Domain",
    "EvidenceReference",
    "FeatureValue",
    "QualityIssue",
    "ReadinessResult",
    "ReadinessStatus",
    "RuleCondition",
    "RuleDefinition",
    "Severity",
    "TrendDirection",
]
