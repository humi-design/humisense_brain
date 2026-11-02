"""Transactional outbox publisher.

The publisher is the only component that moves an outbox row from ``pending`` to
``published``. It publishes at least once and only marks an event published after the sink
confirms, so Layer 2 never claims a record was published when it was not.

Delivery modes:

* ``disabled`` — no publishing. The default, so nothing leaves the deployment by accident.
* ``log``      — the event is written to the structured log. Useful for a pilot and for
  proving the pipeline without a downstream consumer.
* ``http``     — the event is POSTed to a configured sink with a bearer token.

Failures are retried with exponential backoff up to a bound, after which the event is marked
``failed`` and left for an operator. A failed event is never silently dropped.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import structlog
from sqlalchemy import func, select

from humisense.config import Settings
from humisense.db.models import (
    CanonicalRecord,
    Organization,
    OutboxEvent,
    OutboxStatus,
    RecordProcessingEvent,
    RecordState,
)
from humisense.db.session import Database
from humisense.layer2.errors import Stage
from humisense.observability.metrics import Metrics
from humisense.security.egress import EgressPolicy
from humisense.security.pinned_transport import PinnedAsyncHTTPTransport

logger = structlog.get_logger(__name__)

DEFAULT_BATCH_SIZE = 100
MAX_ATTEMPTS = 8
BASE_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 300.0


@dataclass(slots=True)
class PublishResult:
    published: int = 0
    failed: int = 0
    skipped: int = 0


class OutboxPublisher:
    """Publishes pending outbox events and advances record state to PUBLISHED."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        metrics: Metrics | None = None,
        transport: object | None = None,
        egress_policy: EgressPolicy | None = None,
    ) -> None:
        self._database = database
        self._settings = settings
        self._metrics = metrics
        # The transport is injected so tests exercise the real state transitions without a
        # network. In production it is the pinned HTTP client built from the egress policy.
        self._transport = transport
        self._egress = egress_policy
        self._client: httpx.AsyncClient | None = None

    async def publish_pending(
        self, *, organization_id: uuid.UUID, limit: int = DEFAULT_BATCH_SIZE
    ) -> PublishResult:
        result = PublishResult()
        mode = self._settings.layer2_publish_mode
        async with self._database.tenant_session(organization_id) as session:
            events = (
                await session.scalars(
                    select(OutboxEvent)
                    .where(
                        OutboxEvent.organization_id == organization_id,
                        OutboxEvent.status == OutboxStatus.PENDING,
                    )
                    .order_by(OutboxEvent.created_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).all()

            for event in events:
                if mode == "disabled":
                    # No sink is configured. This is not a delivery failure, so the attempt
                    # counter is left alone: the event stays pending, intact, for whenever a
                    # sink is enabled.
                    result.skipped += 1
                    continue
                if event.attempt > 0:
                    backoff = min(
                        BASE_BACKOFF_SECONDS * (2 ** (event.attempt - 1)), MAX_BACKOFF_SECONDS
                    )
                    not_before = event.created_at + timedelta(seconds=backoff)
                    if datetime.now(UTC) < not_before and event.last_error_code is not None:
                        result.skipped += 1
                        continue

                delivered = await self._deliver(event, mode)
                if delivered:
                    event.status = OutboxStatus.PUBLISHED
                    event.published_at = datetime.now(UTC)
                    result.published += 1
                    canonical = await session.scalar(
                        select(CanonicalRecord).where(
                            CanonicalRecord.id == event.canonical_record_id
                        )
                    )
                    if (
                        canonical is not None
                        and canonical.processing_state != RecordState.PUBLISHED
                    ):
                        canonical.processing_state = RecordState.PUBLISHED
                        session.add(
                            RecordProcessingEvent(
                                id=uuid.uuid4(),
                                organization_id=event.organization_id,
                                canonical_record_id=event.canonical_record_id,
                                from_state=RecordState.STORED.value,
                                to_state=RecordState.PUBLISHED.value,
                                stage=Stage.PUBLISH.value,
                                correlation_id=event.correlation_id,
                            )
                        )
                else:
                    event.attempt += 1
                    if event.attempt >= MAX_ATTEMPTS:
                        event.status = OutboxStatus.FAILED
                        event.last_error_code = "publish_exhausted"
                        result.failed += 1
                        if self._metrics:
                            self._metrics.layer2_outbox_failed()
                    else:
                        event.last_error_code = "publish_retry"
                        result.skipped += 1
                        if self._metrics:
                            self._metrics.layer2_retry()

        if self._metrics:
            self._metrics.layer2_outbox_published(count=result.published)
        return result

    async def _deliver(self, event: OutboxEvent, mode: str) -> bool:
        """Deliver one event. Returns whether the sink confirmed receipt."""
        if mode == "disabled":
            # Nothing leaves the deployment. The event stays pending so enabling a sink later
            # publishes it rather than losing it.
            return False
        if mode == "log":
            logger.info(
                "layer2_outbox_event",
                canonical_record_id=str(event.canonical_record_id),
                event_type=event.event_type,
                correlation_id=event.correlation_id,
            )
            return True
        if mode == "http":
            return await self._deliver_http(event)
        logger.error("layer2_publish_mode_unknown", mode=mode)
        return False

    async def _deliver_http(self, event: OutboxEvent) -> bool:
        base_url = self._settings.layer2_publish_base_url
        token = self._settings.layer2_publish_token_resolved()
        if not base_url or not token:
            logger.error("layer2_publish_misconfigured")
            return False
        url = f"{base_url.rstrip('/')}/events"
        # Revalidate before every attempt, exactly as the Layer 1 sink does: a redirect is
        # not followed, so there is no second destination to check.
        if self._egress is not None:
            self._egress.validate(url)
        try:
            response = await self._client_or_create().post(
                url,
                json={
                    "event_type": event.event_type,
                    "canonical_record_id": str(event.canonical_record_id),
                    "payload": event.payload,
                    "correlation_id": event.correlation_id,
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            logger.warning(
                "layer2_publish_failed",
                error_type=type(exc).__name__,
                correlation_id=event.correlation_id,
            )
            return False

    def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is None:
            # Connections are address-pinned by the egress policy, so a DNS answer that
            # changes between the URL check and the connect cannot redirect the request to
            # an internal host. The transport is injected in tests.
            if self._transport is not None:
                transport: httpx.AsyncBaseTransport | None = self._transport  # type: ignore[assignment]
            elif self._egress is not None:
                transport = PinnedAsyncHTTPTransport(
                    policy=self._egress,
                    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                )
            else:
                # No egress policy is configured (only reachable in tests); the caller is
                # responsible for the destination in that case.
                transport = None
            connect_timeout = self._egress.connect_timeout if self._egress is not None else 5.0
            self._client = httpx.AsyncClient(
                transport=transport,
                timeout=httpx.Timeout(
                    connect=connect_timeout,
                    read=self._settings.layer2_timeout_seconds,
                    write=self._settings.layer2_timeout_seconds,
                    pool=self._settings.layer2_timeout_seconds,
                ),
                follow_redirects=False,
                trust_env=False,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def pending_count(self, organization_id: uuid.UUID) -> int:
        async with self._database.tenant_session(organization_id) as session:
            count = await session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.organization_id == organization_id,
                    OutboxEvent.status == OutboxStatus.PENDING,
                )
            )
        return int(count or 0)

    async def publish_all_tenants(self, *, limit: int = DEFAULT_BATCH_SIZE) -> PublishResult:
        """Publish pending events for every tenant.

        The publisher is a background loop, not a request handler, so no single tenant is
        in scope. Each tenant is drained inside its own ``tenant_session``, which sets the
        RLS GUC for that transaction only: the loop cannot read or write across tenants even
        though it visits them in sequence.
        """
        async with self._database.system_session() as session:
            organization_ids = (
                await session.scalars(select(Organization.id).order_by(Organization.id))
            ).all()

        total = PublishResult()
        for organization_id in organization_ids:
            result = await self.publish_pending(organization_id=organization_id, limit=limit)
            total.published += result.published
            total.failed += result.failed
            total.skipped += result.skipped
        return total


async def publish_forever(
    publisher: OutboxPublisher, organization_id: uuid.UUID, *, interval_seconds: float = 5.0
) -> None:
    """Continuously publish pending events. Used by the worker; cancellable."""
    while True:
        try:
            await publisher.publish_pending(organization_id=organization_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop must survive a transient fault
            logger.error("layer2_outbox_loop_error", error_type=type(exc).__name__)
        await asyncio.sleep(interval_seconds)
