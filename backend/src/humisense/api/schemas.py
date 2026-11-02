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
