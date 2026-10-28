/**
 * Wire types for the Layer 1 API.
 *
 * These mirror the FastAPI response models in `backend/src/humisense/api/schemas.py`.
 * They are intentionally hand-written rather than generated so that the frontend has to
 * be updated deliberately when the contract changes.
 */

export interface ErrorBody {
  code: string;
  message: string;
  correlation_id: string | null;
  details: Record<string, unknown> | null;
}

export interface ErrorResponse {
  error: ErrorBody;
}

export type HealthStatus = "ok" | "degraded";

export interface HealthResponse {
  status: HealthStatus;
  version: string;
}

export interface ReadinessResponse {
  status: "ready" | "degraded";
  database: "ok" | "unavailable";
  queue: "ok" | "unavailable" | "not_required";
  rate_limiter: "ok" | "degraded";
  connectors_loaded: number;
}

export interface CurrentUser {
  user_id: string;
  display_name: string | null;
  email: string | null;
  organization_id: string;
  organization_name: string;
  role: string;
  permissions: string[];
  auth_method: string;
}

export interface ConfigField {
  name: string;
  label: string;
  field_type: string;
  required: boolean;
  help_text: string;
  placeholder: string | null;
  choices: string[];
  max_length: number | null;
  default: unknown;
  is_secret: boolean;
}

export type Capabilities = Record<string, boolean>;

export interface Connector {
  connector_type: string;
  display_name: string;
  description: string;
  vendor: string;
  version: string;
  auth_modes: string[];
  capabilities: Capabilities;
  availability: string;
  unavailable_reason: string | null;
  required_setup: string[];
  documentation_url: string | null;
  record_types: string[];
  synthetic_only: boolean;
  config_fields: ConfigField[];
}

export interface Connection {
  id: string;
  connector_type: string;
  name: string;
  description: string | null;
  config: Record<string, unknown>;
  has_credentials: boolean;
  status: string;
  enabled: boolean;
  capabilities: Capabilities;
  last_tested_at: string | null;
  last_success_at: string | null;
  last_error_code: string | null;
  created_at: string;
  updated_at: string;
}

export interface ConnectionList {
  items: Connection[];
  total: number;
  limit: number;
  offset: number;
}

export interface ConnectionCreateRequest {
  connector_type: string;
  name: string;
  description?: string | null;
  config: Record<string, unknown>;
  credentials: Record<string, unknown>;
}

export interface ConnectionTestResult {
  ok: boolean;
  message: string;
  error_code: string | null;
  latency_ms: number | null;
  details: Record<string, unknown>;
}

export interface SyncJob {
  id: string;
  connection_id: string;
  status: string;
  attempt: number;
  records_fetched: number;
  records_persisted: number;
  records_delivered: number;
  records_failed: number;
  error_code: string | null;
  error_message: string | null;
  correlation_id: string;
  queued_at: string;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  cancel_requested: boolean;
}

export interface SyncAccepted {
  job: SyncJob;
  queue_job_id: string | null;
}

export interface SyncJobList {
  items: SyncJob[];
  total: number;
  limit: number;
  offset: number;
}

export interface ConnectionStatus {
  connection_id: string;
  status: string;
  enabled: boolean;
  last_tested_at: string | null;
  last_success_at: string | null;
  last_error_code: string | null;
  record_count: number;
  last_job: SyncJob | null;
  connector_capabilities: Capabilities;
}

export interface SourceRecord {
  id: string;
  connector_type: string;
  source_record_id: string;
  record_type: string;
  schema_version: string;
  source_timestamp: string | null;
  captured_at: string;
  content_hash: string;
  payload: Record<string, unknown>;
  payload_truncated: boolean;
  metadata: Record<string, unknown>;
  untrusted: boolean;
}

export interface SourceRecordList {
  items: SourceRecord[];
  total: number;
  limit: number;
  offset: number;
  notice: string;
}

export interface AuditEvent {
  id: string;
  actor_id: string | null;
  actor_type: string;
  action: string;
  target_type: string | null;
  target_id: string | null;
  outcome: string;
  correlation_id: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface AuditEventList {
  items: AuditEvent[];
  total: number;
  limit: number;
  offset: number;
}

export interface Overview {
  organization_id: string;
  organization_name: string;
  connections_total: number;
  connections_active: number;
  connections_error: number;
  connections_disabled: number;
  connector_types_available: number;
  connector_types_not_configured: number;
  records_captured: number;
  last_sync: SyncJob | null;
  recent_jobs: SyncJob[];
  ingestion_sink: string;
  layer2_configured: boolean;
}
