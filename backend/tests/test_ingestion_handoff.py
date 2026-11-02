"""Layer 2 handoff tests.

Covers the contract in section 13: the record envelope, the acceptance acknowledgement,
retry behaviour, the idempotency key, error handling, correlation ids, and tenant
propagation.

The local test receiver is a real in-process implementation, not a mock, so the tests
exercise the actual serialization and acknowledgement path.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from humisense.connectors.types import SCHEMA_VERSION
from humisense.container import Container
from humisense.security.identity import Principal
from humisense.services.connections import ConnectionDraft
from humisense.services.ingestion import (
    IngestionBatch,
    IngestionRecord,
    LocalTestIngestionSink,
    build_ingestion_service,
)
from tests.conftest import synthetic_rows, write_csv

pytestmark = pytest.mark.asyncio


@pytest.fixture
def receiver() -> LocalTestIngestionSink:
    return LocalTestIngestionSink()


@pytest.fixture
async def connection(container: Container, owner: Principal, csv_root: Path) -> object:
    write_csv(csv_root / "handoff.csv", synthetic_rows(4))
    return await container.connections.create_connection(
        owner,
        ConnectionDraft(
            connector_type="csv",
            name="handoff",
            description=None,
            config={"path": "handoff.csv", "timestamp_column": "timestamp"},
            credentials={},
        ),
    )


async def test_envelope_carries_the_required_fields(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    """Section 6: every captured record carries the standard envelope."""
    from humisense.connectors.types import SourceRecordDraft
    from humisense.services.records import build_envelope

    envelope = build_envelope(
        draft=SourceRecordDraft(
            source_record_id="row-1",
            record_type="csv_row",
            payload={"machine_id": "LINE-01"},
            metadata={"row_number": 3},
        ),
        organization_id=owner.organization_id,
        connection_id=uuid.uuid4(),
        connector_type="csv",
        correlation_id="corr-1",
    )
    payload = envelope.model_dump()
    for field in (
        "connector_type",
        "connection_id",
        "organization_id",
        "source_record_id",
        "record_type",
        "captured_at",
        "payload",
        "metadata",
        "content_hash",
        "correlation_id",
    ):
        assert field in payload, f"envelope is missing {field}"
    assert envelope.schema_version == SCHEMA_VERSION
    assert payload["untrusted"] is True


async def test_envelope_does_not_normalize_source_semantics(
    container: Container, owner: Principal
) -> None:
    """Layer 1 must not convert units or infer timestamps."""
    from humisense.connectors.types import SourceRecordDraft
    from humisense.services.records import build_envelope

    envelope = build_envelope(
        draft=SourceRecordDraft(
            source_record_id="row-1",
            record_type="csv_row",
            payload={"value": "60.000", "unit": "degC"},
            source_timestamp=None,
        ),
        organization_id=owner.organization_id,
        connection_id=uuid.uuid4(),
        connector_type="csv",
        correlation_id="corr-2",
    )
    assert envelope.payload["value"] == "60.000"
    assert envelope.payload["unit"] == "degC"
    assert envelope.source_timestamp is None


async def test_capture_delivers_a_batch_and_records_acceptance(
    container: Container, owner: Principal, connection: object, csv_root: Path
) -> None:
    """End to end: capture, persist, deliver, and record a receipt."""
    from humisense.services.ingestion import LocalTestIngestionSink

    sink = LocalTestIngestionSink()
    container.ingestion._sink = sink

    job = await container.sync.create_job(
        organization_id=owner.organization_id,
        connection_id=connection.id,  # type: ignore[attr-defined]
        triggered_by=owner.user_id,
        correlation_id="handoff-correlation",
    )
    outcome = await container.sync.run_job(owner.organization_id, job.id)

    assert outcome.records_fetched == 4
    assert outcome.records_persisted == 4
    assert outcome.records_delivered == 4
    assert outcome.records_failed == 0
    assert len(sink.batches) == 1

    batch = sink.batches[0]
    assert batch.organization_id == str(owner.organization_id)
    assert len(batch.records) == 4
    for record in batch.records:
        assert record.organization_id == str(owner.organization_id)
        assert record.connection_id == str(connection.id)  # type: ignore[attr-defined]
        assert record.idempotency_key


async def test_receipts_are_persisted(
    container: Container, owner: Principal, connection: object, csv_root: Path
) -> None:
    from humisense.services.ingestion import LocalTestIngestionSink

    container.ingestion._sink = LocalTestIngestionSink()
    job = await container.sync.create_job(
        organization_id=owner.organization_id,
        connection_id=connection.id,  # type: ignore[attr-defined]
        triggered_by=owner.user_id,
        correlation_id="receipts",
    )
    await container.sync.run_job(owner.organization_id, job.id)

    from sqlalchemy import text

    async with container.database.tenant_session(owner.organization_id) as session:
        rows = (
            await session.execute(
                text("SELECT status, attempts, idempotency_key FROM ingestion_receipts")
            )
        ).all()

    assert len(rows) == 4
    for status, attempts, key in rows:
        assert status == "accepted"
        assert attempts >= 1
        assert key


async def test_idempotency_key_is_stable_for_identical_content(
    container: Container, owner: Principal, connection: object, csv_root: Path
) -> None:
    """Re-capturing the same rows must produce the same keys, so Layer 2 can deduplicate."""
    from humisense.services.ingestion import LocalTestIngestionSink

    sink = LocalTestIngestionSink()
    container.ingestion._sink = sink

    # Re-capture the same rows each time. The file is rewritten between runs so its
    # fingerprint changes and the connector restarts the scan from the top; otherwise the
    # second run would resume past the captured rows and deliver nothing, which tests the
    # checkpoint rather than the idempotency key. The row content is byte-identical, so
    # the derived identifiers must match.
    for correlation in ("first", "second"):
        write_csv(csv_root / "handoff.csv", synthetic_rows(4))
        job = await container.sync.create_job(
            organization_id=owner.organization_id,
            connection_id=connection.id,  # type: ignore[attr-defined]
            triggered_by=owner.user_id,
            correlation_id=correlation,
        )
        await container.sync.run_job(owner.organization_id, job.id)

    keys_by_run = [{record.idempotency_key for record in batch.records} for batch in sink.batches]
    assert len(keys_by_run) == 2
    assert keys_by_run[0] == keys_by_run[1]


class FlakySink(LocalTestIngestionSink):
    """A sink that fails a fixed number of times, to exercise the retry path."""

    name = "flaky_test_sink"

    def __init__(self, fail_first_n_calls: int = 0) -> None:
        super().__init__()
        self._failures_remaining = fail_first_n_calls
        self.call_count = 0

    async def deliver(self, batch: IngestionBatch) -> object:
        self.call_count += 1
        if self._failures_remaining > 0:
            self._failures_remaining -= 1
            from humisense.errors import ConnectorSourceError

            raise ConnectorSourceError("The test sink is temporarily unavailable.")
        return await super().deliver(batch)


async def test_delivery_retries_on_a_transient_failure(
    container: Container, owner: Principal, connection: object, csv_root: Path
) -> None:
    """A transient sink failure must be retried rather than dropping records."""
    sink = FlakySink(fail_first_n_calls=2)
    container.ingestion._sink = sink

    job = await container.sync.create_job(
        organization_id=owner.organization_id,
        connection_id=connection.id,  # type: ignore[attr-defined]
        triggered_by=owner.user_id,
        correlation_id="retry-delivery",
    )
    outcome = await container.sync.run_job(owner.organization_id, job.id)

    assert sink.call_count >= 3
    assert outcome.records_delivered == 4


class AlwaysFailingSink(LocalTestIngestionSink):
    """A sink that always fails, to prove capture is independent of acceptance."""

    name = "always_failing_test_sink"

    async def deliver(self, batch: IngestionBatch) -> object:
        from humisense.errors import ConnectorSourceError

        raise ConnectorSourceError("The test sink is permanently unavailable.")


async def test_records_survive_a_permanent_delivery_failure(
    container: Container, owner: Principal, connection: object, csv_root: Path
) -> None:
    """Capture and acceptance are separate: a Layer 2 outage must not discard data."""
    container.ingestion._sink = AlwaysFailingSink()

    job = await container.sync.create_job(
        organization_id=owner.organization_id,
        connection_id=connection.id,  # type: ignore[attr-defined]
        triggered_by=owner.user_id,
        correlation_id="sink-down",
    )
    outcome = await container.sync.run_job(owner.organization_id, job.id)

    # The records are still stored and previewable, and the failure is reported.
    assert (
        await container.records.count_records(
            owner.organization_id,
            connection.id,  # type: ignore[attr-defined]
        )
        == 4
    )
    assert outcome.records_delivered == 0
    assert outcome.records_failed == 4
    assert outcome.status.value in {"failed", "partial"}


async def test_disabled_sink_is_reported_truthfully(
    container: Container, test_settings: object
) -> None:
    """A disabled sink must be reported as disabled, not as a successful handoff."""
    from humisense.config import Settings

    settings = Settings(
        app_env="test",
        layer2_mode="disabled",
        secrets_master_key="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
    )
    service = build_ingestion_service(settings, egress_policy=None)
    assert service.sink_name == "disabled"
    del container, test_settings


async def test_local_test_receiver_is_identifiable() -> None:
    """Section 13: the development sink must be clearly identified as a test one."""
    assert LocalTestIngestionSink.name == "local_test_receiver"
    sink = LocalTestIngestionSink()
    assert "test" in type(sink).__name__.lower()
    assert "test" in (type(sink).__doc__ or "").lower()


async def test_batch_rejects_oversized_record_count() -> None:
    with pytest.raises(ValueError):
        IngestionBatch(
            batch_id=uuid.uuid4(),
            organization_id=str(uuid.uuid4()),
            correlation_id="corr",
            records=[
                IngestionRecord(
                    record_id=str(uuid.uuid4()),
                    organization_id=str(uuid.uuid4()),
                    connection_id=str(uuid.uuid4()),
                    connector_type="csv",
                    source_record_id=f"row-{index}",
                    record_type="csv_row",
                    schema_version=SCHEMA_VERSION,
                    payload={"value": "1"},
                    content_hash="a" * 64,
                    captured_at="2026-01-01T00:00:00+00:00",
                    idempotency_key=f"key-{index}",
                )
                for index in range(5000)
            ],
        )


async def test_batch_correlation_id_is_preserved(
    container: Container, owner: Principal, connection: object, csv_root: Path
) -> None:
    from humisense.services.ingestion import LocalTestIngestionSink

    sink = LocalTestIngestionSink()
    container.ingestion._sink = sink
    job = await container.sync.create_job(
        organization_id=owner.organization_id,
        connection_id=connection.id,  # type: ignore[attr-defined]
        triggered_by=owner.user_id,
        correlation_id="trace-this-correlation",
    )
    await container.sync.run_job(owner.organization_id, job.id)

    assert sink.batches[0].correlation_id == "trace-this-correlation"


async def test_http_sink_requires_an_egress_policy() -> None:
    """Section 4.4: an HTTP sink must not be constructible without SSRF protection."""
    from humisense.config import Settings
    from humisense.errors import ConfigurationError

    settings = Settings(
        app_env="test",
        layer2_mode="http",
        layer2_base_url="https://layer2.example.com",
        layer2_bearer_token="a-token-for-this-test",
        secrets_master_key="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
    )
    with pytest.raises(ConfigurationError):
        build_ingestion_service(settings, egress_policy=None)


async def test_http_sink_refuses_a_private_destination() -> None:
    """A Layer 2 endpoint on a private address must be refused by the egress policy."""
    from humisense.config import Settings
    from humisense.errors import EgressBlockedError
    from humisense.security.egress import EgressPolicy

    settings = Settings(
        app_env="test",
        layer2_mode="http",
        layer2_base_url="https://169.254.169.254/collect",
        layer2_bearer_token="a-token-for-this-test",
        secrets_master_key="MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=",
    )
    policy = EgressPolicy(settings)
    # The cloud metadata address is refused when the sink is constructed, so a
    # misconfiguration cannot silently drop the first batch.
    with pytest.raises(EgressBlockedError):
        build_ingestion_service(settings, egress_policy=policy)


async def test_layer1_does_no_reasoning(
    container: Container, owner: Principal, connection: object, csv_root: Path
) -> None:
    """Section 13: the handoff payload contains captured data only, no derived fields."""
    from humisense.services.ingestion import LocalTestIngestionSink

    sink = LocalTestIngestionSink()
    container.ingestion._sink = sink
    job = await container.sync.create_job(
        organization_id=owner.organization_id,
        connection_id=connection.id,  # type: ignore[attr-defined]
        triggered_by=owner.user_id,
        correlation_id="no-reasoning",
    )
    await container.sync.run_job(owner.organization_id, job.id)

    record = sink.batches[0].records[0]
    payload_keys = set(record.model_dump())
    for forbidden in ("anomaly_score", "classification", "summary", "prediction", "score"):
        assert forbidden not in payload_keys
    assert set(record.payload) <= {"timestamp", "machine_id", "tag", "value", "unit"}


async def test_tenant_identity_is_propagated_to_layer2(
    container: Container,
    owner: Principal,
    foreign_owner: Principal,
    connection: object,
    csv_root: Path,
) -> None:
    """Section 13: the tenant must be unambiguous in every delivered record."""
    from humisense.services.ingestion import LocalTestIngestionSink

    sink = LocalTestIngestionSink()
    container.ingestion._sink = sink
    job = await container.sync.create_job(
        organization_id=owner.organization_id,
        connection_id=connection.id,  # type: ignore[attr-defined]
        triggered_by=owner.user_id,
        correlation_id="tenant-propagation",
    )
    await container.sync.run_job(owner.organization_id, job.id)

    for record in sink.batches[0].records:
        assert record.organization_id == str(owner.organization_id)
        assert record.organization_id != str(foreign_owner.organization_id)
