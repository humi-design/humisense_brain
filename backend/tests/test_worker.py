"""Tests for the arq worker entry point.

The worker is the only component that runs synchronization outside a request, so nothing
in the request-path suite exercises its wiring. These tests drive the real job function
against the real container: a renamed container attribute or an unregistered job function
fails here rather than on a queued job in the middle of the night.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from humisense.config import get_settings
from humisense.connectors.base import get_manifest
from humisense.container import build_container
from humisense.services.connections import ConnectionDraft
from humisense.workers import worker as worker_module
from tests.conftest import Container, Principal, synthetic_rows, write_csv

pytestmark = pytest.mark.security


async def _queued_job(
    container: Container, principal: Principal, csv_root: Path, name: str = "worker"
) -> tuple[uuid.UUID, uuid.UUID]:
    """Create a connection and a queued job for it, as the API route would."""
    write_csv(csv_root / f"{name}.csv", synthetic_rows(5))
    connection = await container.connections.create_connection(
        principal,
        ConnectionDraft(
            connector_type="csv",
            name=name,
            description=None,
            config={"path": f"{name}.csv"},
            credentials={},
        ),
    )
    job = await container.sync.create_job(
        organization_id=principal.organization_id,
        connection_id=connection.id,
        triggered_by=principal.user_id,
        correlation_id=str(uuid.uuid4()),
    )
    return principal.organization_id, job.id


async def test_the_worker_job_runs_a_queued_sync(
    container: Container, owner: Principal, csv_root: Path
) -> None:
    """Regression: the job looked up a container attribute that does not exist.

    ``run_sync_job`` referenced ``container.sync_service``, so every queued job raised
    AttributeError. The service-layer tests stayed green because they call
    ``container.sync.run_job`` directly and never touched the worker's own code path.
    """
    organization_id, job_id = await _queued_job(container, owner, csv_root)

    result = await worker_module.run_sync_job(
        {"container": container},
        str(organization_id),
        str(job_id),
        correlation_id="test-correlation",
    )

    assert result["status"] == "succeeded", result
    assert result["records_fetched"] == 5
    assert result["records_persisted"] == 5
    assert result["records_delivered"] == 5
    assert result["error_code"] is None


async def test_the_worker_job_rejects_identifiers_that_are_not_uuids(
    container: Container,
) -> None:
    """A corrupted queue payload is rejected before it reaches a database query."""
    result = await worker_module.run_sync_job(
        {"container": container}, "not-a-uuid", "also-not-a-uuid"
    )

    assert result["status"] == "rejected"
    assert result["reason"] == "invalid_identifiers"


async def test_the_worker_container_exposes_the_services_the_job_uses(
    test_settings: object,
) -> None:
    """Guard against a service renamed on the container while a caller keeps the old name
    — the exact shape of the defect above."""
    built = build_container(test_settings)  # type: ignore[arg-type]
    try:
        assert hasattr(built, "sync"), "run_sync_job calls container.sync.run_job"
        assert callable(built.sync.run_job)
        assert hasattr(built, "database")
    finally:
        await built.close()


def test_the_worker_registers_the_functions_arq_must_find() -> None:
    """arq resolves jobs by name; a function missing from WorkerSettings is never run."""
    names = {getattr(fn, "__name__", str(fn)) for fn in worker_module.WorkerSettings.functions}

    assert "run_sync_job" in names
    assert "reap_stale_jobs_task" in names


def test_the_worker_settings_survive_arq_resolution() -> None:
    """arq reads settings from the class __dict__, so instance attributes are dropped.

    ``arq.worker.get_kwargs`` collects ``settings_cls.__dict__``, which for a class holds
    only class attributes. A ``redis_settings`` assigned in ``__init__`` is invisible to it,
    so the worker silently falls back to arq's default broker (``localhost:6379``) and the
    cron job never runs. This asserts the settings arq actually resolves.
    """
    from arq.worker import get_kwargs

    resolved = get_kwargs(worker_module.WorkerSettings)

    assert "redis_settings" in resolved, "arq would ignore the configured broker"
    assert "cron_jobs" in resolved, "arq would ignore the stale-job reaper"


def test_the_worker_broker_matches_the_configured_redis_url() -> None:
    """The resolved broker must be the configured one, not arq's localhost default."""
    from arq.connections import RedisSettings

    settings = get_settings()
    expected = RedisSettings.from_dsn(settings.redis_url)

    assert worker_module.WorkerSettings.redis_settings == expected


def test_the_csv_connector_advertises_only_implemented_capabilities() -> None:
    """Section 5: a capability flag must correspond to tested behaviour."""
    capabilities = get_manifest("csv").capabilities

    assert capabilities.read_only() is True
    assert capabilities.supports_write is False
    assert capabilities.supports_webhook is False
