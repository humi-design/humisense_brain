"""Tests for the outbox publisher, quarantine replay, and the ingestion HTTP surface.

These exercise the real database and the assembled ASGI application. Nothing is mocked
except the outbound HTTP transport for the publisher, which stands in for a downstream
sink: the state transitions, retries, and idempotency are all real.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, text

from humisense.container import Container
from humisense.db.models import (
    CanonicalRecord,
    OutboxEvent,
    OutboxStatus,
    QuarantineStatus,
    RawRecord,
    RecordQuarantine,
    RecordState,
)
from humisense.errors import NotFoundError, ValidationFailure
from humisense.layer2.outbox import OutboxPublisher
from humisense.layer2.pipeline import IngestionPipeline
from humisense.layer2.replay import QuarantineService
from humisense.security.identity import Principal


def _record(connection_id: uuid.UUID, payload: dict[str, object]) -> object:
    from humisense.layer2.pipeline import IngestRecord

    return IngestRecord(
        connection_id=connection_id,
        connector_type="csv",
        source_record_id=str(uuid.uuid4()),
        record_type="csv_row",
        source_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        observed_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        payload=payload,
        metadata={},
        source_schema_version="1.0",
        content_hash=None,
        source_record_pk=None,
    )


async def _make_connection(container: Container, principal: Principal, csv_root: Path) -> uuid.UUID:
    async with container.database.tenant_session(principal.organization_id) as session:
        return (
            await session.execute(
                text(
                    "INSERT INTO connections (id, organization_id, connector_type, name, "
                    "config, status, enabled, capabilities, created_at, updated_at) VALUES "
                    "(gen_random_uuid(), :org, 'csv', 'c', '{}', 'active', true, '{}', "
                    "now(), now()) RETURNING id"
                ),
                {"org": principal.organization_id},
            )
        ).scalar_one()


@pytest.mark.asyncio
async def test_outbox_log_mode_marks_published_and_advances_state(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    """A delivered event becomes PUBLISHED and its canonical record advances too."""
    connection_id = await _make_connection(container, owner, csv_root)
    container.settings.layer2_publish_mode = "log"
    pipeline = IngestionPipeline(container.database, container.settings)
    await pipeline.ingest_batch(
        organization_id=owner.organization_id,
        batch_id="b1",
        correlation_id="c1",
        records=[_record(connection_id, {"value": 1.0, "unit": "kPa"})],
    )

    publisher = OutboxPublisher(container.database, container.settings)
    result = await publisher.publish_pending(organization_id=owner.organization_id)
    assert result.published == 1

    async with container.database.tenant_session(owner.organization_id) as session:
        event = (await session.scalars(select(OutboxEvent))).one()
        assert event.status is OutboxStatus.PUBLISHED
        assert event.published_at is not None
        canonical = (await session.scalars(select(CanonicalRecord))).one()
        assert canonical.processing_state is RecordState.PUBLISHED
        # The state change is recorded in the append-only ledger.
        ledger = (
            (
                await session.execute(
                    text(
                        "SELECT to_state FROM record_processing_events "
                        "WHERE canonical_record_id = :id ORDER BY created_at"
                    ),
                    {"id": canonical.id},
                )
            )
            .scalars()
            .all()
        )
        assert "stored" in ledger
        assert "published" in ledger


@pytest.mark.asyncio
async def test_outbox_disabled_mode_keeps_event_pending(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    """With publishing disabled nothing leaves, and the event is not lost."""
    connection_id = await _make_connection(container, owner, csv_root)
    container.settings.layer2_publish_mode = "disabled"
    pipeline = IngestionPipeline(container.database, container.settings)
    await pipeline.ingest_batch(
        organization_id=owner.organization_id,
        batch_id="b1",
        correlation_id="c1",
        records=[_record(connection_id, {"value": 1.0, "unit": "kPa"})],
    )
    publisher = OutboxPublisher(container.database, container.settings)
    result = await publisher.publish_pending(organization_id=owner.organization_id)
    assert result.published == 0
    async with container.database.tenant_session(owner.organization_id) as session:
        event = (await session.scalars(select(OutboxEvent))).one()
        assert event.status is OutboxStatus.PENDING
        assert event.attempt == 0


class _FailingTransport(httpx.AsyncBaseTransport):
    """An httpx transport whose sink always refuses, to exercise the retry path."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sink unavailable", request=request)


@pytest.mark.asyncio
async def test_outbox_retries_then_marks_failed(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    """A sink that never confirms leads to a FAILED event, never a silent drop."""
    connection_id = await _make_connection(container, owner, csv_root)
    container.settings.layer2_publish_mode = "http"
    container.settings.layer2_publish_base_url = "https://sink.invalid"
    container.settings.layer2_publish_bearer_token = SecretStr("token")
    pipeline = IngestionPipeline(container.database, container.settings)
    await pipeline.ingest_batch(
        organization_id=owner.organization_id,
        batch_id="b1",
        correlation_id="c1",
        records=[_record(connection_id, {"value": 1.0, "unit": "kPa"})],
    )
    publisher = OutboxPublisher(
        container.database, container.settings, transport=_FailingTransport()
    )
    for _ in range(8):
        # Clear the backoff gate by forcing created_at into the past.
        async with container.database.tenant_session(owner.organization_id) as session:
            await session.execute(
                text("UPDATE outbox_events SET created_at = created_at - interval '1 hour'")
            )
        await publisher.publish_pending(organization_id=owner.organization_id)

    async with container.database.tenant_session(owner.organization_id) as session:
        event = (await session.scalars(select(OutboxEvent))).one()
        assert event.status is OutboxStatus.FAILED
        assert event.attempt >= 8


@pytest.mark.asyncio
async def test_quarantine_replay_uses_preserved_payload(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    """Replay re-runs the pipeline over raw_records, not the original source."""
    connection_id = await _make_connection(container, owner, csv_root)
    pipeline = IngestionPipeline(container.database, container.settings)
    # A record with no value is quarantined by the normalizer.
    await pipeline.ingest_batch(
        organization_id=owner.organization_id,
        batch_id="b1",
        correlation_id="c1",
        records=[_record(connection_id, {"machine_id": "L1"})],
    )
    async with container.database.tenant_session(owner.organization_id) as session:
        quarantine = (await session.scalars(select(RecordQuarantine))).one()
        quarantine_id = quarantine.id
        raw_id = quarantine.raw_record_id
        assert raw_id is not None

    service = QuarantineService(container.database, container.settings)
    outcome = await service.replay(owner.organization_id, quarantine_id)
    assert outcome.replayed is True

    async with container.database.tenant_session(owner.organization_id) as session:
        quarantine = (await session.scalars(select(RecordQuarantine))).one()
        assert quarantine.status is QuarantineStatus.REPLAYED
        assert quarantine.replay_count == 1
        # The preserved payload is what was replayed.
        raw = (await session.scalars(select(RawRecord))).one()
        assert raw.id == raw_id


@pytest.mark.asyncio
async def test_reingesting_same_record_is_deduplicated(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    """The same source record delivered twice yields one canonical record."""
    connection_id = await _make_connection(container, owner, csv_root)
    pipeline = IngestionPipeline(container.database, container.settings)
    record = _record(connection_id, {"value": 5.0, "unit": "kPa"})
    first = await pipeline.ingest_batch(
        organization_id=owner.organization_id,
        batch_id="b1",
        correlation_id="c1",
        records=[record],
    )
    second = await pipeline.ingest_batch(
        organization_id=owner.organization_id,
        batch_id="b2",
        correlation_id="c2",
        records=[record],
    )
    assert first.accepted == 1
    assert second.duplicates == 1
    async with container.database.tenant_session(owner.organization_id) as session:
        assert len((await session.scalars(select(CanonicalRecord))).all()) == 1


@pytest.mark.asyncio
async def test_redelivering_the_same_batch_id_does_not_double_count(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    """A retransmit of the identical batch must not report a record as both accepted and
    duplicate.

    A producer that does not see the 202 (a dropped connection, a timeout) retransmits the
    same ``batch_id``. The stored counters already describe that batch, so the second
    response must reproduce them rather than seed them and add the per-record outcomes on
    top — which would report one record twice.
    """
    connection_id = await _make_connection(container, owner, csv_root)
    pipeline = IngestionPipeline(container.database, container.settings)
    record = _record(connection_id, {"value": 5.0, "unit": "kPa"})

    first = await pipeline.ingest_batch(
        organization_id=owner.organization_id,
        batch_id="same",
        correlation_id="c1",
        records=[record],
    )
    second = await pipeline.ingest_batch(
        organization_id=owner.organization_id,
        batch_id="same",
        correlation_id="c1",
        records=[record],
    )

    assert (first.accepted, first.duplicates) == (1, 0)
    assert (second.accepted, second.duplicates) == (0, 1)
    assert second.accepted + second.duplicates == len(second.outcomes)


@pytest.mark.asyncio
async def test_replay_of_a_still_invalid_record_does_not_duplicate(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    """Replaying a record that is still invalid is safe to repeat."""
    connection_id = await _make_connection(container, owner, csv_root)
    pipeline = IngestionPipeline(container.database, container.settings)
    await pipeline.ingest_batch(
        organization_id=owner.organization_id,
        batch_id="b1",
        correlation_id="c1",
        records=[_record(connection_id, {"machine_id": "L1"})],
    )
    async with container.database.tenant_session(owner.organization_id) as session:
        quarantine_id = (await session.scalars(select(RecordQuarantine))).one().id

    service = QuarantineService(container.database, container.settings)
    for _ in range(2):
        await service.replay(owner.organization_id, quarantine_id)

    async with container.database.tenant_session(owner.organization_id) as session:
        # No canonical record was created, and no second raw record was appended.
        assert len((await session.scalars(select(CanonicalRecord))).all()) == 0
        assert len((await session.scalars(select(RawRecord))).all()) == 1
        quarantine = (await session.scalars(select(RecordQuarantine))).one()
        assert quarantine.replay_count == 2


@pytest.mark.asyncio
async def test_outbox_sweep_publishes_every_tenant_without_crossing(
    container: Container,
    owner: Principal,
    foreign_owner: Principal,
    csv_root: Path,
) -> None:
    """The all-tenant sweep drains each tenant in its own scope."""
    container.settings.layer2_publish_mode = "log"
    pipeline = IngestionPipeline(container.database, container.settings)
    for principal in (owner, foreign_owner):
        connection_id = await _make_connection(container, principal, csv_root)
        await pipeline.ingest_batch(
            organization_id=principal.organization_id,
            batch_id=str(uuid.uuid4()),
            correlation_id="c1",
            records=[_record(connection_id, {"value": 1.0, "unit": "kPa"})],
        )

    publisher = OutboxPublisher(container.database, container.settings)
    result = await publisher.publish_all_tenants()
    assert result.published == 2

    for principal in (owner, foreign_owner):
        async with container.database.tenant_session(principal.organization_id) as session:
            events = (await session.scalars(select(OutboxEvent))).all()
            # Exactly the tenant's own event is visible.
            assert len(events) == 1
            assert events[0].organization_id == principal.organization_id
            assert events[0].status is OutboxStatus.PUBLISHED


@pytest.mark.asyncio
async def test_quarantine_listing_is_tenant_scoped(
    container: Container,
    owner: Principal,
    foreign_owner: Principal,
    csv_root: Path,
) -> None:
    """One tenant's quarantine listing never shows another tenant's rows."""
    for principal in (owner, foreign_owner):
        connection_id = await _make_connection(container, principal, csv_root)
        pipeline = IngestionPipeline(container.database, container.settings)
        await pipeline.ingest_batch(
            organization_id=principal.organization_id,
            batch_id=str(uuid.uuid4()),
            correlation_id="c1",
            records=[_record(connection_id, {"machine_id": "L1"})],
        )

    service = QuarantineService(container.database, container.settings)
    owner_items = await service.list_open(owner.organization_id)
    foreign_items = await service.list_open(foreign_owner.organization_id)
    assert len(owner_items) == 1
    assert len(foreign_items) == 1
    assert owner_items[0].quarantine_id != foreign_items[0].quarantine_id

    # Replaying another tenant's quarantine id is indistinguishable from a missing one:
    # it raises the same not-found error rather than leaking that the id exists elsewhere.
    with pytest.raises(NotFoundError):
        await service.replay(foreign_owner.organization_id, owner_items[0].quarantine_id)


@pytest.mark.asyncio
async def test_ingestion_route_requires_service_token(
    app_client: object, container: Container, owner: Principal, csv_root: Path
) -> None:
    """The machine endpoint refuses a request with no bearer token."""
    response = await app_client.post(  # type: ignore[attr-defined]
        "/api/v1/ingestion/batches",
        json={
            "envelope_version": "1.0",
            "organization_id": str(owner.organization_id),
            "batch_id": "b1",
            "records": [
                {
                    "connection_id": str(uuid.uuid4()),
                    "connector_type": "csv",
                    "source_record_id": "r1",
                    "record_type": "csv_row",
                    "observed_timestamp": "2026-01-01T00:00:00Z",
                    "payload": {"value": 1.0, "unit": "kPa"},
                    "metadata": {},
                    "source_schema_version": "1.0",
                }
            ],
        },
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_ingestion_route_accepts_and_reports_outcomes(
    app_client: object, container: Container, owner: Principal, csv_root: Path
) -> None:
    """A well-formed batch is accepted and the per-record outcome is returned."""
    connection_id = await _make_connection(container, owner, csv_root)
    container.settings.layer2_ingest_token = SecretStr("secret-token")
    response = await app_client.post(  # type: ignore[attr-defined]
        "/api/v1/ingestion/batches",
        headers={"Authorization": "Bearer secret-token"},
        json={
            "envelope_version": "1.0",
            "organization_id": str(owner.organization_id),
            "batch_id": "b1",
            "records": [
                {
                    "connection_id": str(connection_id),
                    "connector_type": "csv",
                    "source_record_id": "r1",
                    "record_type": "csv_row",
                    "source_timestamp": "2026-01-01T00:00:00Z",
                    "observed_timestamp": "2026-01-01T00:00:00Z",
                    "payload": {"value": 1.0, "unit": "kPa"},
                    "metadata": {},
                    "source_schema_version": "1.0",
                }
            ],
        },
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["accepted"] == 1
    assert body["outcomes"][0]["state"] in {"stored", "published"}


@pytest.mark.asyncio
async def test_disabled_receiver_has_no_machine_route(
    container: Container, owner: Principal
) -> None:
    """With the receiver disabled the batch route is absent, not merely refusing."""
    from httpx import ASGITransport, AsyncClient

    from humisense.app import create_app

    settings = container.settings
    settings.layer2_ingest_enabled = False
    app = create_app(settings, container=container)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        app.state.container = container
        response = await client.post(
            "/api/v1/ingestion/batches",
            headers={"Authorization": "Bearer anything"},
            json={
                "envelope_version": "1.0",
                "organization_id": str(owner.organization_id),
                "batch_id": "b1",
                "records": [],
            },
        )
        assert response.status_code == 404
        # The human-facing quarantine endpoints remain reachable (they need a session,
        # so an anonymous call is rejected rather than missing).
        quarantine = await client.get("/api/v1/ingestion/quarantine")
        assert quarantine.status_code == 401


@pytest.mark.asyncio
async def test_ingestion_route_rejects_foreign_connection(
    app_client: object,
    container: Container,
    owner: Principal,
    foreign_owner: Principal,
    csv_root: Path,
) -> None:
    """A batch that names one tenant but a connection owned by another is refused."""
    foreign_connection = await _make_connection(container, foreign_owner, csv_root)
    container.settings.layer2_ingest_token = SecretStr("secret-token")
    response = await app_client.post(  # type: ignore[attr-defined]
        "/api/v1/ingestion/batches",
        headers={"Authorization": "Bearer secret-token"},
        json={
            "envelope_version": "1.0",
            "organization_id": str(owner.organization_id),
            "batch_id": "b1",
            "records": [
                {
                    "connection_id": str(foreign_connection),
                    "connector_type": "csv",
                    "source_record_id": "r1",
                    "record_type": "csv_row",
                    "observed_timestamp": "2026-01-01T00:00:00Z",
                    "payload": {"value": 1.0, "unit": "kPa"},
                    "metadata": {},
                    "source_schema_version": "1.0",
                }
            ],
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_ingestion_receiver_refuses_an_oversized_batch(
    container: Container, owner: Principal
) -> None:
    """The byte bound is enforced, not merely configured.

    A record count limit alone would let a caller send the maximum number of maximum-size
    payloads. The receiver must refuse the batch on total size before anything is stored.
    """
    from humisense.layer2.receiver import IngestionReceiver, ReceivedBatch, ReceivedRecord

    settings = container.settings
    settings.layer2_max_batch_bytes = 4096
    receiver = IngestionReceiver(container.database, settings)

    def _record(index: int) -> ReceivedRecord:
        return ReceivedRecord(
            connection_id=uuid.uuid4(),
            connector_type="csv",
            source_record_id=f"r{index}",
            record_type="csv_row",
            source_timestamp=None,
            observed_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            payload={"blob": "x" * 2048},
            metadata={},
            source_schema_version="1.0",
            content_hash=None,
            source_record_pk=None,
        )

    batch = ReceivedBatch(
        organization_id=owner.organization_id,
        batch_id="oversized",
        correlation_id="oversized",
        records=[_record(0), _record(1), _record(2)],
    )
    with pytest.raises(ValidationFailure):
        await receiver.receive(batch)
