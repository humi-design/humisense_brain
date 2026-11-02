"""Layer 2 error codes.

Layer 2 never lets a single bad record abort a batch, so its failures are values, not
exceptions that unwind the run. Every stage produces a :class:`StageError` carrying a
stable ``code``; the pipeline decides whether the record is retried, quarantined, or
dropped, and records the decision.

The codes are grouped so an operator can see *which* stage rejected a record without
reading free-text detail. Detail text is bounded and never contains the full payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Stage(str, Enum):
    """The pipeline stage that produced an error."""

    RECEIVE = "receive"
    VALIDATION = "validation"
    NORMALIZATION = "normalization"
    DEDUPLICATION = "deduplication"
    STORAGE = "storage"
    PUBLISH = "publish"


class ErrorCode(str, Enum):
    """Stable, machine-readable reasons a record did not reach ``PUBLISHED``."""

    # --- Receive ----------------------------------------------------------
    UNSUPPORTED_SCHEMA_VERSION = "unsupported_schema_version"
    MALFORMED_ENVELOPE = "malformed_envelope"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    PAYLOAD_TOO_COMPLEX = "payload_too_complex"
    BATCH_TOO_LARGE = "batch_too_large"
    TENANT_MISMATCH = "tenant_mismatch"

    # --- Validation -------------------------------------------------------
    MISSING_REQUIRED_FIELD = "missing_required_field"
    TYPE_MISMATCH = "type_mismatch"
    UNKNOWN_RECORD_TYPE = "unknown_record_type"
    UNKNOWN_FAMILY = "unknown_family"
    INVALID_TIMESTAMP = "invalid_timestamp"
    TIMESTAMP_OUT_OF_RANGE = "timestamp_out_of_range"
    UNSUPPORTED_PAYLOAD = "unsupported_payload"
    CONNECTION_OWNERSHIP_MISMATCH = "connection_ownership_mismatch"
    SOURCE_IDENTITY_INVALID = "source_identity_invalid"

    # --- Normalization ----------------------------------------------------
    NO_NORMALIZER = "no_normalizer"
    NORMALIZER_FAILED = "normalizer_failed"
    INVALID_UNIT = "invalid_unit"
    UNIT_CONVERSION_FAILED = "unit_conversion_failed"
    MISSING_VALUE = "missing_value"
    NON_FINITE_VALUE = "non_finite_value"

    # --- Deduplication ----------------------------------------------------
    DUPLICATE_DELIVERY = "duplicate_delivery"
    CONFLICTING_SOURCE_VERSION = "conflicting_source_version"

    # --- Storage / publish ------------------------------------------------
    STORAGE_FAILED = "storage_failed"
    PUBLISH_FAILED = "publish_failed"
    RETRY_EXHAUSTED = "retry_exhausted"


RETRYABLE_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.STORAGE_FAILED,
        ErrorCode.PUBLISH_FAILED,
        ErrorCode.RETRY_EXHAUSTED,
    }
)
"""Errors worth retrying. Everything else is a permanent property of the record."""


@dataclass(frozen=True, slots=True)
class StageError:
    """A structured, bounded failure from one pipeline stage."""

    stage: Stage
    code: ErrorCode
    detail: str
    field: str | None = None
    location: str | None = None
    """A non-sensitive locator: a field name, a tag, or a row index."""

    def __post_init__(self) -> None:
        # Bound the detail so a crafted record cannot turn the quarantine table into bulk
        # storage. The payload itself is never placed in ``detail``.
        if len(self.detail) > 500:
            object.__setattr__(self, "detail", self.detail[:500])

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES

    def as_metadata(self) -> dict[str, str]:
        """Return a log-safe representation."""
        payload = {"stage": self.stage.value, "code": self.code.value, "detail": self.detail}
        if self.field:
            payload["field"] = self.field
        if self.location:
            payload["location"] = self.location
        return payload
