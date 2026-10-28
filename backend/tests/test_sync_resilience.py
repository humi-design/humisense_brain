"""Synchronization and resilience tests.

Section 12: connector execution must survive an unreliable source, bound its own
resource use, and leave a recoverable job record.
"""

from __future__ import annotations

import uuid

import pytest

from humisense.errors import ConflictError, NotFoundError
from humisense.services.connections import ConnectionDraft
from humisense.services.sync import (
    SyncStatus,
    reap_stale_jobs,
)
from tests.conftest import (
    run_pending_syncs,
    synthetic_rows,
    write_csv,
)

pytestmark = pytest.mark.security


async def _connection(
    container: Container, principal: Principal, csv_root: Path, name: str = "sync"
):
    write_csv(csv_root / f"{name}.csv", synthetic_rows(5))
    return await container.connections.create_connection(
        principal,
        ConnectionDraft(
            connector_type="csv",
            name=name,
            description=None,
            config={"path": f"{name}.csv"},
            credentials={},
        ),
    )


async def _job(container: Container, principal: Principal, connection: object, **kwargs: object):
    return await container.sync.create_job(
        organization_id=principal.organization_id,
        connection_id=connection.id,
        triggered_by=principal.user_id,
        correlation_id=str(uuid.uuid4()),
        **kwargs,
    )


# --- Happy path ----------------------------------------------------------


async def test_a_job_captures_records_end_to_end(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    connection = await _connection(container, owner, csv_root)
    job = await _job(container, owner, connection)

    outcome = await container.sync.run_job(owner.organization_id, job.id)

    assert outcome.status is SyncStatus.SUCCEEDED
    assert outcome.records_fetched == 5
    assert outcome.records_persisted == 5
    assert outcome.records_delivered == 5


async def test_a_completed_job_records_its_final_state(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    connection = await _connection(container, owner, csv_root)
    job = await _job(container, owner, connection)
    await container.sync.run_job(owner.organization_id, job.id)

    stored = await container.sync.get_job(owner.organization_id, job.id)
    assert stored.status is SyncStatus.SUCCEEDED
    assert stored.finished_at is not None
    assert stored.records_persisted == 5


async def test_running_the_same_job_twice_does_not_duplicate_records(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    """A worker restart may redeliver the same job; capture must stay idempotent."""
    connection = await _connection(container, owner, csv_root)
    job = await _job(container, owner, connection)

    first = await container.sync.run_job(owner.organization_id, job.id)
    second = await container.sync.run_job(owner.organization_id, job.id)

    assert first.records_persisted == 5
    assert second.status is SyncStatus.SUCCEEDED
    assert second.records_persisted == 0
    assert second.error_code == "job_not_queued"

    records = await container.records.list_records(owner.organization_id, connection.id, limit=100)
    assert len(records) == 5


async def test_a_completed_run_leaves_the_checkpoint_at_end_of_input(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    """A run that reaches the end of the file must not re-read its last rows.

    The terminal cursor has to point past the final row. If it points at the final row,
    every later run re-reads and re-persists that row and the checkpoint never reaches
    the true end of the input.
    """
    container.settings.sync_batch_size = 2
    connection = await _connection(container, owner, csv_root)

    first = await _job(container, owner, connection)
    outcome = await container.sync.run_job(owner.organization_id, first.id)
    assert outcome.status is SyncStatus.SUCCEEDED
    assert outcome.records_persisted == 5

    refreshed = await container.connections.get_connection(owner, connection.id)
    assert refreshed.checkpoint is not None, "the connection must carry the checkpoint"

    # A second run over an unchanged file captures nothing new.
    second = await _job(container, owner, connection)
    again = await container.sync.run_job(owner.organization_id, second.id)
    assert again.status is SyncStatus.SUCCEEDED
    assert again.records_persisted == 0

    records = await container.records.list_records(owner.organization_id, connection.id, limit=100)
    assert len(records) == 5


async def test_a_run_resumes_from_the_stored_checkpoint(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    """An interrupted run must continue where it stopped, not restart from the top."""
    container.settings.sync_batch_size = 2
    connection = await _connection(container, owner, csv_root)

    # Stand in for a previous run that was cut short after the second row by reading a
    # real first page and storing its cursor. The cursor carries a file fingerprint, and
    # a mismatched fingerprint deliberately restarts the scan, so a hand-written cursor
    # would not exercise resumption at all.
    stored = await container.connections.get_connection(owner, connection.id)
    connector = await container.runtime.build_connector(stored, correlation_id="resume-setup")
    first_page = await connector.fetch_records(None, 2)
    await connector.close()
    assert first_page.next_cursor is not None

    await container.connections.record_sync_outcome(
        owner.organization_id,
        connection.id,
        ok=True,
        error_code=None,
        checkpoint=first_page.next_cursor,
    )

    job = await _job(container, owner, connection)
    outcome = await container.sync.run_job(owner.organization_id, job.id)

    assert outcome.status is SyncStatus.SUCCEEDED
    # Rows 2, 3 and 4 remain.
    assert outcome.records_persisted == 3


# --- Concurrency and tenant limits ---------------------------------------


async def test_a_second_job_for_the_same_connection_is_refused(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    """T9: one connection must not run two syncs at once."""
    connection = await _connection(container, owner, csv_root)
    await _job(container, owner, connection)

    with pytest.raises(ConflictError):
        await _job(container, owner, connection)


async def test_the_per_tenant_concurrency_limit_is_enforced(
    container: Container, owner: Principal, foreign_owner: Principal, csv_root: Path
) -> None:
    """T9: one tenant cannot queue an unbounded backlog of work.

    The limit counts queued and running jobs together, so it is exercised with two
    queued jobs rather than by racing a worker.
    """
    container.settings.sync_max_concurrent_jobs_per_tenant = 1
    first = await _connection(container, owner, csv_root, name="a")
    second = await _connection(container, owner, csv_root, name="b")

    await _job(container, owner, first)
    with pytest.raises(ConflictError):
        await _job(container, owner, second)

    # A different tenant has its own budget and is unaffected.
    other = await container.connections.create_connection(
        foreign_owner,
        ConnectionDraft(
            connector_type="csv",
            name="a",
            description=None,
            config={"path": "a.csv"},
            credentials={},
        ),
    )
    other_job = await container.sync.create_job(
        organization_id=foreign_owner.organization_id,
        connection_id=other.id,
        triggered_by=foreign_owner.user_id,
        correlation_id=str(uuid.uuid4()),
    )
    assert other_job is not None


async def test_a_disabled_connection_cannot_be_synced(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    connection = await _connection(container, owner, csv_root)
    await container.connections.update_connection(owner, connection.id, enabled=False)

    with pytest.raises(ConflictError):
        await _job(container, owner, connection)


async def test_a_job_for_another_tenant_is_not_visible(
    container: Container, owner: Principal, foreign_owner: Principal, csv_root: Path
) -> None:
    """T2: a job id from one tenant must not resolve for another."""
    connection = await _connection(container, owner, csv_root)
    job = await _job(container, owner, connection)

    with pytest.raises(NotFoundError):
        await container.sync.get_job(foreign_owner.organization_id, job.id)


async def test_cannot_cancel_another_tenants_job(
    container: Container, owner: Principal, foreign_owner: Principal, csv_root: Path
) -> None:
    connection = await _connection(container, owner, csv_root)
    job = await _job(container, owner, connection)

    with pytest.raises(NotFoundError):
        await container.sync.request_cancel(foreign_owner.organization_id, job.id)


# --- Failure handling ----------------------------------------------------


async def test_a_missing_source_file_fails_the_job_without_crashing(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    """T7: an inaccessible source produces an honest failure, not an exception."""
    connection = await container.connections.create_connection(
        owner,
        ConnectionDraft(
            connector_type="csv",
            name="missing",
            description=None,
            config={"path": "does-not-exist.csv"},
            credentials={},
        ),
    )
    job = await _job(container, owner, connection)

    outcome = await container.sync.run_job(owner.organization_id, job.id)

    assert outcome.status is SyncStatus.FAILED
    assert outcome.error_code is not None
    stored = await container.sync.get_job(owner.organization_id, job.id)
    assert stored.status is SyncStatus.FAILED


async def test_a_failed_job_records_an_error_without_leaking_a_path(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    connection = await container.connections.create_connection(
        owner,
        ConnectionDraft(
            connector_type="csv",
            name="missing-2",
            description=None,
            config={"path": "nope.csv"},
            credentials={},
        ),
    )
    job = await _job(container, owner, connection)
    await container.sync.run_job(owner.organization_id, job.id)

    stored = await container.sync.get_job(owner.organization_id, job.id)
    message = stored.error_message or ""
    assert "/" not in message.replace(" ", "")
    assert str(csv_root) not in message


async def test_a_malformed_csv_is_reported_as_partial_not_silent(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    """A partially bad file must not discard the rows that are good."""
    path = csv_root / "mixed.csv"
    path.write_text(
        "timestamp,machine_id,tag,value,unit\n"
        "2024-01-01T00:00:00Z,M-1,temp,1.0,C\n"
        "this,row,is,not,valid,extra,columns\n"
        "2024-01-01T00:01:00Z,M-1,temp,2.0,C\n"
    )
    connection = await container.connections.create_connection(
        owner,
        ConnectionDraft(
            connector_type="csv",
            name="mixed",
            description=None,
            config={"path": "mixed.csv"},
            credentials={},
        ),
    )
    job = await _job(container, owner, connection)
    outcome = await container.sync.run_job(owner.organization_id, job.id)

    assert outcome.records_persisted >= 2
    assert outcome.status in {SyncStatus.SUCCEEDED, SyncStatus.PARTIAL}


async def test_an_empty_csv_succeeds_with_no_records(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    (csv_root / "empty.csv").write_text("timestamp,machine_id,tag,value,unit\n")
    connection = await container.connections.create_connection(
        owner,
        ConnectionDraft(
            connector_type="csv",
            name="empty",
            description=None,
            config={"path": "empty.csv"},
            credentials={},
        ),
    )
    job = await _job(container, owner, connection)
    outcome = await container.sync.run_job(owner.organization_id, job.id)

    assert outcome.records_persisted == 0
    assert outcome.status in {SyncStatus.SUCCEEDED, SyncStatus.FAILED}


# --- Cancellation --------------------------------------------------------


async def test_cancellation_is_observed_and_the_job_is_marked_cancelled(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    connection = await _connection(container, owner, csv_root)
    job = await _job(container, owner, connection)

    # Request cancellation before the worker polls.
    await container.sync.request_cancel(owner.organization_id, job.id)
    outcome = await container.sync.run_job(owner.organization_id, job.id)

    assert outcome.status is SyncStatus.CANCELLED


async def test_cancelling_a_finished_job_is_a_no_op(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    connection = await _connection(container, owner, csv_root)
    job = await _job(container, owner, connection)
    await container.sync.run_job(owner.organization_id, job.id)

    stored = await container.sync.request_cancel(owner.organization_id, job.id)
    assert stored.status is SyncStatus.SUCCEEDED


# --- Recovery ------------------------------------------------------------


async def test_stale_running_jobs_are_reaped(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    """A killed worker must not block the connection forever."""
    connection = await _connection(container, owner, csv_root)
    job = await _job(container, owner, connection)

    # Simulate a worker that died mid-run.
    from sqlalchemy import text

    async with container.database.tenant_session(owner.organization_id) as session:
        await session.execute(
            text(
                "UPDATE sync_jobs SET status = 'running', "
                "started_at = now() - interval '2 hours' WHERE id = :id"
            ),
            {"id": job.id},
        )

    reaped = await reap_stale_jobs(container.database, older_than_seconds=3600)
    assert reaped >= 1

    stored = await container.sync.get_job(owner.organization_id, job.id)
    assert stored.status is SyncStatus.FAILED
    assert stored.error_code == "stale_job_reaped"


async def test_a_reaped_connection_can_run_again(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    connection = await _connection(container, owner, csv_root)
    job = await _job(container, owner, connection)

    from sqlalchemy import text

    async with container.database.tenant_session(owner.organization_id) as session:
        await session.execute(
            text(
                "UPDATE sync_jobs SET status = 'running', "
                "started_at = now() - interval '2 hours' WHERE id = :id"
            ),
            {"id": job.id},
        )
    await reap_stale_jobs(container.database, older_than_seconds=3600)

    new_job = await _job(container, owner, connection)
    outcome = await container.sync.run_job(owner.organization_id, new_job.id)
    assert outcome.status is SyncStatus.SUCCEEDED


async def test_list_jobs_is_tenant_scoped_and_bounded(
    container: Container, owner: Principal, foreign_owner: Principal, csv_root: Path
) -> None:
    connection = await _connection(container, owner, csv_root)
    for _ in range(3):
        job = await _job(container, owner, connection)
        await container.sync.run_job(owner.organization_id, job.id)

    mine = await container.sync.list_jobs(owner.organization_id, connection.id, limit=10)
    theirs = await container.sync.list_jobs(foreign_owner.organization_id, connection.id, limit=10)
    assert len(mine) == 3
    assert theirs == []


# --- Inline queue --------------------------------------------------------


async def test_the_inline_queue_runs_a_queued_job(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    connection = await _connection(container, owner, csv_root)
    job = await _job(container, owner, connection)
    await container.enqueue_sync(
        organization_id=owner.organization_id,
        job_id=job.id,
        correlation_id=str(uuid.uuid4()),
    )
    await run_pending_syncs(container)

    jobs = await container.sync.list_jobs(owner.organization_id, connection.id, limit=10)
    assert jobs
    assert jobs[0].status is SyncStatus.SUCCEEDED


from pathlib import Path  # noqa: E402

from humisense.container import Container  # noqa: E402
from humisense.security.identity import Principal  # noqa: E402
