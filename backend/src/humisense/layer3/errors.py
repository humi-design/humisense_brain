"""Layer 3 error codes and result types.

Like Layer 2, Layer 3 treats an analytical failure as a *value*, not an exception that
unwinds a run: one entity that cannot be analyzed must not stop the others. Every stage
produces a :class:`StageError` carrying a stable ``code``, and the engine decides whether
the entity is retried, skipped, or recorded as insufficient data.

The codes are grouped so an operator can see *which* stage produced a result without reading
free-text detail.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Stage(str, Enum):
    """The analytics stage that produced a result or an error."""

    SELECT = "select"
    QUALITY = "quality"
    READINESS = "readiness"
    FEATURES = "features"
    BASELINE = "baseline"
    RULES = "rules"
    DETECTION = "detection"
    TREND = "trend"
    CHANGE_POINT = "change_point"
    FORECAST = "forecast"
    CORRELATION = "correlation"
    SEVERITY = "severity"
    EVENT = "event"
    PERSISTENCE = "persistence"


class AnalyticsErrorCode(str, Enum):
    """Stable, machine-readable reasons an analysis did not produce a detection."""

    # --- Selection / preconditions ---------------------------------------
    NO_RECORDS = "no_records"
    INSUFFICIENT_HISTORY = "insufficient_history"
    STALE_DATA = "stale_data"
    INVALID_DATA = "invalid_data"
    UNKNOWN_METRIC = "unknown_metric"
    UNSUPPORTED_FAMILY = "unsupported_family"
    UNSUPPORTED_METHOD = "unsupported_method"

    # --- Data quality ----------------------------------------------------
    MISSING_VALUE = "missing_value"
    NON_FINITE_VALUE = "non_finite_value"
    DUPLICATE_OBSERVATION = "duplicate_observation"
    OUT_OF_ORDER = "out_of_order"
    IRREGULAR_SAMPLING = "irregular_sampling"
    IMPLAUSIBLE_VALUE = "implausible_value"
    UNIT_INCONSISTENT = "unit_inconsistent"
    SCHEMA_DRIFT = "schema_drift"
    FREQUENCY_CHANGE = "frequency_change"

    # --- Baseline --------------------------------------------------------
    BASELINE_MISSING = "baseline_missing"
    BASELINE_INSUFFICIENT = "baseline_insufficient"
    BASELINE_DRIFT_BLOCKED = "baseline_drift_blocked"

    # --- Rules -----------------------------------------------------------
    RULE_INVALID = "rule_invalid"
    RULE_NOT_APPLICABLE = "rule_not_applicable"

    # --- Computation -----------------------------------------------------
    COMPUTATION_FAILED = "computation_failed"
    NUMERICAL_ERROR = "numerical_error"
    PERSISTENCE_FAILED = "persistence_failed"
    RETRY_EXHAUSTED = "retry_exhausted"


RETRYABLE_CODES: frozenset[AnalyticsErrorCode] = frozenset(
    {
        AnalyticsErrorCode.PERSISTENCE_FAILED,
        AnalyticsErrorCode.COMPUTATION_FAILED,
        AnalyticsErrorCode.RETRY_EXHAUSTED,
    }
)
"""Errors worth retrying. Everything else is a property of the data, not a transient fault."""


@dataclass(frozen=True, slots=True)
class StageError:
    """A structured, bounded failure or skip reason from one analytics stage."""

    stage: Stage
    code: AnalyticsErrorCode
    detail: str
    field: str | None = None
    location: str | None = None
    """A non-sensitive locator: a field name, an entity id, or a window boundary."""

    def __post_init__(self) -> None:
        # Bound the detail so a crafted value cannot turn a result table into bulk storage.
        if len(self.detail) > 500:
            object.__setattr__(self, "detail", self.detail[:500])

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES

    def as_metadata(self) -> dict[str, str]:
        payload = {"stage": self.stage.value, "code": self.code.value, "detail": self.detail}
        if self.field:
            payload["field"] = self.field
        if self.location:
            payload["location"] = self.location
        return payload


__all__ = ["RETRYABLE_CODES", "AnalyticsErrorCode", "Stage", "StageError"]
