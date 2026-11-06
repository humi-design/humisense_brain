"""API request and response schemas.

Rules applied to every model here:

* ``extra="forbid"`` so an unknown field is a validation error, not a silently ignored
  input. This is what prevents mass assignment of privileged fields.
* Response models list fields explicitly. No response model can serialize a credential,
  a ``secret_ref``, or an internal error detail.
* Bounded pagination on every list endpoint.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

StrictName = Annotated[str, Field(min_length=1, max_length=200)]
Description = Annotated[str, Field(max_length=2000)]


class ErrorBody(BaseModel):
    """The single error shape returned by every endpoint."""

    code: str
    message: str
    correlation_id: str | None = None
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str


class ReadinessResponse(BaseModel):
    status: Literal["ready", "degraded"]
    database: Literal["ok", "unavailable"]
    queue: Literal["ok", "unavailable", "not_required"]
    rate_limiter: Literal["ok", "degraded"]
    connectors_loaded: int


class CurrentUserResponse(BaseModel):
    user_id: uuid.UUID
    display_name: str | None
    email: str | None
    organization_id: uuid.UUID
    organization_name: str
    role: str
    permissions: list[str]
    auth_method: str


class ConfigFieldResponse(BaseModel):
    name: str
    label: str
    field_type: str
    required: bool
    help_text: str
    placeholder: str | None
    choices: list[str]
    max_length: int | None
    default: Any = None
    is_secret: bool


class ConnectorResponse(BaseModel):
    connector_type: str
    display_name: str
    description: str
    vendor: str
    version: str
    auth_modes: list[str]
    capabilities: dict[str, bool]
    availability: str
    unavailable_reason: str | None
    required_setup: list[str]
    documentation_url: str | None
    record_types: list[str]
    synthetic_only: bool
    config_fields: list[ConfigFieldResponse]


class ConnectionResponse(BaseModel):
    """A connection as returned to a client.

    ``secret_ref`` is deliberately absent: whether a credential is stored is exposed as a
    boolean, and the reference name itself is not client-visible.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    connector_type: str
    name: str
    description: str | None
    config: dict[str, Any]
    has_credentials: bool
    status: str
    enabled: bool
    capabilities: dict[str, bool]
    last_tested_at: datetime | None
    last_success_at: datetime | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime


class ConnectionListResponse(BaseModel):
    items: list[ConnectionResponse]
    total: int
    limit: int
    offset: int


class ConnectionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connector_type: Annotated[str, Field(min_length=1, max_length=80)]
    name: StrictName
    description: Description | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    credentials: dict[str, Any] = Field(default_factory=dict)

    @field_validator("connector_type")
    @classmethod
    def _connector_type_charset(cls, value: str) -> str:
        if not value.replace("_", "").isalnum():
            raise ValueError("connector_type must be alphanumeric with underscores")
        return value


class ConnectionUpdateRequest(BaseModel):
    """Update payload.

    ``connector_type`` is absent by design: the type of an existing connection is
    immutable, so a caller cannot repoint a connection at a different connector and
    inherit its configuration.
    """

    model_config = ConfigDict(extra="forbid")

    name: StrictName | None = None
    description: Description | None = None
    config: dict[str, Any] | None = None
    credentials: dict[str, Any] | None = None
    enabled: bool | None = None


class ConnectionTestResponse(BaseModel):
    ok: bool
    message: str
    error_code: str | None
    latency_ms: float | None
    details: dict[str, Any]


class SyncJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    connection_id: uuid.UUID
    status: str
    attempt: int
    records_fetched: int
    records_persisted: int
    records_delivered: int
    records_failed: int
    error_code: str | None
    error_message: str | None
    correlation_id: str
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_ms: float | None
    cancel_requested: bool


class SyncAcceptedResponse(BaseModel):
    job: SyncJobResponse
    queue_job_id: str | None


class SyncJobListResponse(BaseModel):
    items: list[SyncJobResponse]
    total: int
    limit: int
    offset: int


class ConnectionStatusResponse(BaseModel):
    connection_id: uuid.UUID
    status: str
    enabled: bool
    last_tested_at: datetime | None
    last_success_at: datetime | None
    last_error_code: str | None
    record_count: int
    last_job: SyncJobResponse | None
    connector_capabilities: dict[str, bool]


class SourceRecordResponse(BaseModel):
    id: str
    connector_type: str
    source_record_id: str
    record_type: str
    schema_version: str
    source_timestamp: str | None
    captured_at: str
    content_hash: str
    payload: dict[str, Any]
    payload_truncated: bool
    metadata: dict[str, Any]
    untrusted: bool


class SourceRecordListResponse(BaseModel):
    items: list[SourceRecordResponse]
    total: int
    limit: int
    offset: int
    notice: str


class AuditEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    actor_id: uuid.UUID | None
    actor_type: str
    action: str
    target_type: str | None
    target_id: str | None
    outcome: str
    correlation_id: str | None
    metadata: dict[str, Any]
    created_at: datetime


class AuditEventListResponse(BaseModel):
    items: list[AuditEventResponse]
    total: int
    limit: int
    offset: int


class OverviewResponse(BaseModel):
    organization_id: uuid.UUID
    organization_name: str
    connections_total: int
    connections_active: int
    connections_error: int
    connections_disabled: int
    connector_types_available: int
    connector_types_not_configured: int
    records_captured: int
    last_sync: SyncJobResponse | None
    recent_jobs: list[SyncJobResponse]
    ingestion_sink: str
    layer2_configured: bool


# --- Layer 2 ---------------------------------------------------------------


class IngestRecordRequest(BaseModel):
    """One record inside an ingestion batch envelope."""

    model_config = ConfigDict(extra="forbid")

    connection_id: uuid.UUID
    connector_type: str = Field(min_length=1, max_length=64)
    source_record_id: str = Field(min_length=1, max_length=200)
    record_type: str = Field(min_length=1, max_length=64)
    source_timestamp: datetime | None = None
    observed_timestamp: datetime
    payload: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_schema_version: str = Field(min_length=1, max_length=32)
    content_hash: str | None = Field(default=None, max_length=128)


class IngestBatchRequest(BaseModel):
    """The batch envelope a producer POSTs to the ingestion receiver."""

    model_config = ConfigDict(extra="forbid")

    envelope_version: Literal["1.0"]
    organization_id: uuid.UUID
    batch_id: str = Field(min_length=1, max_length=128)
    correlation_id: str | None = Field(default=None, max_length=128)
    producer: str | None = Field(default=None, max_length=200)
    records: list[IngestRecordRequest] = Field(min_length=1, max_length=200)


class RecordOutcomeResponse(BaseModel):
    source_record_id: str
    state: str
    error_code: str | None = None


class IngestBatchResponse(BaseModel):
    batch_id: str
    correlation_id: str
    accepted: int
    duplicates: int
    quarantined: int
    failed: int
    outcomes: list[RecordOutcomeResponse]


class QuarantineItemResponse(BaseModel):
    quarantine_id: uuid.UUID
    stage: str
    error_code: str
    source_record_key: str | None
    status: str
    replay_count: int
    created_at: datetime


class QuarantineListResponse(BaseModel):
    items: list[QuarantineItemResponse]
    total: int


class QuarantineReplayResponse(BaseModel):
    quarantine_id: uuid.UUID
    replayed: bool
    state: str
    error_code: str | None = None


# --- Layer 3: analytics -------------------------------------------------------


class AnalyticsRunRequest(BaseModel):
    """Request to run an analytics job over a bounded window."""

    model_config = ConfigDict(extra="forbid")

    since: datetime | None = None
    until: datetime | None = None
    connection_id: uuid.UUID | None = None
    job_type: Literal["analytics_full", "quality_scan"] = "analytics_full"
    row_limit: int = Field(default=20_000, ge=1, le=200_000)


class AnalyticsRunResponse(BaseModel):
    job_type: str
    correlation_id: str
    status: str
    signals_analyzed: int
    detections: int
    suppressed: int
    quality_issues: int
    skips: list[dict[str, str]]
    counters: dict[str, int]


class DetectionResponse(BaseModel):
    """One detection as returned to a consumer.

    Evidence is returned as references, never as a payload, matching the storage contract.
    """

    detection_id: uuid.UUID
    domain: str
    entity_id: str
    entity_type: str | None
    metric: str
    detection_category: str
    detection_type: str
    severity: str
    status: str
    observed_value: float | None
    expected_value: float | None
    deviation: float | None
    unit: str | None
    observation_window_start: datetime | None
    observation_window_end: datetime | None
    detected_at: datetime
    algorithm_name: str
    algorithm_version: str
    baseline_id: uuid.UUID | None
    baseline_version: int | None
    anomaly_score: float | None
    confidence: float | None
    data_quality_status: str
    evidence_references: list[dict[str, Any]]
    explanation: str
    limitations: list[str]
    schema_version: str
    correlation_id: str


class DetectionListResponse(BaseModel):
    items: list[DetectionResponse]
    total: int


class DetectionStatusUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["open", "acknowledged", "investigating", "resolved", "suppressed", "recovered"]
    reason: str | None = Field(default=None, max_length=500)
    suppress_for_seconds: int | None = Field(default=None, ge=0, le=2_592_000)


class QualityResultResponse(BaseModel):
    entity_id: str
    metric: str
    status: str
    evaluated_count: int
    missing_count: int
    invalid_count: int
    duplicate_count: int
    issues: list[dict[str, Any]]
    window_start: datetime | None
    window_end: datetime | None


class QualityListResponse(BaseModel):
    items: list[QualityResultResponse]
    total: int


class RuleCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: StrictName
    description: Description = ""
    domain: Literal["industrial", "business", "data_quality", "generic"] = "generic"
    scope: Literal["entity", "global"] = "entity"
    entity_id: str | None = Field(default=None, max_length=200)
    metric: str = Field(min_length=1, max_length=120)
    condition: dict[str, Any]
    window_seconds: int = Field(default=0, ge=0)
    severity: Literal["info", "low", "medium", "high", "critical"] = "medium"
    enabled: bool = True


class RuleResponse(BaseModel):
    rule_id: uuid.UUID
    name: str
    description: str
    domain: str
    scope: str
    entity_id: str | None
    metric: str
    condition: dict[str, Any]
    window_seconds: int
    severity: str
    enabled: bool
    version: int


# --- Layer 4: work intelligence ---------------------------------------------------
#
# Response models expose derived work with its provenance and confidence. They never expose
# a credential, a raw source payload, or an internal error detail, and they never present an
# inferred value as though it were confirmed.


class WorkSourceReferenceResponse(BaseModel):
    canonical_record_id: uuid.UUID
    source_system: str
    source_record_key: str
    source_record_type: str
    relationship: Literal["primary", "supporting", "mentioned"]
    confidence: float | None


class WorkAssignmentResponse(BaseModel):
    participant_type: str
    user_id: uuid.UUID | None
    department_id: uuid.UUID | None
    external_name: str | None
    role: str
    verification: str
    confidence: float | None


class WorkPriorityResponse(BaseModel):
    level: str
    source: str
    overdue: bool
    due_soon: bool
    blocked: bool
    dependency_count: int
    waiting_duration_seconds: int | None
    commitment_status: str | None
    source_completeness: float
    related_detection_ids: list[uuid.UUID]
    computed_at: datetime | None


class WorkEntityResponse(BaseModel):
    """One work entity, with its provenance and its unresolved fields left explicit."""

    work_id: uuid.UUID
    work_key: str
    work_type: str
    title: str
    description: str | None
    domain: str
    department_id: uuid.UUID | None
    owner_id: uuid.UUID | None
    assigned_user_id: uuid.UUID | None
    priority: str
    priority_source: str
    status: str
    source_status: str | None
    origin: str
    confidence: float | None
    start_at: datetime | None
    due_at: datetime | None
    due_source: str | None
    completed_at: datetime | None
    customer_ref: str | None
    supplier_ref: str | None
    order_ref: str | None
    project_ref: str | None
    machine_ref: str | None
    production_ref: str | None
    attributes: dict[str, Any]
    schema_version: str
    created_at: datetime
    updated_at: datetime


class WorkEntityDetailResponse(WorkEntityResponse):
    """A work entity with its related records for the detail view."""

    source_references: list[WorkSourceReferenceResponse]
    assignments: list[WorkAssignmentResponse]
    commitments: list[dict[str, Any]]
    dependencies: list[dict[str, Any]]
    blockers: list[dict[str, Any]]
    priorities: list[WorkPriorityResponse]
    timeline: list[dict[str, Any]]


class WorkListResponse(BaseModel):
    items: list[WorkEntityResponse]
    total: int
    limit: int
    offset: int


class CommitmentResponse(BaseModel):
    commitment_id: uuid.UUID
    work_id: uuid.UUID | None
    commitment_type: str
    description: str
    committing_party: str
    receiving_party: str
    responsible_owner: uuid.UUID | None
    related_customer_ref: str | None
    related_supplier_ref: str | None
    related_order_ref: str | None
    promised_at: datetime | None
    due_at: datetime | None
    actual_completion_at: datetime | None
    commitment_status: str
    verification_status: str
    confidence: float | None
    source_reference: str | None
    created_at: datetime
    updated_at: datetime


class CommitmentListResponse(BaseModel):
    items: list[CommitmentResponse]
    total: int
    limit: int
    offset: int


class DependencyResponse(BaseModel):
    dependency_id: uuid.UUID
    source_work_id: uuid.UUID
    target_work_id: uuid.UUID | None
    target_entity_type: str
    target_entity_ref: str | None
    relationship: str
    status: str
    origin: str
    verification_status: str
    created_at: datetime
    updated_at: datetime


class DependencyListResponse(BaseModel):
    items: list[DependencyResponse]
    total: int
    limit: int
    offset: int


class BlockerResponse(BaseModel):
    blocker_id: uuid.UUID
    affected_work_id: uuid.UUID
    blocker_type: str
    description: str
    blocking_entity: str | None
    responsible_department_id: uuid.UUID | None
    detected_at: datetime
    expected_resolution_at: datetime | None
    blocker_status: str
    evidence_references: list[dict[str, Any]]
    provenance: str
    verification_status: str


class BlockerListResponse(BaseModel):
    items: list[BlockerResponse]
    total: int
    limit: int
    offset: int


class FollowUpResponse(BaseModel):
    follow_up_id: uuid.UUID
    related_work_id: uuid.UUID
    target_type: str
    target_ref: str
    owner_id: uuid.UUID | None
    reason: str
    last_activity_at: datetime | None
    next_due_at: datetime | None
    urgency: str
    follow_up_status: str
    rule_id: str
    provenance: str


class FollowUpListResponse(BaseModel):
    items: list[FollowUpResponse]
    total: int
    limit: int
    offset: int


class ConflictResponse(BaseModel):
    conflict_id: uuid.UUID
    work_id: uuid.UUID | None
    entity_type: str
    entity_key: str
    conflict_type: str
    detail: str
    candidates: list[dict[str, Any]]
    status: str
    resolved_value: dict[str, Any] | None
    resolved_by: uuid.UUID | None
    resolution_note: str | None
    detected_at: datetime
    resolved_at: datetime | None


class ConflictListResponse(BaseModel):
    items: list[ConflictResponse]
    total: int
    limit: int
    offset: int


class ConflictResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolved_value: dict[str, Any] | None = None
    resolution_note: str | None = Field(default=None, max_length=1000)
    status: Literal["resolved", "accepted"] = "resolved"


class WorkStatusUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal[
        "new",
        "open",
        "assigned",
        "in_progress",
        "waiting",
        "blocked",
        "on_hold",
        "under_review",
        "completed",
        "cancelled",
        "unknown",
    ]
    reason: str | None = Field(default=None, max_length=500)


class WorkAssignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    role: Literal[
        "owner",
        "assignee",
        "requester",
        "approver",
        "collaborator",
        "accountable_manager",
        "escalation",
    ] = "assignee"


class WorkRunRequest(BaseModel):
    """Request a bounded derivation run for the caller's tenant."""

    model_config = ConfigDict(extra="forbid")

    since: datetime | None = None
    until: datetime | None = None
    row_limit: int = Field(default=5000, ge=1, le=50_000)


class WorkRunResponse(BaseModel):
    organization_id: uuid.UUID
    correlation_id: str
    job_type: str
    status: str
    counters: dict[str, int]
    skips: list[dict[str, str]]


class DepartmentResponse(BaseModel):
    department_id: uuid.UUID
    department_key: str
    name: str
    domain: str
    is_active: bool


class DepartmentListResponse(BaseModel):
    items: list[DepartmentResponse]


class WorkTimelineEventResponse(BaseModel):
    event_id: uuid.UUID
    work_id: uuid.UUID
    event_type: str
    event_at: datetime
    source_timestamp: datetime | None
    ingested_at: datetime
    processed_at: datetime
    actor_id: uuid.UUID | None
    actor_type: str
    previous_state: dict[str, Any] | None
    new_state: dict[str, Any] | None
    source_reference: dict[str, Any] | None
    provenance: str
    correlation_id: str


class WorkTimelineResponse(BaseModel):
    items: list[WorkTimelineEventResponse]


class WorkSummaryItem(BaseModel):
    work_id: uuid.UUID
    title: str
    work_type: str
    domain: str
    status: str
    priority: str
    due_at: datetime | None
    overdue: bool
    due_soon: bool
    blocked: bool
    owner_id: uuid.UUID | None
    department_id: uuid.UUID | None
    updated_at: datetime


class MyWorkResponse(BaseModel):
    as_of: datetime
    my_work: list[WorkSummaryItem]
    overdue: list[WorkSummaryItem]
    due_soon: list[WorkSummaryItem]
    waiting_for_me: list[WorkSummaryItem]
    waiting_on_others: list[WorkSummaryItem]
    counts: dict[str, int]


class DepartmentOverviewResponse(BaseModel):
    department_id: uuid.UUID | None
    as_of: datetime
    status_counts: dict[str, int]
    open_work: int
    overdue: int
    blocked: int
    unassigned: int
    items: list[WorkSummaryItem]


class ExecutiveOverviewResponse(BaseModel):
    as_of: datetime
    critical_work: list[WorkSummaryItem]
    overdue_high_priority: list[WorkSummaryItem]
    blocked_items: list[WorkSummaryItem]
    counts: dict[str, int]


class ExtractionResponse(BaseModel):
    extraction_id: uuid.UUID
    extraction_key: str
    canonical_record_id: uuid.UUID | None
    source_reference: str | None
    extraction_method: str
    extraction_version: str
    candidate_type: str
    fields: dict[str, Any]
    confidence: float
    verification_status: str
    promoted_work_id: uuid.UUID | None
    created_at: datetime


class ExtractionListResponse(BaseModel):
    items: list[ExtractionResponse]
    total: int
    limit: int
    offset: int


class WorkflowStepResponse(BaseModel):
    step_key: str
    step_name: str
    step_order: int
    responsible_role: str | None
    observed_state: str
    depends_on: list[str]


class WorkflowInstanceResponse(BaseModel):
    instance_id: uuid.UUID
    definition_id: uuid.UUID
    work_id: uuid.UUID
    workflow_key: str
    workflow_name: str
    workflow_type: str
    version: int
    status: str
    current_step_key: str | None
    steps: list[WorkflowStepResponse]


class WorkflowInstanceListResponse(BaseModel):
    items: list[WorkflowInstanceResponse]
    total: int
