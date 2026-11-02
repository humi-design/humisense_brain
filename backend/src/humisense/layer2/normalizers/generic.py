"""Generic structured-data normalizer.

The fallback for CSV rows, JSON, API payloads, and tabular records that are not clearly
historian samples or business records. It does **not** force the source into a flat shape:
scalar fields are kept as fields, and nested structures are preserved as-is under a
structured section, so a nested source is not silently flattened and lost.

The output is deterministic and preserves every input field.
"""

from __future__ import annotations

from typing import Any, ClassVar

from humisense.layer2.errors import ErrorCode, Stage, StageError
from humisense.layer2.normalizers.base import (
    BaseNormalizer,
    NormalizationInput,
    NormalizedResult,
)

CANONICAL_SCHEMA = "2.0"

MAX_SCALAR_FIELDS = 2000


class GenericNormalizer(BaseNormalizer):
    """Normalizes generic structured records without imposing semantics."""

    name: ClassVar[str] = "generic"
    version: ClassVar[str] = "1.0.0"
    supported_record_types: ClassVar[tuple[str, ...]] = (
        "csv_row",
        "json_record",
        "api_record",
        "tabular_record",
        "generic_record",
    )
    input_schema_versions: ClassVar[tuple[str, ...]] = ("1.0",)
    output_schema_version: ClassVar[str] = CANONICAL_SCHEMA

    def normalize(self, data: NormalizationInput) -> NormalizedResult | list[StageError]:
        payload = data.payload
        if not payload:
            return [
                StageError(
                    stage=Stage.NORMALIZATION,
                    code=ErrorCode.MISSING_REQUIRED_FIELD,
                    detail="The record has no fields to normalize",
                    location="payload",
                )
            ]

        scalar_fields: dict[str, Any] = {}
        structured_fields: dict[str, Any] = {}

        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                # Preserved as a structure rather than flattened, so nesting survives.
                structured_fields[str(key)] = value
            else:
                scalar_fields[str(key)] = value

        if len(scalar_fields) > MAX_SCALAR_FIELDS:
            return [
                StageError(
                    stage=Stage.NORMALIZATION,
                    code=ErrorCode.PAYLOAD_TOO_COMPLEX,
                    detail="The record has too many scalar fields",
                    location="payload",
                )
            ]

        normalized_payload: dict[str, Any] = {
            "fields": scalar_fields,
            "structured": structured_fields,
            "field_count": len(scalar_fields) + len(structured_fields),
        }

        return NormalizedResult(
            normalized_payload=normalized_payload,
            data_family="generic",
            entity_type=None,
            event_type="record",
            metadata={"structured_field_count": len(structured_fields)},
        )
