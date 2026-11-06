"""Layer 4 integration tests: derivation and the engine against the real database.

These write real Layer 2 canonical records through the same ORM the ingestion pipeline uses,
run the engine, and assert the persisted work. They cover the promises that matter: a record
that is not configured as a work source derives nothing, a task record derives a work entity
with a source reference and a timeline event, re-running is an idempotent upsert, a Layer 3
detection is associated with the matching work by a verified machine reference, and the four
work times are preserved separately.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from humisense.container import Container
from humisense.db.models import (
    CanonicalRecord,
    Connection,
    ConnectionStatus,
    RawRecord,
    RecordState,
    WorkOrigin,
    WorkStatus,
    WorkType,
)

pytestmark = [pytest.mark.asyncio]

BASE = datetime(2026, 1, 1, tzinfo=UTC)


async def _seed_record(
    container: Container,
    organization_id: uuid.UUID,
    *,
    record_type: str = "task",
    source_record_key: str = "TASK-1",
    payload: dict[str, object] | None = None,
    at: datetime = BASE,
) -> uuid.UUID:
    """Write a raw record and its canonical record, in the order the pipeline uses."""
    connection_id = uuid.uuid4()
    raw_id = uuid.uuid4()
    canonical_id = uuid.uuid4()
    async with container.database.tenant_session(organization_id) as session:
        session.add(
            Connection(
                id=connection_id,
                organization_id=organization_id,
                connector_type="csv",
                name=f"seed-{connection_id.hex[:8]}",
                config={},
                status=ConnectionStatus.ACTIVE,
            )
        )
        await session.flush()
        session.add(
            RawRecord(
                id=raw_id,
                organization_id=organization_id,
                connection_id=connection_id,
                connector_type="csv",
                source_record_key=source_record_key,
                record_type=record_type,
                source_schema_version="1.0",
                idempotency_key=f"{raw_id}:r",
                source_timestamp=at,
                observed_timestamp=at,
                payload=payload or {},
                content_hash=f"hash-{raw_id}",
                correlation_id="seed",
            )
        )
        # Flush the raw record first: the canonical record's foreign key is set by id.
        await session.flush()
        session.add(
            CanonicalRecord(
                id=canonical_id,
                organization_id=organization_id,
                raw_record_id=raw_id,
                connection_id=connection_id,
                idempotency_key=f"{raw_id}:c",
                source_record_key=source_record_key,
                record_type=record_type,
                entity_type=record_type,
                event_type="recorded",
                data_family="business",
                source_timestamp=at,
                event_timestamp=at,
                observed_timestamp=at,
                received_timestamp=at,
                normalized_payload=payload or {},
                extensions={},
                record_metadata={},
                unit_conversions=[],
                quality_code="GOOD",
                schema_version="2.0",
                source_schema_version="1.0",
                processing_version="layer2-0.1.0",
                processing_state=RecordState.NORMALIZED,
                correlation_id="seed",
            )
        )
    return canonical_id


async def test_a_derivable_record_creates_a_work_entity(
    container: Container, organization: uuid.UUID
) -> None:
    canonical_id = await _seed_record(
        container,
        organization,
        payload={"title": "Replace conveyor bearing", "status": "open", "priority": "high"},
    )
    result = await container.layer4_engine.run(organization, correlation_id="t1")
    assert result.status == "succeeded"
    assert result.work_created == 1
    assert result.records_scanned == 1

    detail = await container.layer4_repository.list_work_entities(organization)
    rows, total = detail
    assert total == 1
    entity = rows[0]
    assert entity.title == "Replace conveyor bearing"
    assert entity.work_type is WorkType.TASK
    assert entity.origin is WorkOrigin.EXPLICIT
    assert entity.status is WorkStatus.OPEN
    assert entity.owner_id is None

    references = await container.layer4_repository.list_source_references(organization, entity.id)
    assert [ref.canonical_record_id for ref in references] == [canonical_id]

    timeline = await container.layer4_repository.list_timeline(organization, entity.id)
    assert any(event.event_type == "work.created" for event in timeline)


async def test_a_non_work_record_derives_nothing(
    container: Container, organization: uuid.UUID
) -> None:
    await _seed_record(
        container,
        organization,
        record_type="email",
        source_record_key="MSG-1",
        payload={"subject": "lunch?", "from": "alice"},
    )
    result = await container.layer4_engine.run(organization, correlation_id="t2")
    assert result.records_scanned == 1
    assert result.work_created == 0
    _, total = await container.layer4_repository.list_work_entities(organization)
    assert total == 0


async def test_rerun_is_an_idempotent_upsert(container: Container, organization: uuid.UUID) -> None:
    await _seed_record(container, organization, payload={"title": "Task", "status": "open"})
    first = await container.layer4_engine.run(organization, correlation_id="t3a")
    second = await container.layer4_engine.run(organization, correlation_id="t3b")
    assert first.work_created == 1
    assert second.work_created == 0
    assert second.work_updated == 1
    _, total = await container.layer4_repository.list_work_entities(organization)
    assert total == 1


async def test_detection_is_associated_with_matching_work(
    container: Container, organization: uuid.UUID
) -> None:
    """A Layer 3 detection on an entity is linked to work with the same machine reference."""
    await _seed_record(
        container,
        organization,
        record_type="maintenance_event",
        source_record_key="MX-1",
        payload={"title": "Inspect press", "machine_id": "PRESS-4B", "status": "open"},
    )
    async with container.database.tenant_session(organization) as session:
        await session.execute(
            text(
                "INSERT INTO detection_events (id, organization_id, detection_id, domain, "
                "source_system, entity_id, metric, detection_category, detection_type, "
                "severity, detected_at, algorithm_name, algorithm_version, anomaly_score, "
                "data_quality_status, evidence_references, explanation, limitations, "
                "schema_version, status, group_key, correlation_id, created_at, updated_at) "
                "VALUES (gen_random_uuid(), :org, :det, 'industrial', 's', 'PRESS-4B', "
                "'vibration', 'point_anomaly', 'z_score', 'high', now(), 'z_score', '1.0.0', "
                "7.0, 'good', '[]', 'spike', '[]', '3.0', 'open', 'k', 'c', now(), now())"
            ),
            {"org": organization, "det": uuid.uuid4()},
        )
    result = await container.layer4_engine.run(organization, correlation_id="t4")
    assert result.detections_associated == 1

    rows, _ = await container.layer4_repository.list_work_entities(organization)
    priorities = await container.layer4_repository.list_priorities(organization, rows[0].id)
    assert any(p.related_detection_ids for p in priorities)


async def test_work_is_not_derived_across_tenants(
    container: Container, organization: uuid.UUID, other_organization: uuid.UUID
) -> None:
    await _seed_record(container, organization, payload={"title": "Tenant A task"})
    await _seed_record(
        container,
        other_organization,
        source_record_key="TASK-2",
        payload={"title": "Tenant B task"},
    )
    await container.layer4_engine.run(organization, correlation_id="t5")
    _, total = await container.layer4_repository.list_work_entities(organization)
    assert total == 1
    rows, _ = await container.layer4_repository.list_work_entities(organization)
    assert rows[0].title == "Tenant A task"


async def test_outbox_event_is_written_for_creation(
    container: Container, organization: uuid.UUID
) -> None:
    await _seed_record(container, organization, payload={"title": "Task"})
    await container.layer4_engine.run(organization, correlation_id="t6")
    async with container.database.tenant_session(organization) as session:
        event_types = list(
            (
                await session.scalars(
                    text("SELECT event_type FROM work_outbox_events WHERE organization_id = :o"),
                    {"o": organization},
                )
            ).all()
        )
    assert "work.entity_created" in event_types


async def test_the_four_work_times_are_preserved_separately(
    container: Container, organization: uuid.UUID
) -> None:
    event_time = BASE - timedelta(days=2)
    await _seed_record(container, organization, payload={"title": "Task"}, at=event_time)
    await container.layer4_engine.run(organization, correlation_id="t7")
    rows, _ = await container.layer4_repository.list_work_entities(organization)
    timeline = await container.layer4_repository.list_timeline(organization, rows[0].id)
    created = next(event for event in timeline if event.event_type == "work.created")
    assert created.event_at == event_time
    assert created.source_timestamp == event_time
    assert created.ingested_at >= event_time
    assert created.processed_at >= created.ingested_at


async def test_a_derivation_failure_is_a_skip_and_the_run_is_partial(
    container: Container, organization: uuid.UUID
) -> None:
    """One un-derivable record must not fail the run.

    A derivable record with an empty payload raises during derivation. The engine must
    record it as a skip, continue, and label the run PARTIAL rather than SUCCEEDED.
    """
    await _seed_record(container, organization, source_record_key="GOOD-1", payload={"title": "ok"})
    await _seed_record(container, organization, source_record_key="BAD-1", payload={})
    result = await container.layer4_engine.run(organization, correlation_id="t8")
    assert result.status == "partial"
    assert len(result.skips) == 1
    assert result.skips[0].code == "derivation_failed"
    assert result.skips[0].source_record_key == "BAD-1"
    # The good record still derived work; the bad one did not stop it.
    assert result.work_created == 1
