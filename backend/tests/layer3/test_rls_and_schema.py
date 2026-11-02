"""Layer 3 tenant-isolation and schema-integrity tests.

Like the Layer 1/2 RLS tests, these issue raw SQL so they verify the database's own
guarantee rather than the application's query construction. A forgotten
``WHERE organization_id`` clause would still pass an ORM-level test; these would not.

The Layer 3 tables are the analytical record of the platform: features, baselines, rules,
models, forecasts, and detections. A cross-tenant leak here would expose one tenant's
operational intelligence to another, so the same RLS treatment as Layers 1 and 2 is
required and asserted.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from humisense.container import Container
from humisense.db.rls import LAYER3_RLS_TABLES

pytestmark = [pytest.mark.asyncio, pytest.mark.security]


async def test_every_layer3_table_has_rls_enabled_and_forced(container: Container) -> None:
    async with container.database.system_session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname = ANY(:names)"
                ),
                {"names": list(LAYER3_RLS_TABLES)},
            )
        ).all()
    assert len(rows) == len(LAYER3_RLS_TABLES)
    for relname, enabled, forced in rows:
        assert enabled is True, f"{relname} does not have RLS enabled"
        assert forced is True, f"{relname} does not FORCE RLS for the table owner"


async def test_every_layer3_table_has_a_bidirectional_policy(container: Container) -> None:
    async with container.database.system_session() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT tablename, qual, with_check FROM pg_policies "
                    "WHERE schemaname = 'public' AND tablename = ANY(:names)"
                ),
                {"names": list(LAYER3_RLS_TABLES)},
            )
        ).all()
    covered = {row[0] for row in rows}
    assert covered == set(LAYER3_RLS_TABLES)
    for _table, qual, with_check in rows:
        assert "app_current_org()" in (qual or "")
        assert "app_current_org()" in (with_check or "")


async def test_detection_events_are_isolated_between_tenants(
    container: Container, organization: uuid.UUID, other_organization: uuid.UUID
) -> None:
    detection_id = uuid.uuid4()
    async with container.database.tenant_session(organization) as session:
        await session.execute(
            text(
                "INSERT INTO detection_events "
                "(id, organization_id, detection_id, domain, entity_id, metric, "
                "detection_category, detection_type, severity, detected_at, algorithm_name, "
                "algorithm_version, schema_version, correlation_id) VALUES "
                "(:id, :org, :det, 'industrial', 'LINE-01', 'temperature', 'point_anomaly', "
                "'z_score', 'high', now(), 'z_score', '1.0.0', '3.0', 'corr')"
            ),
            {"id": uuid.uuid4(), "org": organization, "det": detection_id},
        )

    async with container.database.tenant_session(other_organization) as session:
        count = await session.scalar(text("SELECT count(*) FROM detection_events"))
    assert count == 0

    async with container.database.tenant_session(organization) as session:
        count = await session.scalar(text("SELECT count(*) FROM detection_events"))
    assert count == 1


async def test_layer3_write_is_rejected_for_a_foreign_tenant(
    container: Container, organization: uuid.UUID, other_organization: uuid.UUID
) -> None:
    """WITH CHECK must reject a detection labelled with another tenant's id."""
    with pytest.raises((DBAPIError, ProgrammingError)):
        async with container.database.tenant_session(organization) as session:
            await session.execute(
                text(
                    "INSERT INTO detection_events "
                    "(id, organization_id, detection_id, domain, entity_id, metric, "
                    "detection_category, detection_type, severity, detected_at, algorithm_name, "
                    "algorithm_version, schema_version, correlation_id) VALUES "
                    "(:id, :org, :det, 'industrial', 'LINE-01', 'temperature', 'point_anomaly', "
                    "'z_score', 'high', now(), 'z_score', '1.0.0', '3.0', 'corr')"
                ),
                {"id": uuid.uuid4(), "org": other_organization, "det": uuid.uuid4()},
            )


async def test_analytics_execution_log_rejects_update(
    container: Container, organization: uuid.UUID
) -> None:
    """The run ledger is append-only at the database level."""
    async with container.database.tenant_session(organization) as session:
        await session.execute(
            text(
                "INSERT INTO analytics_execution_logs "
                "(id, organization_id, stage, status, correlation_id) "
                "VALUES (:id, :org, 'quality', 'succeeded', 'corr')"
            ),
            {"id": uuid.uuid4(), "org": organization},
        )

    with pytest.raises(DBAPIError) as excinfo:
        async with container.database.tenant_session(organization) as session:
            await session.execute(text("UPDATE analytics_execution_logs SET status = 'tampered'"))
    assert "append-only" in str(excinfo.value).lower()


async def test_detection_event_history_rejects_delete(
    container: Container, organization: uuid.UUID
) -> None:
    """A detection's lifecycle history cannot be erased to hide a suppression."""
    async with container.database.tenant_session(organization) as session:
        await session.execute(
            text(
                "INSERT INTO detection_event_history "
                "(id, organization_id, detection_id, to_status, correlation_id) "
                "VALUES (:id, :org, :det, 'open', 'corr')"
            ),
            {"id": uuid.uuid4(), "org": organization, "det": uuid.uuid4()},
        )

    with pytest.raises(DBAPIError) as excinfo:
        async with container.database.tenant_session(organization) as session:
            await session.execute(text("DELETE FROM detection_event_history"))
    assert "append-only" in str(excinfo.value).lower()


async def test_baseline_versions_reject_update(
    container: Container, organization: uuid.UUID
) -> None:
    """A baseline version is immutable; a change is a new version, not an edit."""
    baseline_id = uuid.uuid4()
    async with container.database.tenant_session(organization) as session:
        await session.execute(
            text(
                "INSERT INTO baseline_definitions "
                "(id, organization_id, entity_id, metric, baseline_type) "
                "VALUES (:id, :org, 'LINE-01', 'temperature', 'rolling')"
            ),
            {"id": baseline_id, "org": organization},
        )
        await session.execute(
            text(
                "INSERT INTO baseline_versions "
                "(id, organization_id, baseline_id, version, mean, sample_count) "
                "VALUES (:id, :org, :bid, 1, 60.0, 100)"
            ),
            {"id": uuid.uuid4(), "org": organization, "bid": baseline_id},
        )

    with pytest.raises(DBAPIError) as excinfo:
        async with container.database.tenant_session(organization) as session:
            await session.execute(text("UPDATE baseline_versions SET mean = 999.0"))
    assert "immutable" in str(excinfo.value).lower()
