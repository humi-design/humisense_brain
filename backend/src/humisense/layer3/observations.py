"""Observation extraction: Layer 2 canonical records → typed analytical observations.

Layer 3 never re-reads a raw source payload. It reads canonical records and extracts a
small, typed *observation* for each numeric signal the record carries. Extraction is
deliberately conservative:

* **Nothing is invented.** A record with no numeric signal yields no observation, rather
  than a zero. A zero would be indistinguishable from a measured zero.
* **The unit travels with the value.** An observation carries the unit the canonical
  record reported, so a later comparison is unit-aware.
* **The family decides the shape.** A timeseries record yields a (entity, metric, value)
  observation. A business record yields a count observation and any tenant-bound numeric
  extension field. A generic record yields its numeric fields.

Entity and metric identity is stable: the same source tag always maps to the same
``entity_id``/``metric`` pair, so a baseline built yesterday is comparable to a value
observed today.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from humisense.db.models import CanonicalRecord

COUNT_METRIC = "records"
"""The built-in per-record count metric, so event-rate analytics works without configuration."""

GENERIC_FIELD_COUNT_METRIC = "field_count"
"""The number of fields in a generic record; the only metric a generic record always has."""


@dataclass(frozen=True, slots=True)
class Observation:
    """One typed, numeric observation extracted from a canonical record."""

    entity_id: str
    entity_type: str | None
    metric: str
    value: float
    unit: str | None
    at: datetime
    data_family: str
    canonical_record_id: uuid.UUID
    connection_id: uuid.UUID | None
    source_system: str
    quality_code: str | None
    correlation_id: str


def _numeric(value: Any) -> float | None:
    """Return ``value`` as a finite float, or ``None``.

    Bools are refused: ``True`` is not the measurement ``1.0``. A numeric string is
    accepted because JSON and CSV sources routinely carry numbers as text.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        candidate = float(value)
    elif isinstance(value, str):
        try:
            candidate = float(value.strip())
        except (TypeError, ValueError):
            return None
    else:
        return None
    return candidate if math.isfinite(candidate) else None


def _first_str(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _timeseries_entity(payload: dict[str, Any]) -> str | None:
    asset = _first_str(payload, ("asset_id", "machine_id", "equipment_id", "device_id", "line_id"))
    tag = _first_str(payload, ("tag", "tag_name", "measurement", "point", "signal"))
    return asset or tag


def _timeseries_metric(payload: dict[str, Any], entity: str) -> str:
    tag = _first_str(payload, ("tag", "tag_name", "measurement", "point", "signal"))
    # A record keyed only by asset with no tag is measured as a single "value" signal.
    return tag or "value"


def extract_observations(record: CanonicalRecord) -> list[Observation]:
    """Extract every numeric observation a canonical record carries.

    Returns an empty list when the record has no numeric signal. The record's effective
    event time (event timestamp, then source timestamp, never a processing time) is used as
    the observation time.
    """
    at = record.event_timestamp or record.source_timestamp
    if at is None:
        # Without an event time there is nothing to place on a time axis. Layer 3 does not
        # substitute ``received_timestamp``: that would silently rewrite the timeline.
        return []

    payload = record.normalized_payload or {}
    family = record.data_family
    source_system = record.record_type or ""

    if family == "timeseries":
        return _timeseries_observations(record, payload, at, source_system)
    if family == "business":
        return _business_observations(record, payload, at, source_system)
    return _generic_observations(record, payload, at, source_system)


def _timeseries_observations(
    record: CanonicalRecord, payload: dict[str, Any], at: datetime, source_system: str
) -> list[Observation]:
    entity = _timeseries_entity(payload)
    if entity is None:
        return []
    value = _numeric(payload.get("value"))
    if value is None:
        return []
    unit = payload.get("unit")
    return [
        Observation(
            entity_id=entity,
            entity_type=record.entity_type,
            metric=_timeseries_metric(payload, entity),
            value=value,
            unit=unit if isinstance(unit, str) and unit else None,
            at=at,
            data_family="timeseries",
            canonical_record_id=record.id,
            connection_id=record.connection_id,
            source_system=source_system,
            quality_code=record.quality_code,
            correlation_id=record.correlation_id,
        )
    ]


def _business_observations(
    record: CanonicalRecord, payload: dict[str, Any], at: datetime, source_system: str
) -> list[Observation]:
    """Business records yield a count, plus any numeric extension field.

    A business record has no fixed numeric schema (the normalizer preserves arbitrary
    extension fields), so the only always-present metric is the record count. Numeric
    extension fields are surfaced as metrics named ``ext.<field>`` so a tenant can build a
    baseline on a field its own source provides, without Layer 3 inventing a schema.
    """
    kind = payload.get("record_kind")
    entity = str(kind) if isinstance(kind, str) and kind else "business"
    observations = [
        Observation(
            entity_id=entity,
            entity_type=record.entity_type,
            metric=COUNT_METRIC,
            value=1.0,
            unit=None,
            at=at,
            data_family="business",
            canonical_record_id=record.id,
            connection_id=record.connection_id,
            source_system=source_system,
            quality_code=record.quality_code,
            correlation_id=record.correlation_id,
        )
    ]
    for key, raw in (record.extensions or {}).items():
        numeric = _numeric(raw)
        if numeric is not None:
            observations.append(
                Observation(
                    entity_id=entity,
                    entity_type=record.entity_type,
                    metric=f"ext.{key}",
                    value=numeric,
                    unit=None,
                    at=at,
                    data_family="business",
                    canonical_record_id=record.id,
                    connection_id=record.connection_id,
                    source_system=source_system,
                    quality_code=record.quality_code,
                    correlation_id=record.correlation_id,
                )
            )
    return observations


def _generic_observations(
    record: CanonicalRecord, payload: dict[str, Any], at: datetime, source_system: str
) -> list[Observation]:
    """Generic records expose their numeric scalar fields and their field count."""
    entity = _first_str(payload, ("entity_id", "id", "name")) or "generic"
    fields = payload.get("fields")
    observations: list[Observation] = []
    if isinstance(fields, dict):
        for key, raw in fields.items():
            numeric = _numeric(raw)
            if numeric is not None:
                observations.append(
                    Observation(
                        entity_id=entity,
                        entity_type=record.entity_type,
                        metric=str(key),
                        value=numeric,
                        unit=None,
                        at=at,
                        data_family="generic",
                        canonical_record_id=record.id,
                        connection_id=record.connection_id,
                        source_system=source_system,
                        quality_code=record.quality_code,
                        correlation_id=record.correlation_id,
                    )
                )
    count = _numeric(payload.get(GENERIC_FIELD_COUNT_METRIC))
    if count is not None:
        observations.append(
            Observation(
                entity_id=entity,
                entity_type=record.entity_type,
                metric=GENERIC_FIELD_COUNT_METRIC,
                value=count,
                unit=None,
                at=at,
                data_family="generic",
                canonical_record_id=record.id,
                connection_id=record.connection_id,
                source_system=source_system,
                quality_code=record.quality_code,
                correlation_id=record.correlation_id,
            )
        )
    return observations


def group_by_signal(observations: list[Observation]) -> dict[tuple[str, str], list[Observation]]:
    """Group observations by ``(entity_id, metric)``, each sorted by time.

    Sorting is stable and ascending by ``at``, so downstream windowing and baseline
    computation see a deterministic order even when the source delivered records out of
    order.
    """
    grouped: dict[tuple[str, str], list[Observation]] = {}
    for observation in observations:
        grouped.setdefault((observation.entity_id, observation.metric), []).append(observation)
    for series in grouped.values():
        series.sort(key=lambda o: o.at)
    return grouped


__all__ = [
    "COUNT_METRIC",
    "GENERIC_FIELD_COUNT_METRIC",
    "Observation",
    "extract_observations",
    "group_by_signal",
]
