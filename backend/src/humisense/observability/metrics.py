"""Prometheus metrics.

Cardinality discipline: label values come from a small, bounded set (status, error code,
connector type). Tenant identifiers are deliberately **not** labels, because an
unbounded label would let one tenant exhaust the metric registry. Per-tenant visibility
comes from the audit trail and the sync job table instead.

Payload content is never a metric value or a label.
"""

from __future__ import annotations

import uuid

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

SYNC_STARTED = Counter(
    "humisense_sync_started_total",
    "Synchronization jobs that started executing.",
)

SYNC_FINISHED = Counter(
    "humisense_sync_finished_total",
    "Synchronization jobs that finished, by status and error code.",
    labelnames=("status", "error_code"),
)

SYNC_DURATION = Histogram(
    "humisense_sync_duration_seconds",
    "Wall-clock duration of a synchronization job.",
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300),
)

RECORDS_INGESTED = Counter(
    "humisense_records_ingested_total",
    "Source records accepted by the downstream layer.",
)

RECORDS_CAPTURED = Counter(
    "humisense_records_captured_total",
    "Source records captured and persisted.",
    labelnames=("connector_type",),
)

RECORDS_FAILED = Counter(
    "humisense_records_failed_total",
    "Source records that failed validation or delivery.",
    labelnames=("connector_type", "reason"),
)

INGESTION_RETRIES = Counter(
    "humisense_ingestion_retries_total",
    "Retry attempts made while delivering to the downstream layer.",
)

CONNECTIONS_TOTAL = Gauge(
    "humisense_connections_total",
    "Connections by connector type and status.",
    labelnames=("connector_type", "status"),
)

API_REQUESTS = Counter(
    "humisense_api_requests_total",
    "API requests by route, method, and status class.",
    labelnames=("route", "method", "status_class"),
)

API_LATENCY = Histogram(
    "humisense_api_request_duration_seconds",
    "API request latency by route.",
    labelnames=("route",),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)

RATE_LIMIT_REJECTIONS = Counter(
    "humisense_rate_limit_rejections_total",
    "Requests rejected by a rate limit.",
    labelnames=("scope",),
)

AUDIT_WRITE_FAILURES = Counter(
    "humisense_audit_write_failures_total",
    "Audit events that could not be persisted.",
)


class Metrics:
    """Thin wrapper that keeps tenant identifiers out of label values."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self._registry = registry

    def sync_started(self, *, organization_id: uuid.UUID) -> None:
        del organization_id  # never a label
        SYNC_STARTED.inc()

    def sync_finished(
        self,
        *,
        organization_id: uuid.UUID,
        status: str,
        duration_seconds: float,
        error_code: str | None,
    ) -> None:
        del organization_id
        SYNC_FINISHED.labels(status=status, error_code=error_code or "none").inc()
        SYNC_DURATION.observe(duration_seconds)

    def records_ingested(self, *, organization_id: uuid.UUID, count: int) -> None:
        del organization_id
        if count:
            RECORDS_INGESTED.inc(count)

    def records_captured(self, *, connector_type: str, count: int) -> None:
        if count:
            RECORDS_CAPTURED.labels(connector_type=connector_type).inc(count)

    def records_failed(self, *, connector_type: str, reason: str, count: int = 1) -> None:
        if count:
            RECORDS_FAILED.labels(connector_type=connector_type, reason=reason).inc(count)

    def ingestion_retry(self) -> None:
        INGESTION_RETRIES.inc()

    def api_request(self, *, route: str, method: str, status_code: int, duration: float) -> None:
        API_REQUESTS.labels(
            route=route, method=method, status_class=f"{status_code // 100}xx"
        ).inc()
        API_LATENCY.labels(route=route).observe(duration)

    def rate_limit_rejected(self, *, scope: str) -> None:
        RATE_LIMIT_REJECTIONS.labels(scope=scope).inc()

    def audit_write_failed(self) -> None:
        AUDIT_WRITE_FAILURES.inc()
