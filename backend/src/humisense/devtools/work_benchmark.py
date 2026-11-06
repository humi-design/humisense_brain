"""Layer 4 work-intelligence benchmark.

Measures a real derivation run against a real PostgreSQL database: how long it takes to
read a bounded window of canonical records, derive work entities, associate detections,
and compute the read models the UI uses. It is a measurement tool, not a test: it prints
its results, asserts nothing, and the numbers belong in ``docs/LAYER4-RELEASE.md``.

The fixture is deliberately the same shape the ingestion pipeline produces — business
records with a ``record_kind`` of ``task`` — so the benchmark exercises the real derivation
path rather than a synthetic no-op.

Run with::

    uv run humisense work-benchmark --records 5000
"""

from __future__ import annotations

import resource
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from humisense.config import Settings
from humisense.container import build_container
from humisense.db.models import Organization
from humisense.db.session import Database
from humisense.layer2.pipeline import IngestionPipeline, IngestRecord
from humisense.layer4.views import WorkScope

# A small set of work-like business records. The normalizer maps ``task`` record types to
# the ``task`` record kind, which Layer 4 derives a work entity from.
# Every kind must resolve to a Layer 2 record family, have a normalizer, and be a
# derivable work record type. These three business kinds satisfy all three conditions; a
# kind that fails any of them is quarantined or derived as nothing, which would make the
# measurement describe the fixture rather than the engine.
_KINDS = ("task", "purchase_order", "maintenance_event")
_PARTIES = ("procurement", "production", "maintenance", "sales", "quality")


@dataclass(slots=True)
class WorkBenchmarkResult:
    records: int
    accepted: int
    quarantined: int
    failed: int
    derived: int
    updated: int
    skips: int
    detections_associated: int
    elapsed_seconds: float
    ingest_seconds: float
    my_work_seconds: float
    work_list_seconds: float

    @property
    def records_per_second(self) -> float:
        return self.records / self.elapsed_seconds if self.elapsed_seconds > 0 else 0.0


def _build_records(connection_id: uuid.UUID, count: int) -> list[IngestRecord]:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    records: list[IngestRecord] = []
    for index in range(count):
        kind = _KINDS[index % len(_KINDS)]
        party = _PARTIES[index % len(_PARTIES)]
        due = base + timedelta(days=1 + (index % 30))
        records.append(
            IngestRecord(
                connection_id=connection_id,
                connector_type="csv",
                source_record_id=f"work-bench-{index}",
                record_type=kind,
                source_timestamp=base + timedelta(minutes=index),
                observed_timestamp=base + timedelta(minutes=index),
                payload={
                    "id": f"WB-{index}",
                    "title": f"Work item {index}",
                    "status": "open",
                    "priority": "normal",
                    "due_date": due.isoformat(),
                    "owner": party,
                    "machine_id": f"MACHINE-{index % 20}",
                },
                metadata={},
                source_schema_version="1.0",
            )
        )
    return records


async def _create_organization(database: Database) -> tuple[uuid.UUID, uuid.UUID]:
    async with database.system_session() as session:
        organization = Organization(name="WorkBench", slug=f"wbench-{uuid.uuid4().hex[:8]}")
        session.add(organization)
        await session.flush()
        organization_id = organization.id
    async with database.tenant_session(organization_id) as session:
        from sqlalchemy import text

        connection_id = (
            await session.execute(
                text(
                    "INSERT INTO connections (id, organization_id, connector_type, name, "
                    "config, status, enabled, capabilities, created_at, updated_at) VALUES "
                    "(gen_random_uuid(), :org, 'csv', 'work-bench', '{}', 'active', true, '{}', "
                    "now(), now()) RETURNING id"
                ),
                {"org": organization_id},
            )
        ).scalar_one()
    return organization_id, connection_id


async def run_work_benchmark(
    settings: Settings, *, records: int = 5000, batch_size: int = 500
) -> WorkBenchmarkResult:
    """Ingest ``records`` business records, derive work, and measure the read models."""
    container = build_container(settings)
    try:
        organization_id, connection_id = await _create_organization(container.database)
        pipeline = IngestionPipeline(container.database, container.settings)
        all_records = _build_records(connection_id, records)
        chunks = [all_records[i : i + batch_size] for i in range(0, len(all_records), batch_size)]

        ingest_started = time.perf_counter()
        accepted = quarantined = failed = 0
        for index, chunk in enumerate(chunks):
            ingest = await pipeline.ingest_batch(
                organization_id=organization_id,
                batch_id=f"work-bench-batch-{index}",
                correlation_id=f"work-bench-{index}",
                records=chunk,
            )
            accepted += ingest.accepted
            quarantined += ingest.quarantined
            failed += ingest.failed
        ingest_seconds = time.perf_counter() - ingest_started

        derive_started = time.perf_counter()
        result = await container.layer4_engine.run(
            organization_id,
            correlation_id="work-bench-derive",
            since=datetime(2026, 1, 1, tzinfo=UTC),
            until=datetime(2026, 3, 1, tzinfo=UTC),
            row_limit=records,
        )
        elapsed = time.perf_counter() - derive_started

        scope = WorkScope(organization_id=organization_id, user_id=uuid.uuid4(), role="owner")
        my_work_started = time.perf_counter()
        await container.work.personal_view(scope, as_of=datetime(2026, 3, 1, tzinfo=UTC))
        my_work_seconds = time.perf_counter() - my_work_started

        list_started = time.perf_counter()
        await container.layer4_repository.list_work_entities(organization_id, limit=50, offset=0)
        work_list_seconds = time.perf_counter() - list_started
    finally:
        await container.close()

    return WorkBenchmarkResult(
        records=records,
        accepted=accepted,
        quarantined=quarantined,
        failed=failed,
        derived=result.work_created,
        updated=result.work_updated,
        skips=len(result.skips),
        detections_associated=result.detections_associated,
        elapsed_seconds=elapsed,
        ingest_seconds=ingest_seconds,
        my_work_seconds=my_work_seconds,
        work_list_seconds=work_list_seconds,
    )


async def command_work_benchmark(settings: Settings, *, records: int) -> int:
    """Run the benchmark and print the numbers to record in the release document."""
    print(f"layer4 work benchmark: records={records}")
    result = await run_work_benchmark(settings, records=records)
    peak_rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    print(f"  accepted:               {result.accepted}")
    print(f"  quarantined:            {result.quarantined}")
    print(f"  failed:                 {result.failed}")
    print(f"  derived:                {result.derived}")
    print(f"  updated:                {result.updated}")
    print(f"  skips:                  {result.skips}")
    print(f"  detections_associated:  {result.detections_associated}")
    print(f"  ingest_seconds:         {result.ingest_seconds:.3f}")
    print(f"  derive_seconds:         {result.elapsed_seconds:.3f}")
    print(f"  records_per_second:     {result.records_per_second:.1f}")
    print(f"  my_work_seconds:        {result.my_work_seconds:.3f}")
    print(f"  work_list_seconds:      {result.work_list_seconds:.3f}")
    print(f"  peak_rss_mb:            {peak_rss_mb:.1f}")
    if result.skips:
        print("  WARNING: the run produced skips; inspect before citing it")
        return 1
    return 0


__all__ = ["WorkBenchmarkResult", "command_work_benchmark", "run_work_benchmark"]
