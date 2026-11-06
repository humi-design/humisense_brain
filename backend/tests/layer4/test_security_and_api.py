"""Layer 4 tenant-isolation, immutability, and HTTP-surface tests.

The RLS tests issue raw SQL so they verify the database's own guarantee rather than the
application's query construction. The HTTP tests drive the assembled ASGI application, so the
middleware, dependency graph, and permission checks are all in the path.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from humisense.container import Container
from humisense.db.rls import LAYER4_RLS_TABLES
from tests.conftest import make_principal, organization_slug, sign_in

pytestmark = [pytest.mark.asyncio]


# --- RLS ------------------------------------------------------------------------


@pytest.mark.security
async def test_every_layer4_table_has_rls_enabled_and_forced(container: Container) -> None:
    async with container.database.system_session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = ANY(:names)"
                ),
                {"names": list(LAYER4_RLS_TABLES)},
            )
        ).all()
    assert len(rows) == len(LAYER4_RLS_TABLES)
    for relname, enabled, forced in rows:
        assert enabled is True, f"{relname} does not have RLS enabled"
        assert forced is True, f"{relname} does not FORCE RLS for the table owner"


@pytest.mark.security
async def test_every_layer4_table_has_a_bidirectional_policy(container: Container) -> None:
    async with container.database.system_session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT tablename, qual, with_check FROM pg_policies "
                    "WHERE schemaname = 'public' AND tablename = ANY(:names)"
                ),
                {"names": list(LAYER4_RLS_TABLES)},
            )
        ).all()
    covered = {row[0] for row in rows}
    assert covered == set(LAYER4_RLS_TABLES)
    for _table, qual, with_check in rows:
        assert "app_current_org()" in (qual or "")
        assert "app_current_org()" in (with_check or "")


@pytest.mark.security
async def test_work_entities_are_isolated_between_tenants(
    container: Container, organization: uuid.UUID, other_organization: uuid.UUID
) -> None:
    async with container.database.tenant_session(organization) as session:
        await session.execute(
            text(
                "INSERT INTO work_entities (id, organization_id, work_key, work_type, title, "
                "domain, priority, priority_source, status, origin, attributes, "
                "correlation_id, schema_version, created_at, updated_at) VALUES "
                "(gen_random_uuid(), :org, 'task:erp:T1', 'task', 'Tenant A work', 'general', "
                "'normal', 'source', 'open', 'explicit', '{}', 'c', '4.0', now(), now())"
            ),
            {"org": organization},
        )
    async with container.database.tenant_session(other_organization) as session:
        count = await session.scalar(text("SELECT count(*) FROM work_entities"))
    assert count == 0


@pytest.mark.security
async def test_layer4_write_is_rejected_for_a_foreign_tenant(
    container: Container, organization: uuid.UUID, other_organization: uuid.UUID
) -> None:
    """WITH CHECK must reject a work row labelled with another tenant's id."""
    with pytest.raises((DBAPIError, ProgrammingError)):
        async with container.database.tenant_session(organization) as session:
            await session.execute(
                text(
                    "INSERT INTO work_entities (id, organization_id, work_key, work_type, title, "
                    "domain, priority, priority_source, status, origin, attributes, "
                    "correlation_id, schema_version, created_at, updated_at) VALUES "
                    "(gen_random_uuid(), :org, 'task:erp:T1', 'task', 'foreign', 'general', "
                    "'normal', 'source', 'open', 'explicit', '{}', 'c', '4.0', now(), now())"
                ),
                {"org": other_organization},
            )


@pytest.mark.security
async def test_work_timeline_rejects_update(container: Container, organization: uuid.UUID) -> None:
    """The timeline is append-only: a status change cannot be rewritten to hide it."""
    work_id = uuid.uuid4()
    async with container.database.tenant_session(organization) as session:
        await session.execute(
            text(
                "INSERT INTO work_entities (id, organization_id, work_key, work_type, title, "
                "domain, priority, priority_source, status, origin, attributes, "
                "correlation_id, schema_version, created_at, updated_at) VALUES "
                "(:id, :org, 'task:erp:T1', 'task', 'x', 'general', 'normal', 'source', 'open', "
                "'explicit', '{}', 'c', '4.0', now(), now())"
            ),
            {"id": work_id, "org": organization},
        )
        await session.execute(
            text(
                "INSERT INTO work_timeline_events (id, organization_id, work_id, event_type, "
                "event_at, ingested_at, processed_at, actor_type, provenance, correlation_id) "
                "VALUES (gen_random_uuid(), :org, :work, 'work.created', now(), now(), now(), "
                "'system', 'explicit', 'c')"
            ),
            {"org": organization, "work": work_id},
        )
    with pytest.raises(DBAPIError) as excinfo:
        async with container.database.tenant_session(organization) as session:
            await session.execute(text("UPDATE work_timeline_events SET event_type = 'tampered'"))
    assert "append-only" in str(excinfo.value).lower()


@pytest.mark.security
async def test_work_status_history_rejects_delete(
    container: Container, organization: uuid.UUID
) -> None:
    work_id = uuid.uuid4()
    async with container.database.tenant_session(organization) as session:
        await session.execute(
            text(
                "INSERT INTO work_entities (id, organization_id, work_key, work_type, title, "
                "domain, priority, priority_source, status, origin, attributes, "
                "correlation_id, schema_version, created_at, updated_at) VALUES "
                "(:id, :org, 'task:erp:T1', 'task', 'x', 'general', 'normal', 'source', 'open', "
                "'explicit', '{}', 'c', '4.0', now(), now())"
            ),
            {"id": work_id, "org": organization},
        )
        await session.execute(
            text(
                "INSERT INTO work_status_history (id, organization_id, work_id, to_status, "
                "correlation_id) VALUES (gen_random_uuid(), :org, :work, 'open', 'c')"
            ),
            {"org": organization, "work": work_id},
        )
    with pytest.raises(DBAPIError) as excinfo:
        async with container.database.tenant_session(organization) as session:
            await session.execute(text("DELETE FROM work_status_history"))
    assert "append-only" in str(excinfo.value).lower()


@pytest.mark.security
async def test_workflow_definitions_reject_update(
    container: Container, organization: uuid.UUID
) -> None:
    """A workflow definition is versioned, not edited: a change is a new version row."""
    async with container.database.tenant_session(organization) as session:
        await session.execute(
            text(
                "INSERT INTO workflow_definitions (id, organization_id, workflow_key, name, "
                "workflow_type, version, steps, entry_conditions, exit_conditions, "
                "correlation_id) VALUES (gen_random_uuid(), :org, 'wf', 'WF', 'generic', 1, "
                "'[]', '{}', '{}', 'c')"
            ),
            {"org": organization},
        )
    with pytest.raises(DBAPIError) as excinfo:
        async with container.database.tenant_session(organization) as session:
            await session.execute(text("UPDATE workflow_definitions SET name = 'tampered'"))
    assert "immutable" in str(excinfo.value).lower()


# --- HTTP surface ---------------------------------------------------------------


async def _signed_in_client(
    app_client: Any, container: Container, organization: uuid.UUID, *, role: str = "owner"
) -> dict[str, str]:
    subject = f"work-{role}-{uuid.uuid4().hex[:8]}"
    await make_principal(container, organization, role=role, subject=subject)
    payload = await sign_in(
        app_client,
        subject=subject,
        organization_slug=await organization_slug(container, organization),
    )
    return {"X-CSRF-Token": payload["csrf_token"]}


async def _seed_work(container: Container, organization: uuid.UUID, *, title: str) -> uuid.UUID:
    work_id = uuid.uuid4()
    async with container.database.tenant_session(organization) as session:
        await session.execute(
            text(
                "INSERT INTO work_entities (id, organization_id, work_key, work_type, title, "
                "domain, priority, priority_source, status, origin, attributes, "
                "correlation_id, schema_version, created_at, updated_at) VALUES "
                "(:id, :org, :key, 'task', :title, 'general', 'normal', 'source', 'open', "
                "'explicit', '{}', 'c', '4.0', now(), now())"
            ),
            {"id": work_id, "org": organization, "key": f"task:erp:{work_id}", "title": title},
        )
    return work_id


@pytest.mark.security
async def test_anonymous_cannot_read_work(app_client: Any) -> None:
    assert (await app_client.get("/api/v1/work")).status_code == 401


@pytest.mark.security
async def test_anonymous_cannot_change_status(app_client: Any) -> None:
    response = await app_client.post(
        f"/api/v1/work/{uuid.uuid4()}/status", json={"status": "completed"}
    )
    assert response.status_code == 401


async def test_viewer_can_read_but_not_change_work(
    app_client: Any, container: Container, organization: uuid.UUID
) -> None:
    headers = await _signed_in_client(app_client, container, organization, role="viewer")
    work_id = await _seed_work(container, organization, title="Read-only task")
    listing = await app_client.get("/api/v1/work")
    assert listing.status_code == 200
    assert listing.json()["total"] == 1

    denied = await app_client.post(
        f"/api/v1/work/{work_id}/status", json={"status": "completed"}, headers=headers
    )
    assert denied.status_code == 403


async def test_operator_can_change_status(
    app_client: Any, container: Container, organization: uuid.UUID
) -> None:
    headers = await _signed_in_client(app_client, container, organization, role="operator")
    work_id = await _seed_work(container, organization, title="Task")
    response = await app_client.post(
        f"/api/v1/work/{work_id}/status", json={"status": "in_progress"}, headers=headers
    )
    assert response.status_code == 204, response.text

    detail = await app_client.get(f"/api/v1/work/{work_id}")
    assert detail.json()["status"] == "in_progress"


async def test_illegal_transition_is_refused(
    app_client: Any, container: Container, organization: uuid.UUID
) -> None:
    headers = await _signed_in_client(app_client, container, organization, role="operator")
    work_id = await _seed_work(container, organization, title="Task")
    # open -> new is not a legal transition.
    response = await app_client.post(
        f"/api/v1/work/{work_id}/status", json={"status": "new"}, headers=headers
    )
    assert response.status_code == 409


@pytest.mark.security
async def test_a_second_tenant_cannot_read_first_tenants_work(
    app_client: Any,
    container: Container,
    organization: uuid.UUID,
    other_organization: uuid.UUID,
) -> None:
    work_id = await _seed_work(container, organization, title="Tenant A secret")
    await _signed_in_client(app_client, container, other_organization, role="owner")

    listing = await app_client.get("/api/v1/work")
    assert listing.json()["total"] == 0

    detail = await app_client.get(f"/api/v1/work/{work_id}")
    assert detail.status_code == 404


@pytest.mark.security
async def test_status_change_requires_a_csrf_token(
    app_client: Any, container: Container, organization: uuid.UUID
) -> None:
    await _signed_in_client(app_client, container, organization, role="operator")
    work_id = await _seed_work(container, organization, title="Task")
    # No CSRF header: the unsafe request must be refused even with a valid session.
    response = await app_client.post(
        f"/api/v1/work/{work_id}/status", json={"status": "in_progress"}
    )
    assert response.status_code in {403, 400}


async def test_my_work_view_is_available(
    app_client: Any, container: Container, organization: uuid.UUID
) -> None:
    await _signed_in_client(app_client, container, organization, role="viewer")
    await _seed_work(container, organization, title="Task")
    response = await app_client.get("/api/v1/work/my-work")
    assert response.status_code == 200
    body = response.json()
    assert "counts" in body and "my_work" in body


async def test_executive_overview_is_available(
    app_client: Any, container: Container, organization: uuid.UUID
) -> None:
    await _signed_in_client(app_client, container, organization, role="owner")
    response = await app_client.get("/api/v1/work/executive-overview")
    assert response.status_code == 200
    body = response.json()
    assert set(body) >= {"as_of", "critical_work", "overdue_high_priority", "blocked_items"}
