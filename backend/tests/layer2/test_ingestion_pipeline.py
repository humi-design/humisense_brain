"""Milestone 2: the ingestion receiver, source preservation, and validation.

The tests exercise the real pipeline against the real PostgreSQL database, so the unique
constraint, RLS, and immutability trigger are all in play rather than simulated.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from humisense.container import Container
from humisense.db.models import (
    CanonicalRecord,
    RawRecord,
    RecordQuarantine,
    RecordState,
)
from humisense.errors import AuthenticationError, ValidationFailure
from humisense.layer2.errors import ErrorCode
from humisense.layer2.pipeline import IngestionPipeline, IngestRecord
from humisense.layer2.receiver import (
    IngestionReceiver,
    ReceivedBatch,
    ReceivedRecord,
)
from humisense.layer2.validation import infer_family, validate_record
from humisense.security.crypto import content_hash
from humisense.security.identity import Principal
from humisense.services.connections import ConnectionDraft

pytestmark = pytest.mark.security


async def _make_connection(container: Container, owner: Principal, csv_root: Path) -> uuid.UUID:
    (csv_root / "l2.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    connection = await container.connections.create_connection(
        owner,
        ConnectionDraft(
            connector_type="csv",
            name="l2",
            description=None,
            config={"path": "l2.csv"},
            credentials={},
        ),
    )
    return connection.id


def _record(
    connection_id: uuid.UUID,
    *,
    source_record_id: str = "row-1",
    payload: dict | None = None,
    record_type: str = "csv_row",
) -> IngestRecord:
    payload = (
        payload if payload is not None else {"machine_id": "L1", "value": "10", "unit": "degC"}
    )
    return IngestRecord(
        connection_id=connection_id,
        connector_type="csv",
        source_record_id=source_record_id,
        record_type=record_type,
        source_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        observed_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        payload=payload,
        metadata={"row_number": 1},
        content_hash=content_hash(payload),
    )


async def test_pipeline_stores_canonical_and_outbox(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    connection_id = await _make_connection(container, owner, csv_root)
    pipeline = IngestionPipeline(container.database, container.settings)
    result = await pipeline.ingest_batch(
        organization_id=owner.organization_id,
        batch_id="b1",
        correlation_id="c1",
        records=[_record(connection_id)],
    )
    assert result.accepted == 1
    async with container.database.tenant_session(owner.organization_id) as session:
        canonical = (await session.scalars(select(CanonicalRecord))).all()
        raw = (await session.scalars(select(RawRecord))).all()
        outbox = await session.scalar(text("SELECT count(*) FROM outbox_events"))
    assert len(canonical) == 1
    assert len(raw) == 1
    assert canonical[0].processing_state == RecordState.STORED
    assert canonical[0].data_family == "timeseries"
    assert canonical[0].normalized_payload["value"] == 10.0
    # Outbox written in the same transaction as the canonical record.
    assert int(outbox) == 1


async def test_raw_payload_is_preserved_verbatim(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    connection_id = await _make_connection(container, owner, csv_root)
    pipeline = IngestionPipeline(container.database, container.settings)
    original = {"machine_id": "L1", "value": "212", "unit": "degF", "extra": {"nested": [1, 2]}}
    await pipeline.ingest_batch(
        organization_id=owner.organization_id,
        batch_id="b1",
        correlation_id="c1",
        records=[_record(connection_id, payload=original)],
    )
    async with container.database.tenant_session(owner.organization_id) as session:
        raw = (await session.scalars(select(RawRecord))).one()
    # The preserved payload is byte-for-byte the source, even though normalization converted
    # 212 degF to 100 degC in the canonical record.
    assert raw.payload == original


async def test_duplicate_delivery_is_detected(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    connection_id = await _make_connection(container, owner, csv_root)
    pipeline = IngestionPipeline(container.database, container.settings)
    record = _record(connection_id)
    first = await pipeline.ingest_batch(
        organization_id=owner.organization_id, batch_id="b1", correlation_id="c1", records=[record]
    )
    second = await pipeline.ingest_batch(
        organization_id=owner.organization_id, batch_id="b2", correlation_id="c2", records=[record]
    )
    assert first.accepted == 1
    assert second.duplicates == 1
    async with container.database.tenant_session(owner.organization_id) as session:
        raw_count = await session.scalar(select(func.count()).select_from(RawRecord))
        canonical_count = await session.scalar(select(func.count()).select_from(CanonicalRecord))
    assert int(raw_count) == 1
    assert int(canonical_count) == 1


async def test_concurrent_duplicate_submissions_create_one_row(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    """Two concurrent deliveries of the same record cannot both insert."""
    connection_id = await _make_connection(container, owner, csv_root)
    pipeline = IngestionPipeline(container.database, container.settings)

    async def submit(batch_id: str) -> None:
        await pipeline.ingest_batch(
            organization_id=owner.organization_id,
            batch_id=batch_id,
            correlation_id=batch_id,
            records=[_record(connection_id)],
        )

    await asyncio.gather(submit("b1"), submit("b2"), submit("b3"))
    async with container.database.tenant_session(owner.organization_id) as session:
        raw_count = await session.scalar(select(func.count()).select_from(RawRecord))
    assert int(raw_count) == 1


async def test_malformed_record_does_not_abort_batch(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    connection_id = await _make_connection(container, owner, csv_root)
    pipeline = IngestionPipeline(container.database, container.settings)
    result = await pipeline.ingest_batch(
        organization_id=owner.organization_id,
        batch_id="b1",
        correlation_id="c1",
        records=[
            _record(
                connection_id,
                source_record_id="good",
                payload={"machine_id": "L1", "value": "1", "unit": "degC"},
            ),
            _record(
                connection_id, source_record_id="bad", payload={"machine_id": "L1"}
            ),  # no value
            _record(
                connection_id,
                source_record_id="good2",
                payload={"machine_id": "L2", "value": "2", "unit": "degC"},
            ),
        ],
    )
    assert result.accepted == 2
    assert result.quarantined == 1
    async with container.database.tenant_session(owner.organization_id) as session:
        quarantine = (await session.scalars(select(RecordQuarantine))).all()
    assert len(quarantine) == 1
    assert quarantine[0].error_code == ErrorCode.MISSING_VALUE.value


async def test_quarantine_holds_only_a_bounded_preview(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    connection_id = await _make_connection(container, owner, csv_root)
    pipeline = IngestionPipeline(container.database, container.settings)
    big = {"machine_id": "L1", "blob": "x" * 20000}  # no value → quarantined
    await pipeline.ingest_batch(
        organization_id=owner.organization_id,
        batch_id="b1",
        correlation_id="c1",
        records=[_record(connection_id, payload=big)],
    )
    async with container.database.tenant_session(owner.organization_id) as session:
        quarantine = (await session.scalars(select(RecordQuarantine))).one()
    preview = quarantine.payload_preview
    assert preview.get("preview_truncated") is True
    assert len(str(preview["preview"])) <= 2048


async def test_receiver_authenticates(container: Container) -> None:
    receiver = IngestionReceiver(container.database, container.settings)
    # No token configured in test settings → refuse.
    with pytest.raises(AuthenticationError):
        receiver.authenticate("anything")
    container.settings.layer2_ingest_token = None  # type: ignore[assignment]
    with pytest.raises(AuthenticationError):
        receiver.authenticate(None)


async def test_receiver_rejects_foreign_connection(
    container: Container, owner: Principal, foreign_owner: Principal, csv_root: Path
) -> None:
    """A batch claiming a tenant that does not own the connection is refused."""
    foreign_connection = await _make_connection(container, foreign_owner, csv_root)
    receiver = IngestionReceiver(container.database, container.settings)
    batch = ReceivedBatch(
        organization_id=owner.organization_id,  # wrong tenant for this connection
        batch_id="b1",
        correlation_id="c1",
        records=[
            ReceivedRecord(
                connection_id=foreign_connection,
                connector_type="csv",
                source_record_id="r1",
                record_type="csv_row",
                source_timestamp=None,
                observed_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
                payload={"value": "1"},
                metadata={},
                source_schema_version="1.0",
                content_hash=None,
                source_record_pk=None,
            )
        ],
    )
    with pytest.raises(ValidationFailure):
        await receiver.receive(batch)


# --- Validation unit tests --------------------------------------------------


def test_family_inference_is_conservative() -> None:
    assert infer_family("csv_row", {"value": "1", "unit": "degC"}) == "timeseries"
    assert infer_family("csv_row", {"a": 1, "b": 2}) == "generic"
    assert infer_family("historian_sample", {}) == "timeseries"
    assert infer_family("email_message", {}) == "business"
    assert infer_family("something_unknown", {}) is None


def test_validation_rejects_unknown_family() -> None:
    outcome = validate_record(payload={"a": 1}, family=None)
    assert not outcome.ok
    assert outcome.errors[0].code == ErrorCode.UNKNOWN_FAMILY


def test_validation_rejects_non_finite_value() -> None:
    outcome = validate_record(payload={"value": "nan"}, family="timeseries")
    assert not outcome.ok
    assert any(e.code == ErrorCode.NON_FINITE_VALUE for e in outcome.errors)


def test_validation_rejects_oversized_payload() -> None:
    outcome = validate_record(payload={"blob": "x" * (2 * 1024 * 1024)}, family="generic")
    assert not outcome.ok
    assert any(e.code == ErrorCode.PAYLOAD_TOO_LARGE for e in outcome.errors)


def test_validation_rejects_deeply_nested_payload() -> None:
    nested: dict = {}
    cursor = nested
    for _ in range(50):
        cursor["child"] = {}
        cursor = cursor["child"]
    outcome = validate_record(payload=nested, family="generic")
    assert not outcome.ok
    assert any(e.code == ErrorCode.PAYLOAD_TOO_COMPLEX for e in outcome.errors)
