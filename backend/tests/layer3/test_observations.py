"""Observation extraction: Layer 2 canonical records → typed observations.

These tests use real ``CanonicalRecord`` ORM rows (not mocks) so the extraction is exercised
against the same object the pipeline would hand it. The point of the tests is the refusal
behaviour: no event time, no numeric value, and a boolean are all cases where a careless
implementation would invent a number.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from humisense.db.models import CanonicalRecord, RecordState
from humisense.layer3.observations import (
    COUNT_METRIC,
    extract_observations,
    group_by_signal,
)

ORG = uuid.uuid4()
CONN = uuid.uuid4()
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _record(
    *,
    data_family: str,
    payload: dict,
    record_type: str = "historian_sample",
    entity_type: str | None = "asset",
    event_timestamp: datetime | None = BASE,
    source_timestamp: datetime | None = None,
    extensions: dict | None = None,
    quality_code: str | None = None,
) -> CanonicalRecord:
    return CanonicalRecord(
        id=uuid.uuid4(),
        organization_id=ORG,
        raw_record_id=uuid.uuid4(),
        connection_id=CONN,
        idempotency_key=f"{uuid.uuid4()}:r:hash",
        source_record_key="r",
        record_type=record_type,
        entity_type=entity_type,
        event_type="sample",
        data_family=data_family,
        source_timestamp=source_timestamp,
        event_timestamp=event_timestamp,
        observed_timestamp=BASE,
        received_timestamp=BASE,
        normalized_payload=payload,
        extensions=extensions or {},
        record_metadata={},
        unit_conversions=[],
        quality_code=quality_code,
        schema_version="2.0",
        source_schema_version="1.0",
        processing_version="layer2-0.1.0",
        processing_state=RecordState.NORMALIZED,
        correlation_id="corr",
    )


def test_timeseries_record_yields_one_typed_observation() -> None:
    record = _record(
        data_family="timeseries",
        payload={"asset_id": "LINE-01", "tag": "temperature", "value": 60.5, "unit": "degC"},
    )
    observations = extract_observations(record)
    assert len(observations) == 1
    observation = observations[0]
    assert observation.entity_id == "LINE-01"
    assert observation.metric == "temperature"
    assert observation.value == 60.5
    assert observation.unit == "degC"
    assert observation.at == BASE
    assert observation.canonical_record_id == record.id


def test_timeseries_record_without_event_time_yields_nothing() -> None:
    """Layer 3 must not substitute a processing time for a missing event time."""
    record = _record(
        data_family="timeseries",
        payload={"asset_id": "LINE-01", "tag": "temperature", "value": 60.5},
        event_timestamp=None,
        source_timestamp=None,
    )
    assert extract_observations(record) == []


def test_source_timestamp_is_used_when_event_timestamp_is_absent() -> None:
    record = _record(
        data_family="timeseries",
        payload={"asset_id": "LINE-01", "tag": "temperature", "value": 60.5},
        event_timestamp=None,
        source_timestamp=BASE - timedelta(minutes=5),
    )
    observations = extract_observations(record)
    assert len(observations) == 1
    assert observations[0].at == BASE - timedelta(minutes=5)


def test_non_numeric_value_yields_nothing() -> None:
    record = _record(
        data_family="timeseries",
        payload={"asset_id": "LINE-01", "tag": "temperature", "value": "not-a-number"},
    )
    assert extract_observations(record) == []


def test_boolean_value_is_not_treated_as_one() -> None:
    record = _record(
        data_family="timeseries",
        payload={"asset_id": "LINE-01", "tag": "temperature", "value": True},
    )
    assert extract_observations(record) == []


def test_numeric_string_is_accepted() -> None:
    record = _record(
        data_family="timeseries",
        payload={"asset_id": "LINE-01", "tag": "temperature", "value": "60.5"},
    )
    observations = extract_observations(record)
    assert observations and observations[0].value == 60.5


def test_business_record_yields_a_count_and_numeric_extensions() -> None:
    record = _record(
        data_family="business",
        record_type="purchase_order",
        entity_type="order",
        payload={"record_kind": "order", "identifier": "PO-1", "text": "hello", "party": "acme"},
        extensions={"quantity": 12, "amount": "1500.50", "note": "urgent"},
    )
    observations = extract_observations(record)
    metrics = {o.metric for o in observations}
    assert COUNT_METRIC in metrics
    assert "ext.quantity" in metrics
    assert "ext.amount" in metrics
    assert "ext.note" not in metrics  # non-numeric extension is not a metric
    by_metric = {o.metric: o.value for o in observations}
    assert by_metric[COUNT_METRIC] == 1.0
    assert by_metric["ext.quantity"] == 12.0
    assert by_metric["ext.amount"] == 1500.5


def test_generic_record_yields_numeric_fields_and_count() -> None:
    record = _record(
        data_family="generic",
        record_type="json_record",
        entity_type=None,
        payload={"fields": {"a": 1, "b": "2.5", "c": "x"}, "structured": {}, "field_count": 3},
    )
    observations = extract_observations(record)
    metrics = {o.metric for o in observations}
    assert {"a", "b", "field_count"} <= metrics
    assert "c" not in metrics


def test_group_by_signal_sorts_each_series_by_time() -> None:
    records = [
        _record(
            data_family="timeseries",
            payload={"asset_id": "LINE-01", "tag": "temperature", "value": 1.0},
            event_timestamp=BASE + timedelta(minutes=2),
        ),
        _record(
            data_family="timeseries",
            payload={"asset_id": "LINE-01", "tag": "temperature", "value": 0.0},
            event_timestamp=BASE,
        ),
    ]
    observations = [o for r in records for o in extract_observations(r)]
    grouped = group_by_signal(observations)
    series = grouped[("LINE-01", "temperature")]
    assert [o.value for o in series] == [0.0, 1.0]


def test_quality_code_is_preserved_on_the_observation() -> None:
    record = _record(
        data_family="timeseries",
        payload={"asset_id": "LINE-01", "tag": "temperature", "value": 60.5},
        quality_code="GOOD",
    )
    observations = extract_observations(record)
    assert observations[0].quality_code == "GOOD"
