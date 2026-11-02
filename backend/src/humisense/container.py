"""Application container.

Constructs the object graph once per process and holds no request state. Everything a
request or a worker job needs is reached through this object, which keeps the wiring in
one reviewable place instead of scattered through module-level globals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import redis.asyncio as aioredis
import structlog

from humisense.config import Settings
from humisense.connectors.base import load_builtin_connectors, registry
from humisense.db.session import Database
from humisense.layer2.outbox import OutboxPublisher
from humisense.layer2.pipeline import IngestionPipeline
from humisense.layer2.receiver import IngestionReceiver
from humisense.layer2.replay import QuarantineService
from humisense.observability.metrics import Metrics
from humisense.secrets.base import SecretsProvider
from humisense.secrets.factory import build_secrets_provider
from humisense.security.identity import TokenVerifier, build_token_verifier
from humisense.security.rate_limit import RateLimiter, build_rate_limiter
from humisense.services.audit import AuditService
from humisense.services.connections import ConnectionService
from humisense.services.ingestion import IngestionService, build_ingestion_service
from humisense.services.records import RecordService
from humisense.services.runtime import ConnectorRuntime
from humisense.services.sync import SyncService
from humisense.workers.queue import SyncJobPayload, TaskQueue, build_task_queue

logger = structlog.get_logger(__name__)


@dataclass
class Container:
    """Process-wide dependencies."""

    settings: Settings
    database: Database
    secrets_provider: SecretsProvider
    runtime: ConnectorRuntime
    audit: AuditService
    records: RecordService
    connections: ConnectionService
    ingestion: IngestionService
    sync: SyncService
    metrics: Metrics
    layer2_pipeline: IngestionPipeline
    layer2_receiver: IngestionReceiver
    layer2_quarantine: QuarantineService
    layer2_outbox: OutboxPublisher
    token_verifier: TokenVerifier
    rate_limiter: RateLimiter
    task_queue: TaskQueue
    redis: aioredis.Redis | None = None
    _closed: bool = field(default=False, repr=False)

    async def enqueue_sync(
        self, *, organization_id: Any, job_id: Any, correlation_id: str
    ) -> str | None:
        """Enqueue a synchronization job."""
        return await self.task_queue.enqueue_sync(
            SyncJobPayload(
                organization_id=organization_id,
                job_id=job_id,
                correlation_id=correlation_id,
            )
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.layer2_outbox.close()
        await self.task_queue.close()
        await self.ingestion.close()
        await self.database.dispose()
        if self.redis is not None:
            await self.redis.aclose()


def build_container(settings: Settings, *, inline_runner: Any = None) -> Container:
    """Construct the application container.

    Raises:
        ConfigurationError: when the configuration is unsafe for the environment.
    """
    settings.validate_for_production()
    settings.assert_runtime_safe()

    load_builtin_connectors()
    logger.info("connectors_loaded", connector_types=registry.connector_types())

    database = Database(settings)
    secrets_provider = build_secrets_provider(settings, database)
    runtime = ConnectorRuntime(settings=settings, secrets_provider=secrets_provider)
    audit = AuditService(database)
    records = RecordService(database)
    metrics = Metrics()

    egress_policy = runtime.egress_policy()
    ingestion = build_ingestion_service(settings, egress_policy=egress_policy)

    connections = ConnectionService(database, settings, audit, runtime)
    sync = SyncService(database, settings, audit, runtime, records, ingestion, metrics=metrics)

    layer2_pipeline = IngestionPipeline(database, settings, metrics=metrics)
    layer2_receiver = IngestionReceiver(
        database, settings, pipeline=layer2_pipeline, metrics=metrics
    )
    layer2_quarantine = QuarantineService(
        database, settings, pipeline=layer2_pipeline, metrics=metrics
    )
    layer2_outbox = OutboxPublisher(
        database,
        settings,
        metrics=metrics,
        egress_policy=egress_policy,
    )

    token_verifier = build_token_verifier(settings)
    rate_limiter = build_rate_limiter(settings.redis_url, enabled=settings.rate_limit_enabled)

    queue_runner = inline_runner or (
        lambda payload: sync.run_job(payload.organization_id, payload.job_id)
    )
    task_queue = build_task_queue(settings, inline_runner=queue_runner)

    return Container(
        settings=settings,
        database=database,
        secrets_provider=secrets_provider,
        runtime=runtime,
        audit=audit,
        records=records,
        connections=connections,
        ingestion=ingestion,
        sync=sync,
        metrics=metrics,
        layer2_pipeline=layer2_pipeline,
        layer2_receiver=layer2_receiver,
        layer2_quarantine=layer2_quarantine,
        layer2_outbox=layer2_outbox,
        token_verifier=token_verifier,
        rate_limiter=rate_limiter,
        task_queue=task_queue,
    )
