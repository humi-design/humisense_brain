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

// --- Layer 4: work intelligence ---------------------------------------------------
//
// These mirror the Layer 4 response models. A derived value always carries its provenance
// and confidence, and an unresolved field is `null`, never a fabricated value. Nothing
// here is a performance score: the counts are counts.

export interface WorkSourceReference {
  canonical_record_id: string;
  source_system: string;
  source_record_key: string;
  source_record_type: string;
  relationship: "primary" | "supporting" | "mentioned";
  confidence: number | null;
}

export interface WorkAssignment {
  participant_type: string;
  user_id: string | null;
  department_id: string | null;
  external_name: string | null;
  role: string;
  verification: string;
  confidence: number | null;
}

export interface WorkPriority {
  level: string;
  source: string;
  overdue: boolean;
  due_soon: boolean;
  blocked: boolean;
  dependency_count: number;
  waiting_duration_seconds: number | null;
  commitment_status: string | null;
  source_completeness: number;
  related_detection_ids: string[];
  computed_at: string | null;
}

export interface WorkEntity {
  work_id: string;
  work_key: string;
  work_type: string;
  title: string;
  description: string | null;
  domain: string;
  department_id: string | null;
  owner_id: string | null;
  assigned_user_id: string | null;
  priority: string;
  priority_source: string;
  status: string;
  source_status: string | null;
  origin: string;
  confidence: number | null;
  start_at: string | null;
  due_at: string | null;
  due_source: string | null;
  completed_at: string | null;
  customer_ref: string | null;
  supplier_ref: string | null;
  order_ref: string | null;
  project_ref: string | null;
  machine_ref: string | null;
  production_ref: string | null;
  attributes: Record<string, unknown>;
  schema_version: string;
  created_at: string;
  updated_at: string;
}

export interface WorkTimelineEvent {
  event_id: string;
  work_id: string;
  event_type: string;
  event_at: string;
  source_timestamp: string | null;
  ingested_at: string;
  processed_at: string;
  actor_id: string | null;
  actor_type: string;
  previous_state: Record<string, unknown> | null;
  new_state: Record<string, unknown> | null;
  source_reference: Record<string, unknown> | null;
  provenance: string;
  correlation_id: string;
}

export interface WorkCommitment {
  commitment_id: string;
  work_id: string | null;
  commitment_type: string;
  description: string;
  committing_party: string;
  receiving_party: string;
  responsible_owner: string | null;
  related_customer_ref: string | null;
  related_supplier_ref: string | null;
  related_order_ref: string | null;
  promised_at: string | null;
  due_at: string | null;
  actual_completion_at: string | null;
  commitment_status: string;
  verification_status: string;
  confidence: number | null;
  source_reference: string | null;
  created_at: string;
  updated_at: string;
}

export interface WorkDependency {
  dependency_id: string;
  source_work_id: string;
  target_work_id: string | null;
  target_entity_type: string;
  target_entity_ref: string | null;
  relationship: string;
  status: string;
  origin: string;
  verification_status: string;
  created_at: string;
  updated_at: string;
}

export interface WorkBlocker {
  blocker_id: string;
  affected_work_id: string;
  blocker_type: string;
  description: string;
  blocking_entity: string | null;
  responsible_department_id: string | null;
  detected_at: string;
  expected_resolution_at: string | null;
  blocker_status: string;
  evidence_references: Record<string, unknown>[];
  provenance: string;
  verification_status: string;
}

export interface WorkFollowUp {
  follow_up_id: string;
  related_work_id: string;
  target_type: string;
  target_ref: string;
  owner_id: string | null;
  reason: string;
  last_activity_at: string | null;
  next_due_at: string | null;
  urgency: string;
  follow_up_status: string;
  rule_id: string;
  provenance: string;
}

export interface WorkConflict {
  conflict_id: string;
  work_id: string | null;
  entity_type: string;
  entity_key: string;
  conflict_type: string;
  detail: string;
  candidates: Record<string, unknown>[];
  status: string;
  resolved_value: Record<string, unknown> | null;
  resolved_by: string | null;
  resolution_note: string | null;
  detected_at: string;
  resolved_at: string | null;
}

export interface WorkSummaryItem {
  work_id: string;
  title: string;
  work_type: string;
  domain: string;
  status: string;
  priority: string;
  due_at: string | null;
  overdue: boolean;
  due_soon: boolean;
  blocked: boolean;
  owner_id: string | null;
  department_id: string | null;
  updated_at: string;
}

export interface WorkList {
  items: WorkEntity[];
  total: number;
  limit: number;
  offset: number;
}

export interface WorkDetail extends WorkEntity {
  source_references: WorkSourceReference[];
  assignments: WorkAssignment[];
  commitments: WorkCommitment[];
  dependencies: WorkDependency[];
  blockers: WorkBlocker[];
  priorities: WorkPriority[];
  timeline: WorkTimelineEvent[];
}

export interface MyWork {
  as_of: string;
  my_work: WorkSummaryItem[];
  overdue: WorkSummaryItem[];
  due_soon: WorkSummaryItem[];
  waiting_for_me: WorkSummaryItem[];
  waiting_on_others: WorkSummaryItem[];
  counts: Record<string, number>;
}

export interface Department {
  department_id: string;
  department_key: string;
  name: string;
  domain: string;
  is_active: boolean;
}

export interface DepartmentList {
  items: Department[];
}

export interface DepartmentOverview {
  department_id: string | null;
  as_of: string;
  status_counts: Record<string, number>;
  open_work: number;
  overdue: number;
  blocked: number;
  unassigned: number;
  items: WorkSummaryItem[];
}

export interface ExecutiveOverview {
  as_of: string;
  critical_work: WorkSummaryItem[];
  overdue_high_priority: WorkSummaryItem[];
  blocked_items: WorkSummaryItem[];
  counts: Record<string, number>;
}

export interface CommitmentList {
  items: WorkCommitment[];
  total: number;
  limit: number;
  offset: number;
}

export interface DependencyList {
  items: WorkDependency[];
  total: number;
  limit: number;
  offset: number;
}

export interface BlockerList {
  items: WorkBlocker[];
  total: number;
  limit: number;
  offset: number;
}

export interface FollowUpList {
  items: WorkFollowUp[];
  total: number;
  limit: number;
  offset: number;
}

export interface ConflictList {
  items: WorkConflict[];
  total: number;
  limit: number;
  offset: number;
}

export interface WorkflowStep {
  step_key: string;
  step_name: string;
  step_order: number;
  responsible_role: string | null;
  observed_state: string;
  depends_on: string[];
}

export interface WorkflowInstance {
  instance_id: string;
  definition_id: string;
  work_id: string;
  workflow_key: string;
  workflow_name: string;
  workflow_type: string;
  version: number;
  status: string;
  current_step_key: string | null;
  steps: WorkflowStep[];
}

export interface WorkflowInstanceList {
  items: WorkflowInstance[];
  total: number;
}

export interface WorkRun {
  organization_id: string;
  correlation_id: string;
  job_type: string;
  status: string;
  counters: Record<string, number>;
  skips: Record<string, string>[];
}
