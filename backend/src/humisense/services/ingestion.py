"""Layer 2 ingestion handoff.

Layer 1 owns capture, envelope validation, transport, retries, and provenance. Layer 2
owns normalization, deduplication, semantic mapping, and retention of normalized data.

Delivery semantics: **at-least-once**. A record may be delivered more than once if a
request times out after Layer 2 accepted it. Layer 2 deduplicates on the idempotency key
``connection_id:source_record_id:content_hash``. Exactly-once is not claimed, because
proving it would require a transactional handshake Layer 1 does not have.

Retry policy: transient failures (connection error, timeout, 5xx, 429) are retried with
exponential backoff and jitter. Permanent failures (4xx other than 429) are not retried,
because a rejected envelope will be rejected again.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator

from humisense.config import Settings
from humisense.connectors.types import SourceRecordEnvelope
from humisense.errors import (
    ConfigurationError,
    HumisenseError,
    IngestionError,
    is_retryable,
)
from humisense.security.egress import EgressPolicy
from humisense.security.pinned_transport import PinnedAsyncHTTPTransport

logger = structlog.get_logger(__name__)

INGEST_PATH = "/ingest/v1/records"
MAX_BATCH_SIZE = 200


class IngestionRecord(BaseModel):
    """One envelope as transmitted to Layer 2."""

    model_config = ConfigDict(extra="forbid")

    idempotency_key: str
    schema_version: str
    connector_type: str
    connection_id: str
    organization_id: str
    source_record_id: str
    record_type: str
    source_timestamp: datetime | None
    captured_at: datetime
    payload: dict[str, Any]
    metadata: dict[str, Any]
    content_hash: str
    correlation_id: str
    untrusted: bool = True

    @classmethod
    def from_envelope(cls, envelope: SourceRecordEnvelope) -> IngestionRecord:
        return cls(
            idempotency_key=envelope.idempotency_key(),
            schema_version=envelope.schema_version,
            connector_type=envelope.connector_type,
            connection_id=str(envelope.connection_id),
            organization_id=str(envelope.organization_id),
            source_record_id=envelope.source_record_id,
            record_type=envelope.record_type,
            source_timestamp=envelope.source_timestamp,
            captured_at=envelope.captured_at,
            payload=envelope.payload,
            metadata=envelope.metadata,
            content_hash=envelope.content_hash,
            correlation_id=envelope.correlation_id,
            untrusted=True,
        )


class IngestionBatch(BaseModel):
    """A batch of records plus the tenant identity they belong to."""

    model_config = ConfigDict(extra="forbid")

    organization_id: str
    batch_id: str
    correlation_id: str
    produced_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    records: list[IngestionRecord]

    @field_validator("records")
    @classmethod
    def _bounded_batch(cls, value: list[IngestionRecord]) -> list[IngestionRecord]:
        if not value:
            raise ValueError("an ingestion batch must contain at least one record")
        if len(value) > MAX_BATCH_SIZE:
            raise ValueError(f"an ingestion batch must not exceed {MAX_BATCH_SIZE} records")
        return value


class IngestionAck(BaseModel):
    """Layer 2's acknowledgment."""

    model_config = ConfigDict(extra="ignore")

    accepted: int = 0
    rejected: int = 0
    duplicates: int = 0
    rejected_keys: list[str] = Field(default_factory=list)
    batch_id: str | None = None


class IngestionSink(Protocol):
    """Transports envelopes to Layer 2."""

    name: str

    async def deliver(self, batch: IngestionBatch) -> IngestionAck: ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class RetryPolicy:
    """Bounded retry with exponential backoff and full jitter."""

    max_attempts: int = 4
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0

    def delay_for(self, attempt: int) -> float:
        """Return the delay before retry number ``attempt`` (1-based).

        Full jitter is used, so concurrent workers that failed together do not retry in
        lockstep and re-create the load spike that caused the failure.
        """
        ceiling = min(self.base_delay_seconds * (2 ** (attempt - 1)), self.max_delay_seconds)
        return random.uniform(0, ceiling)


def classify_status(status_code: int) -> bool:
    """Return True when an HTTP status is worth retrying."""
    if status_code == 429:
        return True
    return status_code >= 500


@dataclass(slots=True)
class DeliveryResult:
    """Outcome of delivering one batch."""

    delivered: int = 0
    failed: int = 0
    duplicates: int = 0
    attempts: int = 0
    error_code: str | None = None
    failed_keys: list[str] = field(default_factory=list)


class HttpIngestionSink:
    """Posts batches to a Layer 2 HTTP endpoint."""

    name = "http"

    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str,
        egress_policy: EgressPolicy,
        retry_policy: RetryPolicy,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = bearer_token
        self._egress = egress_policy
        self._retry = retry_policy
        self._timeout = timeout_seconds
        self._client: httpx.AsyncClient | None = None
        # Validate the configured endpoint once, at construction. A destination that the
        # egress policy refuses must fail at startup rather than on the first delivery,
        # so a misconfiguration is visible immediately and no batch is lost to it.
        self._egress.validate(f"{self._base_url}{INGEST_PATH}")

    def _client_or_create(self) -> httpx.AsyncClient:
        if self._client is None:
            # The transport connects only to addresses the egress policy validated at
            # connection time, so a DNS answer that changes between the URL check and the
            # connect cannot redirect the request to an internal host (see
            # ``humisense.security.pinned_transport``).
            transport = PinnedAsyncHTTPTransport(
                policy=self._egress,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
            self._client = httpx.AsyncClient(
                transport=transport,
                timeout=httpx.Timeout(
                    connect=self._egress.connect_timeout,
                    read=self._timeout,
                    write=self._timeout,
                    pool=self._timeout,
                ),
                follow_redirects=False,
                # Ignore proxy/CA settings from the environment. A proxy discovered
                # through HTTP_PROXY would otherwise become a second, unpinned path to the
                # destination, which defeats the transport's whole purpose.
                trust_env=False,
            )
        return self._client

    async def deliver(self, batch: IngestionBatch) -> IngestionAck:
        url = f"{self._base_url}{INGEST_PATH}"
        # Validate the destination before every attempt. A redirect is not followed, so
        # there is no redirect target to revalidate.
        self._egress.validate(url)

        payload = batch.model_dump(mode="json")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._token}",
            "Idempotency-Key": batch.batch_id,
            "X-Correlation-Id": batch.correlation_id,
        }
        # Security-sensitive headers are set here and never overridden by caller input.
        client = self._client_or_create()

        last_error: Exception | None = None
        for attempt in range(1, self._retry.max_attempts + 1):
            try:
                response = await client.post(url, json=payload, headers=headers)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout) as exc:
                last_error = exc
                logger.warning(
                    "ingestion_transport_error",
                    attempt=attempt,
                    error_type=type(exc).__name__,
                )
            else:
                if response.status_code < 300:
                    try:
                        return IngestionAck.model_validate(response.json())
                    except Exception:  # noqa: BLE001 - malformed ack
                        return IngestionAck(accepted=len(batch.records), batch_id=batch.batch_id)
                if not classify_status(response.status_code):
                    raise IngestionError(
                        detail="Layer 2 rejected the batch with a permanent status",
                        context={"status_code": response.status_code},
                    )
                last_error = IngestionError(
                    detail="Layer 2 returned a retryable status",
                    context={"status_code": response.status_code},
                )
                logger.warning(
                    "ingestion_retryable_status",
                    attempt=attempt,
                    status_code=response.status_code,
                )

            if attempt < self._retry.max_attempts:
                await asyncio.sleep(self._retry.delay_for(attempt))

        raise IngestionError(
            detail="Layer 2 delivery failed after all attempts",
            context={"attempts": self._retry.max_attempts, "error_type": type(last_error).__name__},
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class LocalTestIngestionSink:
    """A test sink that keeps delivered batches in memory.

    Clearly identified as a test implementation: it performs no normalization, has no
    durability, and must never be used to onboard real enterprise data. Production
    configuration validation refuses this mode.
    """

    name = "local_test_receiver"

    def __init__(self) -> None:
        self._batches: list[IngestionBatch] = []
        self._seen_keys: set[str] = set()

    async def deliver(self, batch: IngestionBatch) -> IngestionAck:
        accepted = 0
        duplicates = 0
        for record in batch.records:
            if record.idempotency_key in self._seen_keys:
                duplicates += 1
                continue
            self._seen_keys.add(record.idempotency_key)
            accepted += 1
        self._batches.append(batch)
        logger.info(
            "local_test_ingestion_received",
            batch_id=batch.batch_id,
            accepted=accepted,
            duplicates=duplicates,
            note="test sink; not a Layer 2 implementation",
        )
        return IngestionAck(accepted=accepted, duplicates=duplicates, batch_id=batch.batch_id)

    async def close(self) -> None:
        return None

    @property
    def batches(self) -> list[IngestionBatch]:
        return list(self._batches)

    def reset(self) -> None:
        self._batches.clear()
        self._seen_keys.clear()


class DisabledIngestionSink:
    """No-op sink used when Layer 2 handoff is disabled.

    Delivery is reported as skipped rather than failed, because no downstream layer is
    configured. Records remain persisted in Layer 1, so nothing is lost; Layer 2 can
    replay from the store once it exists.
    """

    name = "disabled"

    async def deliver(self, batch: IngestionBatch) -> IngestionAck:
        return IngestionAck(accepted=0, rejected=0, duplicates=0, batch_id=batch.batch_id)

    async def close(self) -> None:
        return None


class IngestionService:
    """Batches envelopes and delivers them to the configured sink.

    Delivery is retried here, at the batch boundary, rather than relying on each sink to
    retry internally. One implementation of the policy then covers every sink, including
    a third-party one, and the retry is visible in one place.
    """

    def __init__(
        self,
        sink: IngestionSink,
        *,
        batch_size: int = MAX_BATCH_SIZE,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._sink = sink
        self._batch_size = min(batch_size, MAX_BATCH_SIZE)
        self._retry = retry_policy or RetryPolicy()

    @property
    def sink_name(self) -> str:
        return self._sink.name

    async def deliver_envelopes(
        self,
        envelopes: list[SourceRecordEnvelope],
        *,
        correlation_id: str,
    ) -> DeliveryResult:
        """Deliver envelopes in bounded batches, reporting partial failure."""
        if not envelopes:
            return DeliveryResult()

        result = DeliveryResult()
        if self._sink.name == "disabled":
            return result

        for start in range(0, len(envelopes), self._batch_size):
            chunk = envelopes[start : start + self._batch_size]
            organization_id = str(chunk[0].organization_id)
            batch = IngestionBatch(
                organization_id=organization_id,
                batch_id=str(uuid.uuid4()),
                correlation_id=correlation_id,
                records=[IngestionRecord.from_envelope(envelope) for envelope in chunk],
            )
            ack = await self._deliver_with_retry(batch, correlation_id=correlation_id)
            if ack is None:
                result.failed += len(chunk)
                result.error_code = result.error_code or IngestionError.code
                result.failed_keys.extend(record.idempotency_key for record in batch.records)
                continue
            result.attempts += 1
            result.delivered += ack.accepted
            result.duplicates += ack.duplicates
            if ack.rejected:
                result.failed += ack.rejected
                result.failed_keys.extend(ack.rejected_keys)
        return result

    async def _deliver_with_retry(
        self, batch: IngestionBatch, *, correlation_id: str
    ) -> IngestionAck | None:
        """Deliver one batch, retrying only failures that are worth retrying.

        A permanent failure (a refused destination, an authentication error, a payload the
        receiver rejects) is not retried: repeating it cannot change the outcome and would
        only delay the run. Returns ``None`` when the batch was not accepted.
        """
        for attempt in range(1, self._retry.max_attempts + 1):
            try:
                return await self._sink.deliver(batch)
            except HumisenseError as exc:
                retryable = is_retryable(exc.code)
                logger.warning(
                    "ingestion_batch_attempt_failed",
                    batch_id=batch.batch_id,
                    attempt=attempt,
                    error_code=exc.code,
                    retryable=retryable,
                    correlation_id=correlation_id,
                )
                if not retryable:
                    logger.error(
                        "ingestion_batch_failed",
                        batch_id=batch.batch_id,
                        record_count=len(batch.records),
                        error_code=exc.code,
                        correlation_id=correlation_id,
                    )
                    return None
            except Exception as exc:  # noqa: BLE001 - a sink bug must not abort the run
                logger.error(
                    "ingestion_sink_error",
                    batch_id=batch.batch_id,
                    attempt=attempt,
                    error_type=type(exc).__name__,
                    correlation_id=correlation_id,
                )
            if attempt < self._retry.max_attempts:
                await asyncio.sleep(self._retry.delay_for(attempt))
        logger.error(
            "ingestion_batch_failed",
            batch_id=batch.batch_id,
            record_count=len(batch.records),
            attempts=self._retry.max_attempts,
            correlation_id=correlation_id,
        )
        return None

    async def close(self) -> None:
        await self._sink.close()


def build_ingestion_service(settings: Settings, *, egress_policy: EgressPolicy) -> IngestionService:
    """Construct the ingestion service for the configured Layer 2 mode."""

    if settings.layer2_mode == "disabled":
        return IngestionService(DisabledIngestionSink(), batch_size=settings.sync_batch_size)

    if settings.layer2_mode == "local_test_receiver":
        if settings.is_production:
            raise ConfigurationError(
                detail="layer2_mode='local_test_receiver' is refused in production"
            )
        return IngestionService(LocalTestIngestionSink(), batch_size=settings.sync_batch_size)

    if settings.layer2_mode == "http":
        if egress_policy is None:
            # An HTTP sink without the outbound-destination policy would post to an
            # arbitrary user-controlled address, which is the SSRF boundary this
            # platform refuses to cross.
            raise ConfigurationError(detail="http Layer 2 delivery requires an egress policy")
        if not settings.layer2_base_url:
            raise ConfigurationError(detail="layer2_base_url is required for http mode")
        token = settings.layer2_bearer_token_resolved()
        if not token:
            raise ConfigurationError(
                detail="A Layer 2 bearer token is required; configure layer2_secret_ref or "
                "the HUMISENSE_LAYER2_TOKEN environment variable"
            )
        return IngestionService(
            HttpIngestionSink(
                base_url=settings.layer2_base_url,
                bearer_token=token,
                egress_policy=egress_policy,
                retry_policy=RetryPolicy(max_attempts=settings.layer2_max_attempts),
                timeout_seconds=settings.layer2_timeout_seconds,
            ),
            batch_size=settings.sync_batch_size,
        )

    raise ConfigurationError(detail=f"Unknown layer2_mode {settings.layer2_mode!r}")
