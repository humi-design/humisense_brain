"""Task queue abstraction.

Long-running external capture must not run inside a request handler: a slow source system
would occupy request workers and degrade the whole API. ``POST /connections/{id}/sync``
therefore enqueues a job and returns immediately.

Two implementations exist:

* :class:`ArqTaskQueue` — the production path. Jobs are persisted in Redis and executed
  by a separate worker process.
* :class:`InlineTaskQueue` — runs the job in an ``asyncio`` task in the API process.
  Provided for tests and single-process local development only. It is refused in
  production because it reintroduces exactly the coupling the queue exists to remove.

The queue payload carries only two identifiers and a correlation id. No credentials, no
configuration, and no record data ever enter the queue, so a compromised Redis cannot
yield source data or credentials.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

import structlog

from humisense.errors import ConfigurationError

logger = structlog.get_logger(__name__)

SYNC_JOB_TASK = "run_sync_job"


@dataclass(frozen=True, slots=True)
class SyncJobPayload:
    """The complete, minimal job payload."""

    organization_id: uuid.UUID
    job_id: uuid.UUID
    correlation_id: str

    def as_args(self) -> tuple[str, str, str]:
        return (str(self.organization_id), str(self.job_id), self.correlation_id)


class TaskQueue(Protocol):
    """Enqueues background work."""

    name: str

    async def enqueue_sync(self, payload: SyncJobPayload) -> str | None:
        """Enqueue a synchronization job. Returns the queue job id, when available."""
        ...

    async def close(self) -> None: ...


class ArqTaskQueue:
    """Enqueues jobs onto Redis for an ``arq`` worker."""

    name = "arq"

    def __init__(self, redis_settings: Any) -> None:
        self._redis_settings = redis_settings
        self._pool: Any | None = None

    async def _get_pool(self) -> Any:
        if self._pool is None:
            from arq import create_pool

            self._pool = await create_pool(self._redis_settings)
        return self._pool

    async def enqueue_sync(self, payload: SyncJobPayload) -> str | None:
        pool = await self._get_pool()
        job = await pool.enqueue_job(SYNC_JOB_TASK, *payload.as_args())
        if job is None:
            # arq returns None when an identical job id is already queued.
            logger.info(
                "sync_enqueue_deduplicated",
                job_id=str(payload.job_id),
                correlation_id=payload.correlation_id,
            )
            return None
        return str(job.job_id)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.aclose()
            self._pool = None


class InlineTaskQueue:
    """Runs the job in-process. Tests and single-process development only."""

    name = "inline"

    def __init__(self, runner: Any) -> None:
        self._runner = runner
        self._tasks: set[asyncio.Task[None]] = set()
        self._failures: list[BaseException] = []

    async def enqueue_sync(self, payload: SyncJobPayload) -> str | None:
        task = asyncio.create_task(self._run(payload))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return f"inline-{payload.job_id}"

    async def _run(self, payload: SyncJobPayload) -> None:
        try:
            await self._runner(payload)
        except Exception as exc:  # noqa: BLE001 - surfaced to tests, not to clients
            self._failures.append(exc)
            logger.error(
                "inline_sync_failed",
                job_id=str(payload.job_id),
                error_type=type(exc).__name__,
            )

    async def drain(self, *, deadline_seconds: float = 60.0) -> None:
        """Wait for in-flight jobs. Used by tests before asserting on results."""
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=deadline_seconds)

    @property
    def failures(self) -> list[BaseException]:
        return list(self._failures)

    async def close(self) -> None:
        await self.drain()


def build_task_queue(settings: Any, *, inline_runner: Any = None) -> TaskQueue:
    """Return the configured task queue."""
    mode = getattr(settings, "task_queue_mode", "arq")
    if mode == "inline":
        if settings.is_production:
            raise ConfigurationError(
                detail="task_queue_mode='inline' is refused in production; run a worker"
            )
        if inline_runner is None:
            raise ConfigurationError(detail="inline task queue requires a runner")
        return InlineTaskQueue(inline_runner)

    from arq.connections import RedisSettings

    return ArqTaskQueue(RedisSettings.from_dsn(settings.redis_url))
