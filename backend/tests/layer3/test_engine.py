"""Analytics engine: end-to-end behaviour against the real database.

These tests write real canonical records through the same ORM the pipeline uses, run the
engine, and assert the persisted detections. They cover the promises that matter: a clean
window produces no detection, a clear anomaly produces one with provenance, a stale window
is skipped rather than analyzed, repeated conditions are grouped, and a run is idempotent.
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
)
from humisense.layer3.engine import AnalyticsEngine
from humisense.layer3.repository import AnalyticsRepository

pytestmark = [pytest.mark.asyncio]

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _canonical_record(
    organization_id: uuid.UUID,
    connection_id: uuid.UUID,
    *,
    raw_record_id: uuid.UUID,
    asset: str,
    tag: str,
    value: float,
    at: datetime,
    unit: str = "degC",
) -> CanonicalRecord:
    return CanonicalRecord(
        id=uuid.uuid4(),
        organization_id=organization_id,
        raw_record_id=raw_record_id,
        connection_id=connection_id,
        idempotency_key=f"{uuid.uuid4()}:r:hash",
        source_record_key=f"{asset}-{tag}-{at.isoformat()}",
        record_type="historian_sample",
        entity_type="asset",
        event_type="sample",
        data_family="timeseries",
        source_timestamp=at,
        event_timestamp=at,
        observed_timestamp=at,
        received_timestamp=at,
        normalized_payload={"asset_id": asset, "tag": tag, "value": value, "unit": unit},
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


async def _seed_series(
    container: Container,
    organization_id: uuid.UUID,
    *,
    values: list[float],
    step_seconds: int = 60,
    asset: str = "LINE-01",
    tag: str = "temperature",
    base: datetime = BASE,
) -> uuid.UUID:
    """Seed one connection with a raw record and canonical record per value.

    The canonical record carries a foreign key to its raw record, so the raw record is
    written first, exactly as the Layer 2 pipeline does.
    """
    connection_id = uuid.uuid4()
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
        raws: list[tuple[uuid.UUID, str, float, datetime, dict[str, object]]] = []
        for index, value in enumerate(values):
            at = base + timedelta(seconds=index * step_seconds)
            raw_id = uuid.uuid4()
            source_key = f"{asset}-{tag}-{at.isoformat()}"
            payload = {"asset_id": asset, "tag": tag, "value": value, "unit": "degC"}
            raws.append((raw_id, source_key, value, at, payload))
            session.add(
                RawRecord(
                    id=raw_id,
                    organization_id=organization_id,
                    connection_id=connection_id,
                    connector_type="csv",
                    source_record_key=source_key,
                    record_type="historian_sample",
                    source_schema_version="1.0",
                    idempotency_key=f"{raw_id}:r",
                    source_timestamp=at,
                    observed_timestamp=at,
                    payload=payload,
                    content_hash=f"hash-{raw_id}",
                    correlation_id="seed",
                )
            )
        # Flush the raw records first: the canonical record's foreign key is set by id, so
        # the unit of work cannot infer the ordering on its own.
        await session.flush()
        for raw_id, _source_key, value, at, _payload in raws:
            session.add(
                _canonical_record(
                    organization_id,
                    connection_id,
                    raw_record_id=raw_id,
                    asset=asset,
                    tag=tag,
                    value=value,
                    at=at,
                )
            )
    return connection_id


def _engine(container: Container) -> AnalyticsEngine:
    return AnalyticsEngine(
        AnalyticsRepository(container.database), container.settings, metrics=container.metrics
    )


async def test_clean_window_produces_no_detection(
    container: Container, organization: uuid.UUID
) -> None:
    await _seed_series(container, organization, values=[60.0] * 30)
    result = await _engine(container).run_full(
        organization,
        correlation_id="clean",
        since=BASE,
        until=BASE + timedelta(minutes=31),
    )
    assert result.detections == 0
    async with container.database.tenant_session(organization) as session:
        count = await session.scalar(text("SELECT count(*) FROM detection_events"))
    assert count == 0


async def test_a_clear_anomaly_produces_a_detection_with_provenance(
    container: Container, organization: uuid.UUID
) -> None:
    # A stable series with one extreme spike at the end.
    values = [60.0, 60.1, 59.9, 60.0, 60.2, 59.8, 60.0, 60.1, 59.9, 60.0] * 3 + [140.0]
    await _seed_series(container, organization, values=values)
    result = await _engine(container).run_full(
        organization,
        correlation_id="spike",
        since=BASE,
        until=BASE + timedelta(minutes=len(values) + 1),
    )
    assert result.detections >= 1
    async with container.database.tenant_session(organization) as session:
        row = (
            await session.execute(
                text(
                    "SELECT algorithm_name, algorithm_version, severity, schema_version, "
                    "evidence_references, entity_id, metric FROM detection_events LIMIT 1"
                )
            )
        ).one()
    assert row.entity_id == "LINE-01"
    assert row.metric == "temperature"
    assert row.algorithm_version  # a version is always present
    assert row.schema_version == "3.0"
    assert len(row.evidence_references) > 0  # provenance, not a payload


async def test_a_stale_window_is_skipped_not_analyzed(
    container: Container, organization: uuid.UUID
) -> None:
    # Seed data ending at BASE+30m, then run over a window whose end is hours later. The
    # window is judged against its own end, so the newest observation is stale and no
    # detector may run.
    await _seed_series(container, organization, values=[60.0] * 30)
    late_end = BASE + timedelta(hours=6)
    result = await _engine(container).run_full(
        organization,
        correlation_id="stale",
        since=BASE,
        until=late_end,
    )
    assert result.detections == 0
    async with container.database.tenant_session(organization) as session:
        stale = await session.scalar(
            text("SELECT count(*) FROM data_quality_results WHERE issues::text LIKE '%stale_data%'")
        )
    assert stale >= 1  # the staleness was recorded, not silently ignored


async def test_run_is_idempotent(container: Container, organization: uuid.UUID) -> None:
    values = [60.0] * 20 + [140.0]
    await _seed_series(container, organization, values=values)
    engine = _engine(container)
    window = {"since": BASE, "until": BASE + timedelta(minutes=len(values) + 1)}
    await engine.run_full(organization, correlation_id="first", **window)
    async with container.database.tenant_session(organization) as session:
        first = await session.scalar(text("SELECT count(*) FROM detection_events"))
    await engine.run_full(organization, correlation_id="second", **window)
    async with container.database.tenant_session(organization) as session:
        second = await session.scalar(text("SELECT count(*) FROM detection_events"))
    # The second run does not duplicate the first run's detections (cooldown grouping).
    assert second == first


async def test_detections_are_isolated_between_tenants(
    container: Container,
    organization: uuid.UUID,
    other_organization: uuid.UUID,
) -> None:
    values = [60.0] * 20 + [140.0]
    await _seed_series(container, organization, values=values)
    await _engine(container).run_full(
        organization,
        correlation_id="tenant-a",
        since=BASE,
        until=BASE + timedelta(minutes=len(values) + 1),
    )
    async with container.database.tenant_session(other_organization) as session:
        count = await session.scalar(text("SELECT count(*) FROM detection_events"))
    assert count == 0


async def test_quality_results_are_persisted(container: Container, organization: uuid.UUID) -> None:
    await _seed_series(container, organization, values=[60.0] * 30)
    await _engine(container).run_full(
        organization, correlation_id="quality", since=BASE, until=BASE + timedelta(minutes=31)
    )
    async with container.database.tenant_session(organization) as session:
        count = await session.scalar(text("SELECT count(*) FROM data_quality_results"))
        features = await session.scalar(text("SELECT count(*) FROM feature_values"))
    assert count >= 1
    assert features >= 1


async def test_execution_log_records_each_stage(
    container: Container, organization: uuid.UUID
) -> None:
    await _seed_series(container, organization, values=[60.0] * 10)
    await _engine(container).run_full(
        organization, correlation_id="log", since=BASE, until=BASE + timedelta(minutes=11)
    )
    async with container.database.tenant_session(organization) as session:
        stages = (
            (await session.execute(text("SELECT DISTINCT stage FROM analytics_execution_logs")))
            .scalars()
            .all()
        )
    assert "select" in stages
    assert "persistence" in stages


async def test_sweep_runs_every_tenant_and_isolates_them(
    container: Container,
    organization: uuid.UUID,
    other_organization: uuid.UUID,
) -> None:
    """The sweep visits every tenant, and each tenant's detections stay its own."""
    # The sweep uses the wall clock, so seed data ending near now rather than at BASE.
    now = datetime.now(tz=UTC)
    base = now - timedelta(minutes=21)
    values = [60.0] * 20 + [140.0]
    await _seed_series(container, organization, values=values, base=base)
    engine = _engine(container)
    results = await engine.run_sweep(correlation_id="sweep")
    assert any(r.organization_id == organization for r in results)
    assert any(r.organization_id == other_organization for r in results)
    async with container.database.tenant_session(organization) as session:
        own = await session.scalar(text("SELECT count(*) FROM detection_events"))
    async with container.database.tenant_session(other_organization) as session:
        other = await session.scalar(text("SELECT count(*) FROM detection_events"))
    assert own >= 1
    assert other == 0


async def test_the_sweep_worker_task_is_a_no_op_when_disabled(container: Container) -> None:
    from humisense.workers import worker as worker_module

    container.settings.layer3_sweep_enabled = False
    result = await worker_module.analytics_sweep_task({"container": container})
    assert result == {"tenants": 0, "detections": 0, "skipped": 0}
