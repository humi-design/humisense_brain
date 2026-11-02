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
from arq.connections import RedisSettings
from arq.cron import cron

from humisense.config import get_settings
from humisense.container import build_container
from humisense.db.models import JobStatus
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


async def publish_outbox_task(ctx: dict[str, Any]) -> dict[str, int]:
    """Periodic Layer 2 publication: drain the outbox for every tenant.

    Runs on the worker rather than the API so exactly one process owns publication, and the
    loop survives an API restart. Each tenant is drained inside its own tenant-scoped
    transaction, so the sweep cannot cross a tenant boundary.
    """
    container = ctx["container"]
    result = await container.layer2_outbox.publish_all_tenants()
    if result.published or result.failed:
        logger.info(
            "layer2_outbox_swept",
            published=result.published,
            failed=result.failed,
            skipped=result.skipped,
        )
    return {"published": result.published, "failed": result.failed, "skipped": result.skipped}


async def analytics_sweep_task(ctx: dict[str, Any]) -> dict[str, int]:
    """Periodic Layer 3 sweep: run the analytics pipeline for every tenant.

    Disabled unless ``layer3_sweep_enabled`` is set, so a deployment that has not sized its
    windows does not have detection running on a timer it did not ask for. Each tenant is
    run inside its own tenant-scoped sessions, so the sweep cannot cross a tenant boundary,
    and one tenant's failure does not stop the sweep.
    """
    container = ctx["container"]
    if not container.settings.layer3_sweep_enabled:
        return {"tenants": 0, "detections": 0, "skipped": 0}
    results = await container.layer3_engine.run_sweep(correlation_id="layer3-sweep")
    detections = sum(r.detections for r in results)
    skipped = sum(1 for r in results if r.status == JobStatus.SKIPPED)
    logger.info(
        "layer3_sweep_finished",
        tenants=len(results),
        detections=detections,
        skipped=skipped,
    )
    return {"tenants": len(results), "detections": detections, "skipped": skipped}


class WorkerSettings:
    """arq worker configuration.

    Every setting is a class attribute, not an instance attribute. arq reads settings from
    ``settings_cls.__dict__`` (``arq.worker.get_kwargs``), which for a class contains only
    the class attributes; attributes assigned in ``__init__`` live on the instance and are
    silently ignored. ``redis_settings`` set in ``__init__`` was therefore dropped and the
    worker connected to arq's default ``localhost:6379`` instead of the configured broker,
    and the cron job never ran. The values are resolved here, at import time, so they are
    class attributes.
    """

    functions: ClassVar[list[Any]] = [
        run_sync_job,
        reap_stale_jobs_task,
        publish_outbox_task,
        analytics_sweep_task,
    ]
    on_startup = startup
    on_shutdown = shutdown

    # A synchronization is a bounded unit of work. The timeout is generous but finite so
    # a wedged connector cannot hold a worker slot forever.
    job_timeout = 900
    max_tries = 3
    keep_result = 3600
    health_check_interval = 30

    _settings = get_settings()
    redis_settings = RedisSettings.from_dsn(_settings.redis_url)
    cron_jobs: ClassVar[list[Any]] = [
        cron(reap_stale_jobs_task, minute={0, 30}, run_at_startup=False),
        # Drain the Layer 2 outbox every minute so publication latency is bounded without
        # tying publication to request traffic.
        cron(publish_outbox_task, minute=set(range(60)), run_at_startup=True),
        # The analytics sweep is a no-op unless enabled, so the schedule exists without
        # imposing detection on a deployment that has not configured it.
        cron(analytics_sweep_task, minute={5, 35}, run_at_startup=False),
    ]
