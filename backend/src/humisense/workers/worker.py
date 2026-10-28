"""arq worker entry point.

Run with::

    uv run arq humisense.workers.worker.WorkerSettings

The worker holds no tenant context between jobs. Each job re-establishes tenant scope
inside its own transactions, so a job for one tenant cannot observe another tenant's data
even if the worker process is long-lived and processes many tenants sequentially.
"""

from __future__ import annotations

import uuid
from typing import Any, ClassVar

import structlog

from humisense.config import get_settings
from humisense.container import build_container
from humisense.logging_setup import configure_logging
from humisense.observability.correlation import set_correlation_id

logger = structlog.get_logger(__name__)


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(json_logs=settings.log_json, level=settings.log_level)
    container = build_container(settings)
    ctx["container"] = container
    logger.info("worker_started", queue=container.task_queue.name)


async def shutdown(ctx: dict[str, Any]) -> None:
    container = ctx.get("container")
    if container is not None:
        await container.close()
    logger.info("worker_stopped")


async def run_sync_job(
    ctx: dict[str, Any],
    organization_id: str,
    job_id: str,
    correlation_id: str = "",
) -> dict[str, Any]:
    """Execute one synchronization job.

    The payload is re-validated: a malformed identifier is rejected rather than used, so a
    corrupted queue entry cannot become a path into the database.
    """
    set_correlation_id(correlation_id or job_id)
    container = ctx["container"]

    try:
        organization_uuid = uuid.UUID(organization_id)
        job_uuid = uuid.UUID(job_id)
    except (ValueError, AttributeError, TypeError):
        logger.error("worker_invalid_payload", organization_id=organization_id, job_id=job_id)
        return {"status": "rejected", "reason": "invalid_identifiers"}

    outcome = await container.sync.run_job(organization_uuid, job_uuid)

    if outcome.retryable and outcome.status.value == "failed":
        # Let arq schedule a retry; the checkpoint was not advanced, so the run resumes.
        from humisense.errors import ConnectorSourceError

        logger.warning(
            "sync_retryable_failure",
            job_id=job_id,
            error_code=outcome.error_code,
            correlation_id=correlation_id,
        )
        raise ConnectorSourceError(
            "The source system failed; the job will be retried.",
            detail=f"retryable sync failure code={outcome.error_code}",
        )

    return {
        "status": outcome.status.value,
        "records_fetched": outcome.records_fetched,
        "records_persisted": outcome.records_persisted,
        "records_delivered": outcome.records_delivered,
        "records_failed": outcome.records_failed,
        "error_code": outcome.error_code,
    }


async def reap_stale_jobs_task(ctx: dict[str, Any]) -> int:
    """Periodic maintenance: reclaim jobs abandoned by a killed worker."""
    from humisense.services.sync import reap_stale_jobs

    container = ctx["container"]
    reaped = await reap_stale_jobs(container.database, older_than_seconds=3600)
    if reaped:
        logger.warning("stale_jobs_reaped", count=reaped)
    return reaped


class WorkerSettings:
    """arq worker configuration."""

    functions: ClassVar[list[Any]] = [run_sync_job, reap_stale_jobs_task]
    on_startup = startup
    on_shutdown = shutdown

    # A synchronization is a bounded unit of work. The timeout is generous but finite so
    # a wedged connector cannot hold a worker slot forever.
    job_timeout = 900
    max_tries = 3
    keep_result = 3600
    health_check_interval = 30

    @staticmethod
    def _settings() -> Any:
        return get_settings()

    def __init__(self) -> None:
        settings = get_settings()
        from arq.connections import RedisSettings

        self.redis_settings = RedisSettings.from_dsn(settings.redis_url)
        from arq.cron import cron

        # Instance attributes, not ClassVars: arq reads them off the instance.
        self.cron_jobs = [cron(reap_stale_jobs_task, minute={0, 30}, run_at_startup=False)]
