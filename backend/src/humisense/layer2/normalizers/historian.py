"""Historian / industrial time-series normalizer.

Turns a captured historian sample into a typed canonical payload: an asset identifier, a
tag, a numeric value, a unit, and a quality code, with the timestamp the source reported.

What this normalizer deliberately does **not** do:

* It does not assume a unit. The unit travels with the value, and a conversion is recorded
  with its rule (see :mod:`humisense.layer2.units`).
* It does not interpolate, fill, or invent a missing value. A missing value is an error the
  record carries, not a zero.
* It does not interpret a quality code. It is preserved verbatim for a later layer.

The output is deterministic: the same input produces the same ``normalized_payload``.
"""

from __future__ import annotations

import math
from typing import Any, ClassVar

from humisense.layer2.errors import ErrorCode, Stage, StageError
from humisense.layer2.normalizers.base import (
    BaseNormalizer,
    NormalizationInput,
    NormalizedResult,
)
from humisense.layer2.units import (
    find_rule,
    normalize_unit_name,
    target_unit_for,
)

CANONICAL_SCHEMA = "2.0"

# Field aliases observed in real historian exports. The value and the timestamp are the
# only fields with strong meaning; everything else is carried through as an extension.
_ASSET_KEYS = ("machine_id", "asset_id", "asset", "equipment_id", "device_id", "line_id")
_TAG_KEYS = ("tag", "tag_name", "tagname", "measurement", "point", "signal")
_VALUE_KEYS = ("value", "val", "reading", "measurement_value", "sample")
_UNIT_KEYS = ("unit", "units", "uom", "engineering_unit")
_QUALITY_KEYS = ("quality", "quality_code", "status", "qc", "opc_quality")
_TIMESTAMP_KEYS = ("timestamp", "ts", "time", "sample_time", "event_time")


class HistorianNormalizer(BaseNormalizer):
    """Normalizes historian and machine-telemetry records."""

    name: ClassVar[str] = "historian"
    version: ClassVar[str] = "1.0.0"
    supported_record_types: ClassVar[tuple[str, ...]] = (
        "csv_row",
        "historian_sample",
        "historian_tag",
        "timeseries_sample",
        "telemetry_sample",
        "sensor_reading",
        "tag_sample",
    )
    input_schema_versions: ClassVar[tuple[str, ...]] = ("1.0",)
    output_schema_version: ClassVar[str] = CANONICAL_SCHEMA

    def normalize(self, data: NormalizationInput) -> NormalizedResult | list[StageError]:
        payload = data.payload
        errors: list[StageError] = []

        asset_id = _first_present(payload, _ASSET_KEYS)
        tag = _first_present(payload, _TAG_KEYS)
        raw_value = _first_present(payload, _VALUE_KEYS)
        raw_unit = _first_present(payload, _UNIT_KEYS)
        quality = _first_present(payload, _QUALITY_KEYS)

        if asset_id is None and tag is None:
            errors.append(
                StageError(
                    stage=Stage.NORMALIZATION,
                    code=ErrorCode.MISSING_REQUIRED_FIELD,
                    detail="A historian record needs an asset identifier or a tag",
                    location="payload",
                )
            )

        if raw_value is None:
            errors.append(
                StageError(
                    stage=Stage.NORMALIZATION,
                    code=ErrorCode.MISSING_VALUE,
                    detail="The record has no value; Layer 2 does not invent one",
                    field="value",
                )
            )
            return errors

        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            errors.append(
                StageError(
                    stage=Stage.NORMALIZATION,
                    code=ErrorCode.TYPE_MISMATCH,
                    detail="The value is not numeric",
                    field="value",
                )
            )
            return errors

        if not math.isfinite(value):
            errors.append(
                StageError(
                    stage=Stage.NORMALIZATION,
                    code=ErrorCode.NON_FINITE_VALUE,
                    detail="The value is not a finite number",
                    field="value",
                )
            )
            return errors

        unit_conversions: list[dict[str, Any]] = []
        normalized_value = value
        normalized_unit: str | None = None
        original_unit: str | None = None

        if raw_unit not in (None, ""):
            original_unit = str(raw_unit)
            unit_name = normalize_unit_name(original_unit)
            if unit_name is None:
                # The source gave a unit we could not place. Keep the raw unit and the raw
                # value; do not convert. The unit is preserved so nothing is lost.
                normalized_unit = original_unit
            else:
                target = target_unit_for(unit_name)
                if target is None:
                    normalized_unit = unit_name
                else:
                    rule = find_rule(unit_name, target)
                    if rule is None:
                        errors.append(
                            StageError(
                                stage=Stage.NORMALIZATION,
                                code=ErrorCode.UNIT_CONVERSION_FAILED,
                                detail=f"No conversion rule from {unit_name!r} to {target!r}",
                                field="unit",
                            )
                        )
                        return errors
                    converted = rule.apply(value)
                    unit_conversions.append(
                        {
                            "field": "value",
                            "original_value": value,
                            "original_unit": original_unit,
                            "normalized_value": converted,
                            "normalized_unit": target,
                            "rule": rule.rule_id,
                        }
                    )
                    normalized_value = converted
                    normalized_unit = target

        normalized_payload: dict[str, Any] = {
            "asset_id": asset_id,
            "tag": tag,
            "value": normalized_value,
            "unit": normalized_unit,
            "quality_code": quality,
        }

        # Source-specific fields that do not fit the typed shape are preserved as extensions
        # rather than dropped or flattened into the typed payload.
        extensions: dict[str, Any] = {}
        consumed = set(
            _ASSET_KEYS + _TAG_KEYS + _VALUE_KEYS + _UNIT_KEYS + _QUALITY_KEYS + _TIMESTAMP_KEYS
        )
        for key, item in payload.items():
            if key not in consumed:
                extensions[key] = item

        metadata = {
            "original_unit": original_unit,
            "unit_preserved": original_unit is not None,
        }

        return NormalizedResult(
            normalized_payload=normalized_payload,
            data_family="timeseries",
            entity_type="asset" if asset_id else "tag",
            event_type="sample",
            quality_code=str(quality) if quality is not None else None,
            unit_conversions=unit_conversions,
            extensions=extensions,
            metadata=metadata,
        )


def _first_present(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return None
