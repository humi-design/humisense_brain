"""Layer 2 ingestion benchmark.

Measures the pipeline end to end against a real PostgreSQL database using the committed
synthetic historian pattern. The brief requires a benchmark before adopting a separate
time-series store; this is that benchmark, and it is reproducible rather than a one-off.

It is a measurement tool, not a test: it is run by hand or by a release step, it prints
its results, and it asserts nothing. Numbers belong in ``docs/LAYER2-RELEASE.md``.

Run with::

    uv run humisense benchmark --records 5000 --batch-size 500
"""

from __future__ import annotations

import resource
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from humisense.config import Settings
from humisense.container import build_container
from humisense.db.models import Organization
from humisense.db.session import Database
from humisense.layer2.pipeline import IngestionPipeline, IngestRecord

# One synthetic historian tag cycle: the shapes the CSV connector produces, so the
# benchmark measures the real normalizers rather than a synthetic no-op.
_TAGS = (
    ("temperature", "degC"),
    ("pressure", "bar"),
    ("vibration", "mm_s"),
    ("throughput", "units_min"),
)
_MACHINES = ("LINE-01", "LINE-02", "LINE-03")


@dataclass(slots=True)
class BenchmarkResult:
    records: int
    batch_size: int
    batches: int
    accepted: int
    duplicates: int
    quarantined: int
    failed: int
    elapsed_seconds: float
    redelivered_duplicates: int = 0
    redelivery_seconds: float = 0.0

    @property
    def records_per_second(self) -> float:
        return self.records / self.elapsed_seconds if self.elapsed_seconds > 0 else 0.0


def _build_records(connection_id: uuid.UUID, count: int) -> list[IngestRecord]:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    records: list[IngestRecord] = []
    for index in range(count):
        tag, unit = _TAGS[(index // len(_MACHINES)) % len(_TAGS)]
        records.append(
            IngestRecord(
                connection_id=connection_id,
                connector_type="csv",
                source_record_id=f"bench-{index}",
                record_type="csv_row",
                source_timestamp=base + timedelta(minutes=index),
                observed_timestamp=base + timedelta(minutes=index),
                payload={
                    "machine_id": _MACHINES[index % len(_MACHINES)],
                    "tag": tag,
                    "value": 60.0 + (index % 100) / 10.0,
                    "unit": unit,
                },
                metadata={},
                source_schema_version="1.0",
                content_hash=None,
                source_record_pk=None,
            )
        )
    return records


async def _create_connection(database: Database) -> tuple[uuid.UUID, uuid.UUID]:
    async with database.system_session() as session:
        organization = Organization(name="Benchmark", slug=f"bench-{uuid.uuid4().hex[:8]}")
        session.add(organization)
        await session.flush()
        organization_id = organization.id
    async with database.tenant_session(organization_id) as session:
        connection_id = (
            await session.execute(
                text(
                    "INSERT INTO connections (id, organization_id, connector_type, name, "
                    "config, status, enabled, capabilities, created_at, updated_at) VALUES "
                    "(gen_random_uuid(), :org, 'csv', 'bench', '{}', 'active', true, '{}', "
                    "now(), now()) RETURNING id"
                ),
                {"org": organization_id},
            )
        ).scalar_one()
    return organization_id, connection_id


async def run_benchmark(
    settings: Settings, *, records: int = 5000, batch_size: int = 500
) -> BenchmarkResult:
    """Ingest ``records`` records through the real pipeline and report the result."""
    container = build_container(settings)
    try:
        organization_id, connection_id = await _create_connection(container.database)
        pipeline = IngestionPipeline(container.database, container.settings)
        all_records = _build_records(connection_id, records)
        chunks = [all_records[i : i + batch_size] for i in range(0, len(all_records), batch_size)]

        started = time.perf_counter()
        accepted = duplicates = quarantined = failed = 0
        for index, chunk in enumerate(chunks):
            result = await pipeline.ingest_batch(
                organization_id=organization_id,
                batch_id=f"bench-batch-{index}",
                correlation_id=f"bench-{index}",
                records=chunk,
            )
            accepted += result.accepted
            duplicates += result.duplicates
            quarantined += result.quarantined
            failed += result.failed
        elapsed = time.perf_counter() - started

        # Redelivery: re-run the first batch. This measures the idempotency path — every
        # record must be recognized as a duplicate rather than stored twice.
        redelivered_duplicates = 0
        redelivery_seconds = 0.0
        if chunks:
            redelivery_started = time.perf_counter()
            redelivered = await pipeline.ingest_batch(
                organization_id=organization_id,
                batch_id="bench-batch-0",
                correlation_id="bench-redelivery",
                records=chunks[0],
            )
            redelivery_seconds = time.perf_counter() - redelivery_started
            redelivered_duplicates = redelivered.duplicates
    finally:
        await container.close()

    return BenchmarkResult(
        records=records,
        batch_size=batch_size,
        batches=len(chunks),
        accepted=accepted,
        duplicates=duplicates,
        quarantined=quarantined,
        failed=failed,
        elapsed_seconds=elapsed,
        redelivered_duplicates=redelivered_duplicates,
        redelivery_seconds=redelivery_seconds,
    )


async def command_benchmark(settings: Settings, *, records: int, batch_size: int) -> int:
    """Run the benchmark and print the numbers to record in the release document."""
    print(f"layer2 benchmark: records={records} batch_size={batch_size}")
    result = await run_benchmark(settings, records=records, batch_size=batch_size)
    peak_rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    print(f"  batches:            {result.batches}")
    print(f"  accepted:           {result.accepted}")
    print(f"  duplicates:         {result.duplicates}")
    print(f"  quarantined:        {result.quarantined}")
    print(f"  failed:             {result.failed}")
    print(f"  elapsed_seconds:    {result.elapsed_seconds:.3f}")
    print(f"  records_per_second: {result.records_per_second:.1f}")
    print(f"  peak_rss_mb:        {peak_rss_mb:.1f}")
    print(
        f"  redelivery:         {result.redelivered_duplicates} duplicates of "
        f"{result.batch_size} in {result.redelivery_seconds:.3f}s"
    )
    if result.redelivered_duplicates != result.batch_size:
        print("  WARNING: redelivery was not fully deduplicated; inspect before citing it")
        return 1
    if result.failed or result.quarantined:
        print("  WARNING: the run produced non-accepted records; inspect before citing it")
        return 1
    return 0


__all__ = ["BenchmarkResult", "command_benchmark", "run_benchmark"]
