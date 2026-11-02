"""Milestone 1: the canonical contract, the state machine, and the Layer 2 schema.

These are the foundations every later stage depends on, so they are tested first and
independently of the pipeline. The schema tests run against the real PostgreSQL database
through the migration the test session applies, so they verify the RLS policies and the
immutability triggers rather than a ``create_all`` approximation.
"""

from __future__ import annotations

import itertools
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from humisense.container import Container
from humisense.layer2.contract import (
    CANONICAL_SCHEMA_VERSION,
    CanonicalRecord,
    Provenance,
    UnitConversion,
)
from humisense.layer2.states import (
    IllegalTransition,
    ProcessingState,
    allowed_targets,
    assert_transition,
    can_transition,
    is_terminal,
)


def _provenance(organization_id: uuid.UUID) -> Provenance:
    return Provenance(
        source_system="csv",
        connector_type="csv",
        connection_id=uuid.uuid4(),
        source_record_id="row-1",
        source_schema_version="1.0",
        content_hash="abc123",
        correlation_id="corr-1",
    )


def _record(organization_id: uuid.UUID, **overrides: object) -> CanonicalRecord:
    now = datetime.now(UTC)
    base: dict[str, object] = {
        "record_id": uuid.uuid4(),
        "organization_id": organization_id,
        "idempotency_key": "conn:row-1:abc123",
        "record_type": "csv_row",
        "data_family": "generic",
        "observed_timestamp": now,
        "received_timestamp": now,
        "ingestion_status": "received",
        "correlation_id": "corr-1",
        "provenance": _provenance(organization_id),
    }
    base.update(overrides)
    return CanonicalRecord(**base)  # type: ignore[arg-type]


# --- Contract ---------------------------------------------------------------


def test_canonical_record_carries_the_required_contract_fields() -> None:
    """The contract includes every field the brief requires."""
    record = _record(uuid.uuid4())
    dumped = record.model_dump()
    for field in (
        "organization_id",
        "record_id",
        "record_type",
        "entity_type",
        "event_type",
        "source_timestamp",
        "event_timestamp",
        "observed_timestamp",
        "received_timestamp",
        "processed_timestamp",
        "normalized_payload",
        "metadata",
        "schema_version",
        "source_timezone",
        "source_time_metadata",
        "correlation_id",
        "idempotency_key",
        "ingestion_status",
        "processing_version",
        "provenance",
    ):
        assert field in dumped, field
    assert dumped["schema_version"] == CANONICAL_SCHEMA_VERSION


def test_naive_timestamps_are_refused() -> None:
    """A timestamp without a timezone is refused rather than assumed to be UTC."""
    with pytest.raises(ValidationError):
        _record(uuid.uuid4(), observed_timestamp=datetime(2026, 1, 1, 0, 0, 0))


def test_timezone_aware_timestamps_are_normalized_to_utc() -> None:
    """A non-UTC offset is converted to UTC but its offset is recoverable from the source."""
    plus_two = timezone(timedelta(hours=2))
    record = _record(
        uuid.uuid4(),
        source_timestamp=datetime(2026, 1, 1, 12, 0, 0, tzinfo=plus_two),
        source_timezone="+02:00",
    )
    assert record.source_timestamp == datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
    assert record.source_timezone == "+02:00"


def test_unit_conversion_is_recorded_not_silent() -> None:
    """A conversion carries the original and normalized value, unit, and rule."""
    conversion = UnitConversion(
        field="temperature",
        original_value=212.0,
        original_unit="degF",
        normalized_value=100.0,
        normalized_unit="degC",
        rule="degF_to_degC",
    )
    record = _record(uuid.uuid4(), unit_conversions=[conversion])
    assert record.unit_conversions[0].original_value == 212.0
    assert record.unit_conversions[0].rule == "degF_to_degC"


def test_effective_event_time_does_not_invent_a_value() -> None:
    """With no source or event timestamp, the effective event time is None."""
    record = _record(uuid.uuid4())
    assert record.effective_event_time() is None


def test_extensions_are_bounded() -> None:
    """Too many extension keys are refused, so a record cannot become unbounded."""
    with pytest.raises(ValidationError):
        _record(uuid.uuid4(), extensions={f"k{i}": i for i in range(1000)})


# --- State machine ----------------------------------------------------------


def test_happy_path_transitions_are_legal() -> None:
    chain = [
        ProcessingState.RECEIVED,
        ProcessingState.VALIDATING,
        ProcessingState.VALIDATED,
        ProcessingState.NORMALIZING,
        ProcessingState.NORMALIZED,
        ProcessingState.STORED,
        ProcessingState.PUBLISHED,
    ]
    for source, target in itertools.pairwise(chain):
        assert can_transition(source, target)
        assert_transition(source, target)


def test_illegal_transition_raises() -> None:
    """A record cannot jump from RECEIVED straight to PUBLISHED."""
    assert not can_transition(ProcessingState.RECEIVED, ProcessingState.PUBLISHED)
    with pytest.raises(IllegalTransition):
        assert_transition(ProcessingState.RECEIVED, ProcessingState.PUBLISHED)


def test_terminal_states_have_no_outgoing_transitions() -> None:
    for state in (
        ProcessingState.PUBLISHED,
        ProcessingState.FAILED,
        ProcessingState.QUARANTINED,
        ProcessingState.DUPLICATE,
    ):
        assert is_terminal(state)
        assert allowed_targets(state) == frozenset()


# --- Schema -----------------------------------------------------------------


async def test_layer2_tables_have_row_level_security(container: Container) -> None:
    """Every Layer 2 table has ENABLE and FORCE row-level security."""
    async with container.database.system_session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname IN "
                    "('ingest_batches','raw_records','canonical_records',"
                    "'record_quarantine','outbox_events','record_processing_events')"
                )
            )
        ).all()
    assert len(rows) == 6
    for name, enabled, forced in rows:
        assert enabled, f"{name} has RLS disabled"
        assert forced, f"{name} does not FORCE RLS"


async def test_raw_records_reject_update_at_the_database(
    container: Container, organization: uuid.UUID
) -> None:
    """The preserved payload is immutable at the storage boundary, not just in code."""
    async with container.database.tenant_session(organization) as session:
        connection_id = (
            await session.execute(
                text(
                    "INSERT INTO connections (id, organization_id, connector_type, name, "
                    "config, status, enabled, capabilities, created_at, updated_at) VALUES "
                    "(gen_random_uuid(), :org, 'csv', 'c', '{}', 'pending', true, '{}', "
                    "now(), now()) RETURNING id"
                ),
                {"org": organization},
            )
        ).scalar_one()
        await session.execute(
            text(
                "INSERT INTO raw_records (id, organization_id, connection_id, "
                "connector_type, source_record_key, record_type, source_schema_version, "
                "idempotency_key, observed_timestamp, payload, record_metadata, content_hash, "
                "correlation_id, untrusted) VALUES (gen_random_uuid(), :org, :conn, 'csv', "
                "'r1', 'csv_row', '1.0', 'k1', now(), '{}', '{}', 'h1', 'corr', true)"
            ),
            {"org": organization, "conn": connection_id},
        )

    with pytest.raises(DBAPIError) as excinfo:
        async with container.database.tenant_session(organization) as session:
            await session.execute(text("UPDATE raw_records SET content_hash = 'h2'"))
    assert "append-only" in str(excinfo.value)


async def test_processing_ledger_rejects_delete(
    container: Container, organization: uuid.UUID
) -> None:
    """The processing ledger is append-only at the database boundary."""
    async with container.database.tenant_session(organization) as session:
        await session.execute(
            text(
                "INSERT INTO record_processing_events "
                "(id, organization_id, to_state, stage, correlation_id) "
                "VALUES (gen_random_uuid(), :org, 'received', 'receive', 'corr')"
            ),
            {"org": organization},
        )
    with pytest.raises(DBAPIError) as excinfo:
        async with container.database.tenant_session(organization) as session:
            await session.execute(text("DELETE FROM record_processing_events"))
    assert "append-only" in str(excinfo.value)


async def test_layer2_tables_are_tenant_isolated(
    container: Container, organization: uuid.UUID, other_organization: uuid.UUID
) -> None:
    """A tenant session cannot see another tenant's batch row."""
    async with container.database.tenant_session(organization) as session:
        await session.execute(
            text(
                "INSERT INTO ingest_batches (id, organization_id, batch_id, "
                "correlation_id, status, records_received, created_at) VALUES "
                "(gen_random_uuid(), :org, 'batch-1', 'corr', 'received', 0, now())"
            ),
            {"org": organization},
        )
    async with container.database.tenant_session(other_organization) as session:
        count = await session.scalar(text("SELECT count(*) FROM ingest_batches"))
    assert int(count or 0) == 0
