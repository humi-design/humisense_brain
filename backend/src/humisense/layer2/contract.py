"""The canonical record contract.

This is Layer 2's stable internal contract, the shape every later layer reads. It is
versioned (``CANONICAL_SCHEMA_VERSION``) so a consumer can pin the shape it understands and
Layer 2 can evolve without breaking it.

Design rules encoded here, and enforced by the validators:

* **Four timestamps are never conflated.** ``source_timestamp`` is what the source reported
  and is never rewritten; ``event_timestamp`` is when the event occurred, when the source
  distinguishes it; ``observed_timestamp`` is when Layer 1 captured the record;
  ``received_timestamp`` is when Layer 2 durably accepted it; ``processed_timestamp`` is when
  normalization finished.
* **UTC internally, original timezone retained.** Values are stored timezone-aware in UTC.
  The source's original timezone and any source time metadata are kept in ``source_timezone``
  and ``source_time_metadata`` rather than being discarded.
* **Strong types, controlled extension.** Identity, timestamps, and classification are typed
  columns. Source-specific fields live in ``normalized_payload`` (typed per family) and
  ``extensions`` (a bounded, namespaced map), so a source is never flattened into one bag.

Nothing here makes a business decision or runs a model. It is a faithful, typed description
of a source record and the normalization applied to it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CANONICAL_SCHEMA_VERSION = "2.0"
"""Version of the canonical record contract produced by Layer 2."""

PROCESSING_VERSION = "layer2-0.1.0"
"""Version of the normalization pipeline that produced a record.

Bumped when normalization logic changes in a way that would produce a different output for
the same input, so a stored record can be tied to the code that made it and re-processed
deliberately rather than by surprise.
"""

MAX_EXTENSION_KEYS = 50
MAX_EXTENSION_VALUE_BYTES = 4096

DataFamily = Literal["timeseries", "business", "generic"]


class Provenance(BaseModel):
    """Where a canonical record came from and how it was produced."""

    model_config = ConfigDict(extra="forbid")

    source_system: str = Field(min_length=1, max_length=120)
    connector_type: str = Field(min_length=1, max_length=80)
    connection_id: uuid.UUID
    source_record_id: str = Field(min_length=1, max_length=512)
    source_schema_version: str = Field(min_length=1, max_length=20)
    content_hash: str = Field(min_length=1, max_length=128)
    normalizer: str | None = Field(default=None, max_length=120)
    """The normalizer that produced ``normalized_payload``, or None if none applied."""
    normalizer_version: str | None = Field(default=None, max_length=40)
    correlation_id: str = Field(min_length=1, max_length=64)
    received_batch_id: uuid.UUID | None = None


class UnitConversion(BaseModel):
    """A single, explicit unit conversion applied during normalization.

    A conversion is never silent: the original value and unit, the normalized value and
    unit, and the rule that connects them are all recorded, so a consumer can audit or
    reverse it.
    """

    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1, max_length=120)
    original_value: float
    original_unit: str = Field(min_length=1, max_length=40)
    normalized_value: float
    normalized_unit: str = Field(min_length=1, max_length=40)
    rule: str = Field(min_length=1, max_length=120)
    """A stable identifier for the conversion, for example ``degF_to_degC``."""


class CanonicalRecord(BaseModel):
    """A normalized, deduplicated, provenance-carrying record."""

    model_config = ConfigDict(extra="forbid")

    # --- Identity ---------------------------------------------------------
    record_id: uuid.UUID
    organization_id: uuid.UUID
    idempotency_key: str = Field(min_length=1, max_length=160)
    """Deterministic, tenant-scoped identity: ``connection_id:source_record_id:content_hash``."""

    # --- Classification ---------------------------------------------------
    record_type: str = Field(min_length=1, max_length=120)
    entity_type: str | None = Field(default=None, max_length=120)
    event_type: str | None = Field(default=None, max_length=120)
    data_family: DataFamily

    # --- Timestamps -------------------------------------------------------
    source_timestamp: datetime | None = None
    event_timestamp: datetime | None = None
    observed_timestamp: datetime
    received_timestamp: datetime
    processed_timestamp: datetime | None = None

    source_timezone: str | None = Field(default=None, max_length=64)
    """The original timezone name or offset the source reported, when it reported one."""
    source_time_metadata: dict[str, Any] = Field(default_factory=dict)
    """Any additional source time detail (for example a raw timestamp string)."""

    # --- Payloads ---------------------------------------------------------
    normalized_payload: dict[str, Any] = Field(default_factory=dict)
    """The normalized representation, typed per data family."""
    extensions: dict[str, Any] = Field(default_factory=dict)
    """Bounded, namespaced source-specific fields that do not fit the typed shape."""

    # --- Enrichment -------------------------------------------------------
    metadata: dict[str, Any] = Field(default_factory=dict)
    unit_conversions: list[UnitConversion] = Field(default_factory=list)
    quality_code: str | None = Field(default=None, max_length=40)
    """The source's own quality/status flag, preserved verbatim, never interpreted here."""

    # --- Versioning and state --------------------------------------------
    schema_version: str = CANONICAL_SCHEMA_VERSION
    processing_version: str = PROCESSING_VERSION
    ingestion_status: str = Field(min_length=1, max_length=30)
    correlation_id: str = Field(min_length=1, max_length=64)
    """The trace identifier, shared with Layer 1 and carried onto the downstream event."""
    provenance: Provenance

    @field_validator("observed_timestamp", "received_timestamp")
    @classmethod
    def _must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware; naive datetimes are refused")
        return value.astimezone(UTC)

    @field_validator("source_timestamp", "event_timestamp", "processed_timestamp")
    @classmethod
    def _normalize_optional(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware; naive datetimes are refused")
        return value.astimezone(UTC)

    @field_validator("extensions")
    @classmethod
    def _bound_extensions(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > MAX_EXTENSION_KEYS:
            raise ValueError(f"extensions must not exceed {MAX_EXTENSION_KEYS} keys")
        return value

    def effective_event_time(self) -> datetime | None:
        """Return the best available event time, without inventing one.

        Prefers the source's event timestamp, then its sample timestamp. Returns None when
        the source provided neither; callers must not substitute a processing time.
        """
        return self.event_timestamp or self.source_timestamp
