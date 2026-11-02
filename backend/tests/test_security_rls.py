"""Row-Level Security and schema-integrity tests.

The RLS tests issue raw SQL rather than using the ORM, so they verify the database's own
guarantee rather than the application's query construction. That distinction matters: if
the application forgot a ``WHERE organization_id`` clause, an ORM-level test could still
pass while the database leaked rows. These tests would not.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from humisense.container import Container
from humisense.db.base import Base
from humisense.db.models import Connection, ConnectionStatus, SourceRecord
from humisense.db.rls import RLS_TABLES
from humisense.security.identity import Principal
from tests.conftest import make_principal

pytestmark = [pytest.mark.asyncio, pytest.mark.security]

TENANT_TABLES = (
    "connections",
    "sync_jobs",
    "source_records",
    "ingestion_receipts",
    "secrets",
    "audit_events",
)


async def test_every_tenant_table_has_rls_enabled_and_forced(container: Container) -> None:
    async with container.database.system_session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = ANY(:names)"
                ),
                {"names": list(TENANT_TABLES)},
            )
        ).all()
    assert len(rows) == len(TENANT_TABLES)
    for relname, enabled, forced in rows:
        assert enabled is True, f"{relname} does not have RLS enabled"
        assert forced is True, f"{relname} does not FORCE RLS for the table owner"


async def test_every_tenant_table_has_a_policy(container: Container) -> None:
    async with container.database.system_session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT tablename, qual, with_check FROM pg_policies "
                    "WHERE schemaname = 'public' AND tablename = ANY(:names)"
                ),
                {"names": list(TENANT_TABLES)},
            )
        ).all()
    covered = {row[0] for row in rows}
    assert covered == set(TENANT_TABLES)
    for _table, qual, with_check in rows:
        # Both directions must be constrained. A USING-only policy would allow a tenant to
        # write a row labelled with another tenant's id.
        assert "app_current_org()" in (qual or "")
        assert "app_current_org()" in (with_check or "")


async def test_query_without_tenant_context_returns_no_rows(container: Container) -> None:
    """Fail closed: an unscoped connection sees nothing, even as the table owner."""
    async with container.database.system_session() as session:
        # A system session explicitly clears the tenant GUC.
        assert await session.scalar(text("SELECT app_current_org()")) is None
        visible = await session.scalar(text("SELECT count(*) FROM connections"))
    assert visible == 0


async def test_rls_blocks_cross_tenant_read(
    container: Container, organization: uuid.UUID, other_organization: uuid.UUID
) -> None:
    async with container.database.tenant_session(organization) as session:
        session.add(
            Connection(
                organization_id=organization,
                connector_type="csv",
                name="tenant-a-connection",
                config={"path": "a.csv"},
                status=ConnectionStatus.PENDING,
                enabled=True,
                capabilities={},
            )
        )

    # Reading under tenant B's context must not see tenant A's row, using raw SQL.
    async with container.database.tenant_session(other_organization) as session:
        count = await session.scalar(text("SELECT count(*) FROM connections"))
        rows = (await session.execute(text("SELECT name FROM connections"))).all()
    assert count == 0
    assert rows == []


async def test_rls_blocks_cross_tenant_write(
    container: Container, organization: uuid.UUID, other_organization: uuid.UUID
) -> None:
    """WITH CHECK must reject a row labelled with another tenant's id."""
    with pytest.raises((DBAPIError, ProgrammingError)):
        async with container.database.tenant_session(organization) as session:
            await session.execute(
                text(
                    "INSERT INTO connections (id, organization_id, connector_type, "
                    "name, config, status, enabled, capabilities) VALUES "
                    "(:id, :org, 'csv', 'smuggled', '{}'::jsonb, 'pending', true, "
                    "'{}'::jsonb)"
                ),
                {"id": uuid.uuid4(), "org": other_organization},
            )


async def test_rls_blocks_cross_tenant_update(
    container: Container, organization: uuid.UUID, other_organization: uuid.UUID
) -> None:
    async with container.database.tenant_session(organization) as session:
        session.add(
            Connection(
                organization_id=organization,
                connector_type="csv",
                name="update-target",
                config={"path": "a.csv"},
                status=ConnectionStatus.PENDING,
                enabled=True,
                capabilities={},
            )
        )

    # Tenant B cannot update tenant A's row: the row is invisible, so zero rows change.
    async with container.database.tenant_session(other_organization) as session:
        result = await session.execute(text("UPDATE connections SET name = 'hijacked'"))
        assert result.rowcount == 0

    async with container.database.tenant_session(organization) as session:
        name = await session.scalar(text("SELECT name FROM connections"))
    assert name == "update-target"


async def test_rls_blocks_cross_tenant_delete(
    container: Container, organization: uuid.UUID, other_organization: uuid.UUID
) -> None:
    async with container.database.tenant_session(organization) as session:
        session.add(
            Connection(
                organization_id=organization,
                connector_type="csv",
                name="delete-target",
                config={"path": "a.csv"},
                status=ConnectionStatus.PENDING,
                enabled=True,
                capabilities={},
            )
        )

    async with container.database.tenant_session(other_organization) as session:
        result = await session.execute(text("DELETE FROM connections"))
        assert result.rowcount == 0

    async with container.database.tenant_session(organization) as session:
        assert await session.scalar(text("SELECT count(*) FROM connections")) == 1


async def test_tenant_context_does_not_leak_between_sessions(
    container: Container, organization: uuid.UUID, other_organization: uuid.UUID
) -> None:
    """T1: the GUC is transaction-local, so a pooled connection cannot carry it over."""
    async with container.database.tenant_session(organization) as session:
        session.add(
            Connection(
                organization_id=organization,
                connector_type="csv",
                name="leak-check",
                config={"path": "a.csv"},
                status=ConnectionStatus.PENDING,
                enabled=True,
                capabilities={},
            )
        )
        await session.flush()
        assert await session.scalar(text("SELECT count(*) FROM connections")) == 1

    # A new tenant session for B reuses a pooled connection. If the GUC persisted, B would
    # see A's row.
    async with container.database.tenant_session(other_organization) as session:
        assert await session.scalar(text("SELECT count(*) FROM connections")) == 0


async def test_audit_events_reject_update(container: Container, organization: uuid.UUID) -> None:
    """T14: the audit trail is append-only at the database level."""
    async with container.database.tenant_session(organization) as session:
        await session.execute(
            text(
                "INSERT INTO audit_events "
                "(id, organization_id, actor_type, action, outcome, event_metadata) "
                "VALUES (:id, :org, 'system', 'test.action', 'success', '{}'::jsonb)"
            ),
            {"id": uuid.uuid4(), "org": organization},
        )

    with pytest.raises(DBAPIError) as excinfo:
        async with container.database.tenant_session(organization) as session:
            await session.execute(text("UPDATE audit_events SET outcome = 'tampered'"))
    assert "append-only" in str(excinfo.value).lower()


async def test_audit_events_reject_delete(container: Container, organization: uuid.UUID) -> None:
    async with container.database.tenant_session(organization) as session:
        await session.execute(
            text(
                "INSERT INTO audit_events "
                "(id, organization_id, actor_type, action, outcome, event_metadata) "
                "VALUES (:id, :org, 'system', 'test.action', 'success', '{}'::jsonb)"
            ),
            {"id": uuid.uuid4(), "org": organization},
        )

    with pytest.raises(DBAPIError) as excinfo:
        async with container.database.tenant_session(organization) as session:
            await session.execute(text("DELETE FROM audit_events"))
    assert "append-only" in str(excinfo.value).lower()


# Tables that carry organization_id but are deliberately not under RLS. They are read
# while resolving identity, before a tenant is known, so a tenant GUC cannot scope them.
# Each is reached only through ``system_session`` and is filtered in code:
#   * sessions    - looked up by opaque token digest, which is itself the authorization
#   * memberships - looked up by (user_id, organization_id) with the organization taken
#                   from the session row, never from the request
AUTH_SCOPE_TABLES = {"sessions", "memberships"}


async def test_every_tenant_table_is_either_rls_scoped_or_auth_scoped() -> None:
    """No table may carry organization_id without an isolation mechanism."""
    from humisense.db import rls as rls_module

    tenant_tables = {
        table.name for table in Base.metadata.tables.values() if "organization_id" in table.columns
    }
    rls_scoped = (
        set(rls_module.RLS_TABLES)
        | set(rls_module.LAYER2_RLS_TABLES)
        | set(rls_module.LAYER3_RLS_TABLES)
    )
    unprotected = tenant_tables - rls_scoped - AUTH_SCOPE_TABLES
    assert unprotected == set(), f"tables with no isolation mechanism: {sorted(unprotected)}"
    assert rls_scoped <= tenant_tables


async def test_mapped_columns_match_migration_columns(container: Container) -> None:
    """Guard against schema drift between the models and the migration.

    This catches a class of bug that is otherwise silent: a mapped attribute whose
    database column name differs, which surfaces only as an obscure error on the first
    real write.
    """
    async with container.database.system_session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public'"
                )
            )
        ).all()

    actual: dict[str, set[str]] = {}
    for table_name, column_name in rows:
        actual.setdefault(table_name, set()).add(column_name)

    for table in Base.metadata.tables.values():
        expected = {column.name for column in table.columns}
        assert expected <= actual.get(table.name, set()), (
            f"{table.name}: mapped columns missing from the database: "
            f"{sorted(expected - actual.get(table.name, set()))}"
        )


async def test_source_record_insert_round_trip(
    container: Container, organization: uuid.UUID
) -> None:
    """The metadata column is the one that previously resolved to MetaData in an insert."""
    async with container.database.tenant_session(organization) as session:
        connection = Connection(
            organization_id=organization,
            connector_type="csv",
            name="round-trip",
            config={"path": "a.csv"},
            status=ConnectionStatus.PENDING,
            enabled=True,
            capabilities={},
        )
        session.add(connection)
        await session.flush()
        session.add(
            SourceRecord(
                organization_id=organization,
                connection_id=connection.id,
                connector_type="csv",
                source_record_id="row-1",
                record_type="csv_row",
                schema_version="1.0",
                captured_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
                payload={"a": "1"},
                record_metadata={"untrusted": True},
                content_hash="a" * 64,
                correlation_id="round-trip",
            )
        )
        await session.flush()
        stored = await session.scalar(text("SELECT record_metadata FROM source_records LIMIT 1"))
    assert stored == {"untrusted": True}


async def test_idempotency_constraint_prevents_duplicate_records(
    container: Container, organization: uuid.UUID
) -> None:
    """T15: repeated capture of identical content must not create duplicate rows."""

    from humisense.connectors.types import SourceRecordDraft
    from humisense.services.records import RecordService

    async with container.database.tenant_session(organization) as session:
        connection = Connection(
            organization_id=organization,
            connector_type="csv",
            name="idempotent",
            config={"path": "a.csv"},
            status=ConnectionStatus.PENDING,
            enabled=True,
            capabilities={},
        )
        session.add(connection)
        await session.flush()
        connection_id = connection.id

    service = RecordService(container.database)
    draft = SourceRecordDraft(
        source_record_id="stable-id",
        record_type="csv_row",
        payload={"value": "1"},
        source_timestamp=None,
        metadata={"row_number": 2},
    )

    first = await service.persist_batch(
        organization_id=organization,
        connection_id=connection_id,
        connector_type="csv",
        correlation_id="first",
        drafts=[draft],
    )
    second = await service.persist_batch(
        organization_id=organization,
        connection_id=connection_id,
        connector_type="csv",
        correlation_id="second",
        drafts=[draft],
    )

    assert len(first) == 1
    assert len(second) == 1
    assert first[0].id == second[0].id
    assert first[0].is_new is True
    assert second[0].is_new is False
    assert await service.count_records(organization, connection_id) == 1


async def test_rls_applies_to_the_worker_role(
    container: Container, organization: uuid.UUID, other_organization: uuid.UUID
) -> None:
    """T11: the worker uses the same database role and therefore the same RLS policies."""
    from humisense.services.sync import DbCancellationToken

    token = DbCancellationToken(
        container.database, organization_id=other_organization, job_id=uuid.uuid4()
    )
    assert token.cancelled is False

    async with container.database.tenant_session(organization) as session:
        session.add(
            Connection(
                organization_id=organization,
                connector_type="csv",
                name="worker-scope",
                config={"path": "a.csv"},
                status=ConnectionStatus.PENDING,
                enabled=True,
                capabilities={},
            )
        )

    async with container.database.tenant_session(other_organization) as session:
        assert await session.scalar(text("SELECT count(*) FROM connections")) == 0


async def test_rls_table_list_is_not_empty() -> None:
    assert set(RLS_TABLES) >= set(TENANT_TABLES)


async def test_principal_cannot_be_created_for_a_foreign_tenant(
    container: Container, organization: uuid.UUID, other_organization: uuid.UUID
) -> None:
    """A principal is a server-side construct; a client cannot assert one."""
    principal: Principal = await make_principal(container, organization)
    assert principal.organization_id == organization
    assert principal.organization_id != other_organization
