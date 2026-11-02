"""Audit trail tests.

Section 11 and T16: security-sensitive operations must leave a durable, tenant-scoped,
non-repudiable record that does not itself leak secrets.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import DatabaseError

from humisense.services.audit import (
    OUTCOME_DENIED,
    OUTCOME_FAILURE,
    OUTCOME_SUCCESS,
    AuditAction,
    AuditActor,
    AuditEntry,
)

pytestmark = pytest.mark.security


async def test_recording_an_event_persists_it(container: Container, owner: Principal) -> None:
    actor = AuditActor.user(owner, correlation_id="corr-1")
    await container.audit.record(
        actor,
        AuditEntry(
            action=AuditAction.CONNECTION_CREATED,
            outcome=OUTCOME_SUCCESS,
            target_type="connection",
            target_id=str(uuid.uuid4()),
        ),
    )
    events = await container.audit.list_events(owner.organization_id, limit=100)
    assert len(events) == 1
    assert events[0].action == AuditAction.CONNECTION_CREATED
    assert events[0].outcome == OUTCOME_SUCCESS


async def test_event_records_the_actor_and_correlation_id(
    container: Container, owner: Principal
) -> None:
    actor = AuditActor.user(owner, correlation_id="trace-me")
    await container.audit.record(
        actor, AuditEntry(action=AuditAction.SYNC_REQUESTED, outcome=OUTCOME_SUCCESS)
    )
    event = (await container.audit.list_events(owner.organization_id, limit=100))[0]
    assert event.actor_id == owner.user_id
    assert event.correlation_id == "trace-me"
    assert event.organization_id == owner.organization_id


async def test_audit_events_are_tenant_scoped(
    container: Container, owner: Principal, foreign_owner: Principal
) -> None:
    """T2: one tenant's audit trail is never visible to another."""
    await container.audit.record(
        AuditActor.user(owner),
        AuditEntry(action=AuditAction.CONNECTION_CREATED, outcome=OUTCOME_SUCCESS),
    )
    await container.audit.record(
        AuditActor.user(foreign_owner),
        AuditEntry(action=AuditAction.CONNECTION_DELETED, outcome=OUTCOME_SUCCESS),
    )

    mine = await container.audit.list_events(owner.organization_id, limit=100)
    theirs = await container.audit.list_events(foreign_owner.organization_id, limit=100)
    assert {event.action for event in mine} == {AuditAction.CONNECTION_CREATED}
    assert {event.action for event in theirs} == {AuditAction.CONNECTION_DELETED}


async def test_credential_shaped_metadata_is_redacted(
    container: Container, owner: Principal
) -> None:
    """T13: an audit record must not become a place secrets leak into."""
    await container.audit.record(
        AuditActor.user(owner),
        AuditEntry(
            action=AuditAction.CONNECTION_CREDENTIALS_SET,
            outcome=OUTCOME_SUCCESS,
            metadata={
                "api_key": "sk-live-should-not-appear",
                "password": "hunter2",
                "token": "bearer-abc",
                "host": "historian.example.com",
            },
        ),
    )
    event = (await container.audit.list_events(owner.organization_id, limit=100))[0]
    rendered = str(event.event_metadata)
    assert "sk-live-should-not-appear" not in rendered
    assert "hunter2" not in rendered
    assert "bearer-abc" not in rendered
    # Non-sensitive context survives, so the trail stays useful.
    assert event.event_metadata["host"] == "historian.example.com"


async def test_oversized_metadata_is_bounded(container: Container, owner: Principal) -> None:
    """T9: the audit table must not be usable as bulk storage."""
    await container.audit.record(
        AuditActor.user(owner),
        AuditEntry(
            action=AuditAction.CONNECTION_UPDATED,
            outcome=OUTCOME_SUCCESS,
            metadata={"note": "x" * 100_000, "extra": [1] * 1000},
        ),
    )
    event = (await container.audit.list_events(owner.organization_id, limit=100))[0]
    assert len(str(event.event_metadata["note"])) < 10_000
    assert len(event.event_metadata["extra"]) <= 10


async def test_metadata_key_count_is_bounded(container: Container, owner: Principal) -> None:
    await container.audit.record(
        AuditActor.user(owner),
        AuditEntry(
            action=AuditAction.CONNECTION_UPDATED,
            outcome=OUTCOME_SUCCESS,
            metadata={f"key-{index}": index for index in range(500)},
        ),
    )
    event = (await container.audit.list_events(owner.organization_id, limit=100))[0]
    assert len(event.event_metadata) <= 50


async def test_denied_actions_are_recorded(container: Container, owner: Principal) -> None:
    """A refused action is exactly the event an investigation needs to see."""
    await container.audit.record(
        AuditActor.user(owner),
        AuditEntry(
            action=AuditAction.AUTHORIZATION_DENIED,
            outcome=OUTCOME_DENIED,
            target_type="connection",
            metadata={"required_permission": "connections:sync"},
        ),
    )
    event = (await container.audit.list_events(owner.organization_id, limit=100))[0]
    assert event.outcome == OUTCOME_DENIED


async def test_a_failing_audit_write_does_not_raise_into_the_caller(
    container: Container, owner: Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auditing must never be able to break the business operation it observes."""
    import humisense.services.audit as audit_module

    def explode(*args: object, **kwargs: object) -> None:
        raise RuntimeError("audit storage is unavailable")

    monkeypatch.setattr(audit_module, "_sanitize_metadata", explode)
    # No exception escapes.
    await container.audit.record(
        AuditActor.user(owner),
        AuditEntry(action=AuditAction.SYNC_STARTED, outcome=OUTCOME_SUCCESS),
    )


async def test_events_are_returned_newest_first(container: Container, owner: Principal) -> None:
    for action in (
        AuditAction.CONNECTION_CREATED,
        AuditAction.CONNECTION_TESTED,
        AuditAction.SYNC_REQUESTED,
    ):
        await container.audit.record(
            AuditActor.user(owner), AuditEntry(action=action, outcome=OUTCOME_SUCCESS)
        )
    events = await container.audit.list_events(owner.organization_id, limit=100)
    assert [event.action for event in events] == [
        AuditAction.SYNC_REQUESTED,
        AuditAction.CONNECTION_TESTED,
        AuditAction.CONNECTION_CREATED,
    ]


async def test_event_count_matches_the_stored_events(
    container: Container, owner: Principal
) -> None:
    for _ in range(4):
        await container.audit.record(
            AuditActor.user(owner),
            AuditEntry(action=AuditAction.RECORDS_VIEWED, outcome=OUTCOME_SUCCESS),
        )
    assert await container.audit.count_events(owner.organization_id) == 4


async def test_audit_records_cannot_be_updated(container: Container, owner: Principal) -> None:
    """Section 11: the trail must be append-only, enforced by the database."""
    from sqlalchemy import text

    await container.audit.record(
        AuditActor.user(owner),
        AuditEntry(action=AuditAction.CONNECTION_CREATED, outcome=OUTCOME_SUCCESS),
    )
    event_id = (await container.audit.list_events(owner.organization_id, limit=100))[0].id

    with pytest.raises(DatabaseError):
        async with container.database.tenant_session(owner.organization_id) as session:
            await session.execute(
                text("UPDATE audit_events SET action = 'tampered' WHERE id = :id"),
                {"id": event_id},
            )


async def test_audit_records_cannot_be_deleted(container: Container, owner: Principal) -> None:
    from sqlalchemy import text

    await container.audit.record(
        AuditActor.user(owner),
        AuditEntry(action=AuditAction.CONNECTION_CREATED, outcome=OUTCOME_SUCCESS),
    )
    event_id = (await container.audit.list_events(owner.organization_id, limit=100))[0].id

    with pytest.raises(DatabaseError):
        async with container.database.tenant_session(owner.organization_id) as session:
            await session.execute(text("DELETE FROM audit_events WHERE id = :id"), {"id": event_id})


async def test_worker_actor_is_recorded_without_a_user(
    container: Container, owner: Principal
) -> None:
    """A background job acts on behalf of the tenant, not a user."""
    await container.audit.record(
        AuditActor.worker(owner.organization_id, correlation_id="job-1"),
        AuditEntry(action=AuditAction.SYNC_STARTED, outcome=OUTCOME_SUCCESS),
    )
    event = (await container.audit.list_events(owner.organization_id, limit=100))[0]
    assert event.actor_id is None
    assert event.actor_type.value == "worker"
    assert event.correlation_id == "job-1"


async def test_sync_failure_is_audited(container: Container, owner: Principal) -> None:
    await container.audit.record(
        AuditActor.worker(owner.organization_id),
        AuditEntry(
            action=AuditAction.SYNC_FAILED,
            outcome=OUTCOME_FAILURE,
            metadata={"error_code": "connector_source_error"},
        ),
    )
    event = (await container.audit.list_events(owner.organization_id, limit=100))[0]
    assert event.outcome == OUTCOME_FAILURE
    assert event.event_metadata["error_code"] == "connector_source_error"


async def test_list_events_is_bounded(container: Container, owner: Principal) -> None:
    for _ in range(30):
        await container.audit.record(
            AuditActor.user(owner),
            AuditEntry(action=AuditAction.RECORDS_VIEWED, outcome=OUTCOME_SUCCESS),
        )
    events = await container.audit.list_events(owner.organization_id, limit=10)
    assert len(events) == 10


from humisense.container import Container  # noqa: E402
from humisense.security.identity import Principal  # noqa: E402
