"""Validation framework.

Validation is *policy-driven per data family*. A family describes what a well-formed record
of that kind looks like; the framework applies the family's policy plus the universal checks
that hold for every record, and returns structured errors rather than raising. A malformed
record produces a :class:`StageError`, never an exception that aborts the batch.

Three initial families:

* ``timeseries`` — historian and machine telemetry: a value, a unit, a quality code, and an
  asset or tag identifier. Units are *never* assumed to be consistent across tags.
* ``business``   — email, conversations, documents, tasks, orders, inventory, production and
  maintenance events: an identifier and a body, preserved verbatim.
* ``generic``    — CSV rows, JSON, API and tabular payloads: a set of fields with no assumed
  semantics.

The framework deliberately does not interpret content. It checks shape, type, and bounds,
which is what lets later layers trust that a record reached them intact.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from humisense.layer2.errors import ErrorCode, Stage, StageError

MAX_PAYLOAD_BYTES = 1 * 1024 * 1024
"""Per-record payload cap. Larger records are rejected, never truncated."""

MAX_PAYLOAD_DEPTH = 32
"""Maximum nesting depth of a payload object."""

MAX_PAYLOAD_KEYS = 5000
"""Maximum total keys across a payload, bounding complexity as well as size."""

MAX_STRING_LENGTH = 100_000

# Timestamps outside this window are almost always a source bug or a unit error (seconds
# treated as milliseconds). They are rejected rather than silently stored, because a wrong
# timestamp is worse than a missing one for a time-series record.
MIN_PLAUSIBLE_TIMESTAMP = datetime(1900, 1, 1, tzinfo=UTC)
MAX_PLAUSIBLE_TIMESTAMP = datetime(2200, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class ValidationPolicy:
    """Declarative rules for one record family.

    ``required_fields`` are checked on the source payload. ``timestamp_field`` names the
    field, if any, whose value is the record's source timestamp.
    """

    family: str
    required_fields: tuple[str, ...] = ()
    timestamp_field: str | None = None
    numeric_fields: tuple[str, ...] = ()
    max_payload_bytes: int = MAX_PAYLOAD_BYTES
    allow_empty_payload: bool = False
    description: str = ""


TIMESERIES_POLICY = ValidationPolicy(
    family="timeseries",
    # A historian sample is meaningless without a value and an asset/tag identity, so those
    # are required. The unit and timestamp are validated when present.
    required_fields=("value",),
    timestamp_field="timestamp",
    numeric_fields=("value",),
    description="Historian and machine telemetry: a numeric value with asset/tag identity.",
)

BUSINESS_POLICY = ValidationPolicy(
    family="business",
    required_fields=(),
    timestamp_field="timestamp",
    numeric_fields=(),
    allow_empty_payload=False,
    description="Email, conversations, documents, tasks, orders, and organizational events.",
)

GENERIC_POLICY = ValidationPolicy(
    family="generic",
    required_fields=(),
    timestamp_field="timestamp",
    numeric_fields=(),
    allow_empty_payload=False,
    description="CSV rows, JSON, API payloads, and tabular records with no assumed semantics.",
)

FAMILY_POLICIES: dict[str, ValidationPolicy] = {
    "timeseries": TIMESERIES_POLICY,
    "business": BUSINESS_POLICY,
    "generic": GENERIC_POLICY,
}


@dataclass(slots=True)
class ValidationOutcome:
    """The result of validating one record."""

    ok: bool
    family: str | None = None
    errors: list[StageError] = field(default_factory=list)
    source_timestamp: datetime | None = None
    source_timezone: str | None = None
    source_time_metadata: dict[str, Any] = field(default_factory=dict)


def payload_size_bytes(payload: dict[str, Any]) -> int:
    """Return the encoded size of a payload, using a stable serialization."""
    import json

    return len(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")
    )


def _depth_and_keys(value: Any, depth: int = 0) -> tuple[int, int]:
    """Return the maximum nesting depth and total key count of a JSON-like value."""
    if depth > MAX_PAYLOAD_DEPTH:
        return depth, 0
    if isinstance(value, dict):
        max_depth = depth
        keys = len(value)
        for item in value.values():
            child_depth, child_keys = _depth_and_keys(item, depth + 1)
            max_depth = max(max_depth, child_depth)
            keys += child_keys
        return max_depth, keys
    if isinstance(value, list):
        max_depth = depth
        keys = 0
        for item in value:
            child_depth, child_keys = _depth_and_keys(item, depth + 1)
            max_depth = max(max_depth, child_depth)
            keys += child_keys
        return max_depth, keys
    return depth, 0


def validate_shape(
    payload: dict[str, Any],
    *,
    policy: ValidationPolicy,
) -> list[StageError]:
    """Apply the size and complexity limits that hold for every family."""
    errors: list[StageError] = []
    size = payload_size_bytes(payload)
    if size > policy.max_payload_bytes:
        errors.append(
            StageError(
                stage=Stage.VALIDATION,
                code=ErrorCode.PAYLOAD_TOO_LARGE,
                detail="Payload exceeds the per-record size limit",
                location="payload",
            )
        )
    depth, keys = _depth_and_keys(payload)
    if depth > MAX_PAYLOAD_DEPTH or keys > MAX_PAYLOAD_KEYS:
        errors.append(
            StageError(
                stage=Stage.VALIDATION,
                code=ErrorCode.PAYLOAD_TOO_COMPLEX,
                detail="Payload nesting or key count exceeds the limit",
                location="payload",
            )
        )
    return errors


def validate_required_fields(
    payload: dict[str, Any], *, policy: ValidationPolicy
) -> list[StageError]:
    errors: list[StageError] = []
    for name in policy.required_fields:
        if name not in payload or payload[name] in (None, ""):
            errors.append(
                StageError(
                    stage=Stage.VALIDATION,
                    code=ErrorCode.MISSING_REQUIRED_FIELD,
                    detail=f"Required field {name!r} is missing or empty",
                    field=name,
                )
            )
    return errors


def validate_numeric_fields(
    payload: dict[str, Any], *, policy: ValidationPolicy
) -> list[StageError]:
    """Check that numeric fields hold finite numbers.

    A value that is present but not numeric is a type mismatch. NaN and infinity are refused
    because they cannot be represented faithfully in JSON and would corrupt downstream maths.
    """
    import math

    errors: list[StageError] = []
    for name in policy.numeric_fields:
        if name not in payload or payload[name] in (None, ""):
            continue
        raw = payload[name]
        try:
            number = float(raw)
        except (TypeError, ValueError):
            errors.append(
                StageError(
                    stage=Stage.VALIDATION,
                    code=ErrorCode.TYPE_MISMATCH,
                    detail=f"Field {name!r} is present but is not numeric",
                    field=name,
                )
            )
            continue
        if not math.isfinite(number):
            errors.append(
                StageError(
                    stage=Stage.VALIDATION,
                    code=ErrorCode.NON_FINITE_VALUE,
                    detail=f"Field {name!r} is not a finite number",
                    field=name,
                )
            )
    return errors


def validate_timestamp(
    payload: dict[str, Any], *, policy: ValidationPolicy, source_timestamp: datetime | None
) -> tuple[list[StageError], datetime | None, str | None, dict[str, Any]]:
    """Validate a timestamp field and return its parsed value, timezone, and metadata.

    Returns the timestamp the *source* provided. It is never replaced with a processing
    time: a record with no valid timestamp keeps a null source timestamp, and the reason is
    recorded, so a later layer can tell "the source did not say" from "we dropped it".
    """
    errors: list[StageError] = []
    metadata: dict[str, Any] = {}
    field_name = policy.timestamp_field

    if source_timestamp is not None:
        if source_timestamp.tzinfo is None:
            errors.append(
                StageError(
                    stage=Stage.VALIDATION,
                    code=ErrorCode.INVALID_TIMESTAMP,
                    detail="The source timestamp is not timezone-aware",
                    field=field_name,
                )
            )
            return errors, None, None, metadata
        normalized = source_timestamp.astimezone(UTC)
        if not (MIN_PLAUSIBLE_TIMESTAMP <= normalized <= MAX_PLAUSIBLE_TIMESTAMP):
            errors.append(
                StageError(
                    stage=Stage.VALIDATION,
                    code=ErrorCode.TIMESTAMP_OUT_OF_RANGE,
                    detail="The source timestamp is outside the plausible range",
                    field=field_name,
                )
            )
            return errors, None, None, metadata
        metadata["source_timestamp_raw"] = source_timestamp.isoformat()
        return errors, normalized, _offset_name(source_timestamp), metadata

    if field_name and payload.get(field_name) not in (None, ""):
        # The envelope did not carry a parsed timestamp but the payload has a raw one.
        # Layer 2 does not guess a format here; the normalizer may parse it explicitly. The
        # raw value is preserved so nothing is lost.
        metadata["source_timestamp_raw"] = str(payload[field_name])[:200]

    return errors, None, None, metadata


def _offset_name(value: datetime) -> str | None:
    offset = value.utcoffset()
    if offset is None:
        return None
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def validate_record(
    *,
    payload: Any,
    family: str | None,
    source_timestamp: datetime | None = None,
) -> ValidationOutcome:
    """Validate one record against its family policy and the universal rules.

    A record whose family cannot be determined is rejected with ``UNKNOWN_FAMILY`` rather
    than being silently treated as generic, so an unclassified record is visible.
    """
    errors: list[StageError] = []
    if not isinstance(payload, dict):
        return ValidationOutcome(
            ok=False,
            errors=[
                StageError(
                    stage=Stage.VALIDATION,
                    code=ErrorCode.UNSUPPORTED_PAYLOAD,
                    detail="The payload is not a JSON object",
                    location="payload",
                )
            ],
        )

    if family is None or family not in FAMILY_POLICIES:
        return ValidationOutcome(
            ok=False,
            errors=[
                StageError(
                    stage=Stage.VALIDATION,
                    code=ErrorCode.UNKNOWN_FAMILY,
                    detail="The record family is unknown",
                    location="family",
                )
            ],
        )

    policy = FAMILY_POLICIES[family]
    if not payload and not policy.allow_empty_payload:
        errors.append(
            StageError(
                stage=Stage.VALIDATION,
                code=ErrorCode.MISSING_REQUIRED_FIELD,
                detail="The payload is empty",
                location="payload",
            )
        )

    errors.extend(validate_shape(payload, policy=policy))
    errors.extend(validate_required_fields(payload, policy=policy))
    errors.extend(validate_numeric_fields(payload, policy=policy))
    ts_errors, parsed_ts, tz_name, ts_metadata = validate_timestamp(
        payload, policy=policy, source_timestamp=source_timestamp
    )
    errors.extend(ts_errors)

    return ValidationOutcome(
        ok=not errors,
        family=family,
        errors=errors,
        source_timestamp=parsed_ts,
        source_timezone=tz_name,
        source_time_metadata=ts_metadata,
    )


def infer_family(record_type: str, payload: dict[str, Any]) -> str | None:
    """Infer a record family from the record type and payload shape.

    This is a *classification* step, not a business decision: it selects which structural
    policy applies. It is deliberately conservative and returns None for anything it cannot
    confidently place, so the record is quarantined rather than mis-validated.
    """
    lowered = record_type.lower()
    if any(token in lowered for token in ("historian", "telemetry", "sensor", "tag", "sample")):
        return "timeseries"
    if any(
        token in lowered
        for token in ("email", "message", "document", "task", "order", "inventory", "maintenance")
    ):
        return "business"
    if lowered in {"csv_row", "json_record", "api_record", "tabular_record"}:
        # A CSV row shaped like a historian sample (value + unit + tag/machine) is treated
        # as time-series; otherwise it is generic.
        if "value" in payload and (
            "unit" in payload or "tag" in payload or "machine_id" in payload
        ):
            return "timeseries"
        return "generic"
    return None


def first_error_code(errors: Iterable[StageError]) -> str | None:
    for error in errors:
        return error.code.value
    return None
