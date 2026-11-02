"""Health and readiness endpoints.

Deliberately minimal. Liveness reveals only that the process is up. Readiness reports
whether dependencies are reachable, using a coarse status per dependency. Neither exposes
versions of dependencies, hostnames, connection strings, credentials, or topology.

Both endpoints are unauthenticated, because a load balancer must be able to call them.
They return nothing an attacker can use: no build metadata beyond the application version,
and no error text from a failing dependency.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter
from sqlalchemy import text

from humisense import __version__
from humisense.api.deps import ContainerDep, SettingsDep
from humisense.api.schemas import HealthResponse, ReadinessResponse
from humisense.connectors.base import registry

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health() -> HealthResponse:
    """Report that the process is running and able to serve requests."""
    return HealthResponse(status="ok", version=__version__)


@router.get("/health/ready", response_model=ReadinessResponse, summary="Readiness probe")
async def readiness(container: ContainerDep, settings: SettingsDep) -> ReadinessResponse:
    """Report whether the service's dependencies are usable.

    A degraded dependency yields ``status: degraded`` with HTTP 200, so an orchestrator
    does not restart a healthy process because a downstream system is briefly unavailable.
    Only the database is treated as required for readiness.
    """
    database_status = "ok"
    try:
        async with container.database.system_session() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - any failure means not ready
        database_status = "unavailable"
        logger.warning("readiness_database_unavailable", error_type=type(exc).__name__)

    if settings.task_queue_mode == "inline":
        queue_status = "not_required"
    else:
        queue_status = "ok"
        try:
            from arq.connections import RedisSettings, create_pool

            pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
            await pool.ping()
            await pool.aclose()
        except Exception as exc:  # noqa: BLE001 - queue is a soft dependency for the API
            queue_status = "unavailable"
            logger.warning("readiness_queue_unavailable", error_type=type(exc).__name__)

    limiter_status = "degraded" if container.rate_limiter.degraded else "ok"

    overall = "ready" if database_status == "ok" else "degraded"
    return ReadinessResponse(
        status=overall,
        database=database_status,
        queue=queue_status,
        rate_limiter=limiter_status,
        connectors_loaded=len(registry),
    )
