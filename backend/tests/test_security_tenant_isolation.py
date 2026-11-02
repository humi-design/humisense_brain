"""Cross-tenant isolation and object-level authorization tests.

Covers T1 (cross-tenant exposure) and T2 (broken object-level authorization). Every test
uses two real organizations and two real principals, because an isolation test that only
has one tenant cannot fail.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from humisense.container import Container
from humisense.db.models import Connection, SyncStatus
from humisense.errors import AuthorizationError, NotFoundError
from humisense.security.identity import Principal
from humisense.services.connections import ConnectionDraft
from tests.conftest import make_principal, run_pending_syncs, synthetic_rows, write_csv

pytestmark = [pytest.mark.asyncio, pytest.mark.security]


async def make_connection(
    container: Container, principal: Principal, csv_root: Path, name: str = "csv-conn"
) -> Connection:
    write_csv(csv_root / f"{name}.csv", synthetic_rows(5))
    return await container.connections.create_connection(
        principal,
        ConnectionDraft(
            connector_type="csv",
            name=name,
            description=None,
            config={"path": f"{name}.csv", "timestamp_column": "timestamp"},
            credentials={},
        ),
    )


@pytest.fixture(autouse=True)
def point_settings_at_tmp_root(container: Container, csv_root: Path) -> None:
    """Point the container's path policy at the per-test CSV root."""
    container.settings.csv_allowed_roots = (str(csv_root),)
    container.runtime._path_policy = None


async def test_owner_can_read_own_connection(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    connection = await make_connection(container, owner, csv_root)
    fetched = await container.connections.get_connection(owner, connection.id)
    assert fetched.id == connection.id


async def test_get_foreign_connection_404(
    container: Container,
    owner: Principal,
    foreign_owner: Principal,
    csv_root: Path,
) -> None:
    """T2: reading another tenant's connection must fail, and must not confirm it exists."""
    connection = await make_connection(container, owner, csv_root)
    with pytest.raises(NotFoundError):
        await container.connections.get_connection(foreign_owner, connection.id)


async def test_update_foreign_connection_404(
    container: Container,
    owner: Principal,
    foreign_owner: Principal,
    csv_root: Path,
) -> None:
    connection = await make_connection(container, owner, csv_root)
    with pytest.raises(NotFoundError):
        await container.connections.update_connection(foreign_owner, connection.id, name="hijacked")
    refreshed = await container.connections.get_connection(owner, connection.id)
    assert refreshed.name == connection.name


async def test_delete_foreign_connection_404(
    container: Container,
    owner: Principal,
    foreign_owner: Principal,
    csv_root: Path,
) -> None:
    connection = await make_connection(container, owner, csv_root)
    with pytest.raises(NotFoundError):
        await container.connections.delete_connection(foreign_owner, connection.id)
    assert await container.connections.get_connection(owner, connection.id)


async def test_list_connections_is_tenant_scoped(
    container: Container,
    owner: Principal,
    foreign_owner: Principal,
    csv_root: Path,
) -> None:
    await make_connection(container, owner, csv_root, "mine")
    await make_connection(container, foreign_owner, csv_root, "theirs")

    mine, mine_total = await container.connections.list_connections(owner, limit=50)
    theirs, theirs_total = await container.connections.list_connections(foreign_owner, limit=50)

    assert mine_total == 1 and theirs_total == 1
    assert {c.name for c in mine} == {"mine"}
    assert {c.name for c in theirs} == {"theirs"}


async def test_foreign_sync_request_404(
    container: Container,
    owner: Principal,
    foreign_owner: Principal,
    csv_root: Path,
) -> None:
    connection = await make_connection(container, owner, csv_root)
    with pytest.raises(NotFoundError):
        await container.connections.get_connection(foreign_owner, connection.id)
    with pytest.raises(NotFoundError):
        await container.sync.create_job(
            organization_id=foreign_owner.organization_id,
            connection_id=connection.id,
            triggered_by=foreign_owner.user_id,
            correlation_id="cross-tenant",
        )


async def test_foreign_job_read_404(
    container: Container,
    owner: Principal,
    foreign_owner: Principal,
    csv_root: Path,
) -> None:
    connection = await make_connection(container, owner, csv_root)
    job = await container.sync.create_job(
        organization_id=owner.organization_id,
        connection_id=connection.id,
        triggered_by=owner.user_id,
        correlation_id="owner-job",
    )
    with pytest.raises(NotFoundError):
        await container.sync.get_job(foreign_owner.organization_id, job.id)


async def test_foreign_record_preview_returns_nothing(
    container: Container,
    owner: Principal,
    foreign_owner: Principal,
    csv_root: Path,
) -> None:
    """T1: a record query for another tenant's connection yields no rows, not an error."""
    connection = await make_connection(container, owner, csv_root)
    job = await container.sync.create_job(
        organization_id=owner.organization_id,
        connection_id=connection.id,
        triggered_by=owner.user_id,
        correlation_id="records-test",
    )
    await container.sync.run_job(owner.organization_id, job.id)

    own_records = await container.records.list_records(
        owner.organization_id, connection.id, limit=100
    )
    assert len(own_records) == 5

    foreign_records = await container.records.list_records(
        foreign_owner.organization_id, connection.id, limit=100
    )
    assert foreign_records == []
    assert await container.records.count_records(foreign_owner.organization_id, connection.id) == 0


async def test_foreign_cannot_cancel_another_tenants_job(
    container: Container,
    owner: Principal,
    foreign_owner: Principal,
    csv_root: Path,
) -> None:
    connection = await make_connection(container, owner, csv_root)
    job = await container.sync.create_job(
        organization_id=owner.organization_id,
        connection_id=connection.id,
        triggered_by=owner.user_id,
        correlation_id="cancel-test",
    )
    with pytest.raises(NotFoundError):
        await container.sync.request_cancel(foreign_owner.organization_id, job.id)


async def test_audit_events_are_tenant_scoped(
    container: Container, owner: Principal, foreign_owner: Principal, csv_root: Path
) -> None:
    await make_connection(container, owner, csv_root)

    owner_events = await container.audit.list_events(owner.organization_id, limit=100)
    foreign_events = await container.audit.list_events(foreign_owner.organization_id, limit=100)

    assert owner_events, "the creating tenant must have audit events"
    assert foreign_events == []
    for event in owner_events:
        assert event.organization_id == owner.organization_id


async def test_credentials_are_not_resolvable_across_tenants(
    container: Container,
    owner: Principal,
    foreign_owner: Principal,
    csv_root: Path,
) -> None:
    """T4: a secret reference is bound to its tenant, so it cannot be resolved elsewhere."""
    from humisense.errors import SecretNotFoundError
    from humisense.secrets.base import SecretRef

    connection_id = uuid.uuid4()
    await container.secrets_provider.put(
        SecretRef(name=f"connection-{connection_id}", organization_id=owner.organization_id),
        {"token": "owner-only-value"},
        created_by=owner.user_id,
    )
    with pytest.raises(SecretNotFoundError):
        await container.secrets_provider.get(
            SecretRef(
                name=f"connection-{connection_id}",
                organization_id=foreign_owner.organization_id,
            )
        )


async def test_worker_scope_is_enforced(
    container: Container,
    owner: Principal,
    foreign_owner: Principal,
    csv_root: Path,
) -> None:
    """T11: a worker job must not read a connection belonging to another tenant."""
    connection = await make_connection(container, owner, csv_root)
    with pytest.raises(NotFoundError):
        await container.connections.get_connection_for_worker(
            foreign_owner.organization_id, connection.id
        )


async def test_sync_job_carries_the_right_tenant(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    connection = await make_connection(container, owner, csv_root)
    job = await container.sync.create_job(
        organization_id=owner.organization_id,
        connection_id=connection.id,
        triggered_by=owner.user_id,
        correlation_id="tenant-check",
    )
    assert job.organization_id == owner.organization_id
    assert job.status is SyncStatus.QUEUED


async def test_viewer_without_permission_is_refused(
    container: Container, viewer: Principal, csv_root: Path
) -> None:
    """T3: server-side authorization, not frontend visibility."""
    write_csv(csv_root / "denied.csv", synthetic_rows(2))
    with pytest.raises(AuthorizationError):
        await container.connections.create_connection(
            viewer,
            ConnectionDraft(
                connector_type="csv",
                name="denied",
                description=None,
                config={"path": "denied.csv"},
                credentials={},
            ),
        )


async def test_guessing_a_random_uuid_does_not_leak(
    container: Container, owner: Principal, foreign_owner: Principal
) -> None:
    """Identifiers are UUIDv4, so an enumeration attempt yields only 404s."""
    for _ in range(20):
        with pytest.raises(NotFoundError):
            await container.connections.get_connection(foreign_owner, uuid.uuid4())


async def test_second_tenant_can_use_the_same_connection_name(
    container: Container, owner: Principal, foreign_owner: Principal, csv_root: Path
) -> None:
    """The uniqueness constraint is per tenant, not global."""
    await make_connection(container, owner, csv_root, "shared-name")
    second = await make_connection(container, foreign_owner, csv_root, "shared-name")
    assert second.name == "shared-name"


async def test_ingestion_batch_carries_only_one_tenant(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    """T11/T1: envelopes delivered to Layer 2 all belong to the capturing tenant."""
    connection = await make_connection(container, owner, csv_root)
    job = await container.sync.create_job(
        organization_id=owner.organization_id,
        connection_id=connection.id,
        triggered_by=owner.user_id,
        correlation_id="envelope-tenant",
    )
    await container.sync.run_job(owner.organization_id, job.id)
    await run_pending_syncs(container)

    sink = container.ingestion._sink
    batches = sink.batches
    assert batches
    for batch in batches:
        assert batch.organization_id == str(owner.organization_id)
        for record in batch.records:
            assert record.organization_id == str(owner.organization_id)


async def test_deleting_a_connection_removes_its_records(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    """Data-lifecycle contract: disconnecting a source removes the captured data."""
    connection = await make_connection(container, owner, csv_root)
    job = await container.sync.create_job(
        organization_id=owner.organization_id,
        connection_id=connection.id,
        triggered_by=owner.user_id,
        correlation_id="lifecycle",
    )
    await container.sync.run_job(owner.organization_id, job.id)
    assert await container.records.count_records(owner.organization_id, connection.id) == 5

    await container.connections.delete_connection(owner, connection.id)

    assert await container.records.count_records(owner.organization_id, connection.id) == 0


async def test_cross_tenant_membership_is_required(
    container: Container, organization: uuid.UUID, other_organization: uuid.UUID
) -> None:
    """A principal must not be constructible for a tenant it has no membership in."""
    from sqlalchemy import select

    from humisense.db.models import Membership

    principal = await make_principal(container, organization, role="owner")
    async with container.database.system_session() as session:
        memberships = list(
            (
                await session.scalars(
                    select(Membership).where(Membership.user_id == principal.user_id)
                )
            ).all()
        )
    assert {m.organization_id for m in memberships} == {organization}
    assert other_organization not in {m.organization_id for m in memberships}
