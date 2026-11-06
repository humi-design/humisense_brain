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

# --- Layer 2 ---------------------------------------------------------------------

L2_RECORDS_RECEIVED = Counter(
    "humisense_layer2_records_received_total",
    "Records received by the Layer 2 ingestion receiver.",
)

L2_RECORDS_STORED = Counter(
    "humisense_layer2_records_stored_total",
    "Canonical records stored by Layer 2, by data family.",
    labelnames=("data_family",),
)

L2_RECORDS_DUPLICATE = Counter(
    "humisense_layer2_records_duplicate_total",
    "Records recognized as duplicate deliveries.",
)

L2_RECORDS_QUARANTINED = Counter(
    "humisense_layer2_records_quarantined_total",
    "Records quarantined, by error code.",
    labelnames=("reason",),
)

L2_VALIDATION_FAILURES = Counter(
    "humisense_layer2_validation_failures_total",
    "Records that failed validation, by error code.",
    labelnames=("reason",),
)

L2_NORMALIZATION_FAILURES = Counter(
    "humisense_layer2_normalization_failures_total",
    "Records that failed normalization, by error code.",
    labelnames=("reason",),
)

L2_RETRIES = Counter(
    "humisense_layer2_retries_total",
    "Retry attempts made by the Layer 2 pipeline.",
)

L2_PROCESSING_LATENCY = Histogram(
    "humisense_layer2_processing_latency_seconds",
    "Wall-clock latency of processing one batch.",
    buckets=(0.005, 0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30),
)

L2_OUTBOX_PENDING = Gauge(
    "humisense_layer2_outbox_pending",
    "Outbox events awaiting publication.",
)

L2_OUTBOX_PUBLISHED = Counter(
    "humisense_layer2_outbox_published_total",
    "Outbox events published downstream.",
)

L2_OUTBOX_FAILED = Counter(
    "humisense_layer2_outbox_failed_total",
    "Outbox events that failed to publish.",
)

L2_QUEUE_LAG = Gauge(
    "humisense_layer2_queue_lag_seconds",
    "Age of the oldest unprocessed received record.",
)

# --- Layer 3 ---------------------------------------------------------------------

L3_RUNS_STARTED = Counter(
    "humisense_layer3_runs_started_total",
    "Analytics runs started, by job type.",
    labelnames=("job_type",),
)

L3_RUNS_FINISHED = Counter(
    "humisense_layer3_runs_finished_total",
    "Analytics runs finished, by job type and status.",
    labelnames=("job_type", "status"),
)

L3_RUN_DURATION = Histogram(
    "humisense_layer3_run_duration_seconds",
    "Wall-clock duration of an analytics run.",
    labelnames=("job_type",),
    buckets=(0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 300),
)

L3_DETECTIONS = Counter(
    "humisense_layer3_detections_total",
    "Detections emitted, by domain, severity, and method.",
    labelnames=("domain", "severity", "method"),
)

L3_DETECTIONS_SUPPRESSED = Counter(
    "humisense_layer3_detections_suppressed_total",
    "Detections withheld by cooldown or suppression.",
    labelnames=("reason",),
)

L3_QUALITY_RESULTS = Counter(
    "humisense_layer3_quality_results_total",
    "Data-quality assessments, by status.",
    labelnames=("status",),
)

L3_READINESS_RESULTS = Counter(
    "humisense_layer3_readiness_results_total",
    "Readiness evaluations, by method and status.",
    labelnames=("method", "status"),
)

L3_SKIPS = Counter(
    "humisense_layer3_skips_total",
    "Signals skipped before detection, by stage and error code.",
    labelnames=("stage", "reason"),
)

L3_FEATURES_COMPUTED = Counter(
    "humisense_layer3_features_computed_total",
    "Feature values computed and persisted.",
)

L3_BASELINES_BUILT = Counter(
    "humisense_layer3_baselines_built_total",
    "Baseline versions built.",
)

L3_BASELINE_DRIFT_BLOCKED = Counter(
    "humisense_layer3_baseline_drift_blocked_total",
    "Baseline rebuilds blocked by the drift guard.",
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

    # --- Layer 2 ----------------------------------------------------------

    def layer2_received(self, *, count: int = 1) -> None:
        if count:
            L2_RECORDS_RECEIVED.inc(count)

    def layer2_stored(self, *, data_family: str, count: int = 1) -> None:
        if count:
            L2_RECORDS_STORED.labels(data_family=data_family).inc(count)

    def layer2_duplicate(self, *, count: int = 1) -> None:
        if count:
            L2_RECORDS_DUPLICATE.inc(count)

    def layer2_quarantined(self, *, reason: str, count: int = 1) -> None:
        if count:
            L2_RECORDS_QUARANTINED.labels(reason=reason).inc(count)

    def layer2_validation_failed(self, *, reason: str) -> None:
        L2_VALIDATION_FAILURES.labels(reason=reason).inc()

    def layer2_normalization_failed(self, *, reason: str) -> None:
        L2_NORMALIZATION_FAILURES.labels(reason=reason).inc()

    def layer2_retry(self) -> None:
        L2_RETRIES.inc()

    def layer2_batch_processed(self, *, duration_seconds: float) -> None:
        L2_PROCESSING_LATENCY.observe(duration_seconds)

    def layer2_outbox_pending(self, *, count: int) -> None:
        L2_OUTBOX_PENDING.set(count)

    def layer2_outbox_published(self, *, count: int = 1) -> None:
        if count:
            L2_OUTBOX_PUBLISHED.inc(count)

    def layer2_outbox_failed(self, *, count: int = 1) -> None:
        if count:
            L2_OUTBOX_FAILED.inc(count)

    def layer2_queue_lag(self, *, seconds: float) -> None:
        L2_QUEUE_LAG.set(seconds)

    # --- Layer 3 ----------------------------------------------------------

    def layer3_run_started(self, *, job_type: str) -> None:
        L3_RUNS_STARTED.labels(job_type=job_type).inc()

    def layer3_run_finished(self, *, job_type: str, status: str, duration_seconds: float) -> None:
        L3_RUNS_FINISHED.labels(job_type=job_type, status=status).inc()
        L3_RUN_DURATION.labels(job_type=job_type).observe(duration_seconds)

    def layer3_detection(self, *, domain: str, severity: str, method: str) -> None:
        L3_DETECTIONS.labels(domain=domain, severity=severity, method=method).inc()

    def layer3_detection_suppressed(self, *, reason: str) -> None:
        L3_DETECTIONS_SUPPRESSED.labels(reason=reason).inc()

    def layer3_quality_result(self, *, status: str) -> None:
        L3_QUALITY_RESULTS.labels(status=status).inc()

    def layer3_readiness_result(self, *, method: str, status: str) -> None:
        L3_READINESS_RESULTS.labels(method=method, status=status).inc()

    def layer3_skip(self, *, stage: str, reason: str) -> None:
        L3_SKIPS.labels(stage=stage, reason=reason).inc()

    def layer3_features_computed(self, *, count: int = 1) -> None:
        if count:
            L3_FEATURES_COMPUTED.inc(count)

    def layer3_baseline_built(self) -> None:
        L3_BASELINES_BUILT.inc()

    def layer3_baseline_drift_blocked(self) -> None:
        L3_BASELINE_DRIFT_BLOCKED.inc()


# --- Layer 4 ---------------------------------------------------------------------

L4_RUNS_STARTED = Counter(
    "humisense_layer4_runs_started_total",
    "Work derivation runs started, by job type.",
    labelnames=("job_type",),
)

L4_RUNS_FINISHED = Counter(
    "humisense_layer4_runs_finished_total",
    "Work derivation runs finished, by job type and status.",
    labelnames=("job_type", "status"),
)

L4_RUN_DURATION = Histogram(
    "humisense_layer4_run_duration_seconds",
    "Wall-clock duration of a work derivation run.",
    labelnames=("job_type",),
    buckets=(0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30, 60, 300),
)

L4_WORK_CREATED = Counter(
    "humisense_layer4_work_created_total",
    "Work entities created.",
)

L4_WORK_UPDATED = Counter(
    "humisense_layer4_work_updated_total",
    "Work entities updated.",
)

L4_SKIPS = Counter(
    "humisense_layer4_skips_total",
    "Source records skipped before derivation, by stage and reason.",
    labelnames=("stage", "reason"),
)

L4_EVENTS_ENQUEUED = Counter(
    "humisense_layer4_events_enqueued_total",
    "Layer 4 outbox events enqueued, by event type.",
    labelnames=("event_type",),
)

L4_OUTBOX_PENDING = Gauge(
    "humisense_layer4_outbox_pending",
    "Work outbox events awaiting publication.",
)

L4_OUTBOX_PUBLISHED = Counter(
    "humisense_layer4_outbox_published_total",
    "Work outbox events published downstream.",
)

L4_OUTBOX_FAILED = Counter(
    "humisense_layer4_outbox_failed_total",
    "Work outbox events that failed to publish.",
)

L4_CONFLICTS_DETECTED = Counter(
    "humisense_layer4_conflicts_detected_total",
    "Work conflicts detected, by conflict type.",
    labelnames=("conflict_type",),
)


class WorkMetrics:
    """Layer 4 metric emitters, kept separate from the Layer 1-3 facade.

    Tenant identifiers are never label values, matching the platform rule: a metric label is
    an unbounded-cardinality hazard and a data-exposure surface.
    """

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self._registry = registry

    def layer4_run_started(self, *, job_type: str) -> None:
        L4_RUNS_STARTED.labels(job_type=job_type).inc()

    def layer4_run_finished(self, *, job_type: str, status: str, duration_seconds: float) -> None:
        L4_RUNS_FINISHED.labels(job_type=job_type, status=status).inc()
        L4_RUN_DURATION.labels(job_type=job_type).observe(duration_seconds)

    def layer4_work_created(self, *, count: int = 1) -> None:
        if count:
            L4_WORK_CREATED.inc(count)

    def layer4_work_updated(self, *, count: int = 1) -> None:
        if count:
            L4_WORK_UPDATED.inc(count)

    def layer4_skip(self, *, stage: str, reason: str) -> None:
        L4_SKIPS.labels(stage=stage, reason=reason).inc()

    def layer4_event_enqueued(self, *, event_type: str) -> None:
        L4_EVENTS_ENQUEUED.labels(event_type=event_type).inc()

    def layer4_outbox_pending(self, *, value: float) -> None:
        L4_OUTBOX_PENDING.set(value)

    def layer4_outbox_published(self) -> None:
        L4_OUTBOX_PUBLISHED.inc()

    def layer4_outbox_failed(self) -> None:
        L4_OUTBOX_FAILED.inc()

    def layer4_conflict_detected(self, *, conflict_type: str) -> None:
        L4_CONFLICTS_DETECTED.labels(conflict_type=conflict_type).inc()
