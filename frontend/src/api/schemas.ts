/**
 * Runtime validation for API responses.
 *
 * TypeScript types vanish at runtime, so every response is parsed before the UI trusts
 * it. A shape mismatch becomes a handled error instead of a render crash, and no
 * unexpected field is ever rendered.
 */

import { z } from "zod";

export const capabilitiesSchema = z.record(z.string(), z.boolean());

export const currentUserSchema = z.object({
  user_id: z.string(),
  display_name: z.string().nullable(),
  email: z.string().nullable(),
  organization_id: z.string(),
  organization_name: z.string(),
  role: z.string(),
  permissions: z.array(z.string()),
  auth_method: z.string(),
});

export const configFieldSchema = z.object({
  name: z.string(),
  label: z.string(),
  field_type: z.string(),
  required: z.boolean(),
  help_text: z.string(),
  placeholder: z.string().nullable(),
  choices: z.array(z.string()),
  max_length: z.number().nullable(),
  default: z.unknown(),
  is_secret: z.boolean(),
});

export const connectorSchema = z.object({
  connector_type: z.string(),
  display_name: z.string(),
  description: z.string(),
  vendor: z.string(),
  version: z.string(),
  auth_modes: z.array(z.string()),
  capabilities: capabilitiesSchema,
  availability: z.string(),
  unavailable_reason: z.string().nullable(),
  required_setup: z.array(z.string()),
  documentation_url: z.string().nullable(),
  record_types: z.array(z.string()),
  synthetic_only: z.boolean(),
  config_fields: z.array(configFieldSchema),
});

export const connectorListSchema = z.array(connectorSchema);

export const connectionSchema = z.object({
  id: z.string(),
  connector_type: z.string(),
  name: z.string(),
  description: z.string().nullable(),
  config: z.record(z.string(), z.unknown()),
  has_credentials: z.boolean(),
  status: z.string(),
  enabled: z.boolean(),
  capabilities: capabilitiesSchema,
  last_tested_at: z.string().nullable(),
  last_success_at: z.string().nullable(),
  last_error_code: z.string().nullable(),
  created_at: z.string(),
  updated_at: z.string(),
});

export const connectionListSchema = z.object({
  items: z.array(connectionSchema),
  total: z.number(),
  limit: z.number(),
  offset: z.number(),
});

export const connectionTestResultSchema = z.object({
  ok: z.boolean(),
  message: z.string(),
  error_code: z.string().nullable(),
  latency_ms: z.number().nullable(),
  details: z.record(z.string(), z.unknown()),
});

export const syncJobSchema = z.object({
  id: z.string(),
  connection_id: z.string(),
  status: z.string(),
  attempt: z.number(),
  records_fetched: z.number(),
  records_persisted: z.number(),
  records_delivered: z.number(),
  records_failed: z.number(),
  error_code: z.string().nullable(),
  error_message: z.string().nullable(),
  correlation_id: z.string(),
  queued_at: z.string(),
  started_at: z.string().nullable(),
  finished_at: z.string().nullable(),
  duration_ms: z.number().nullable(),
  cancel_requested: z.boolean(),
});

export const syncAcceptedSchema = z.object({
  job: syncJobSchema,
  queue_job_id: z.string().nullable(),
});

export const syncJobListSchema = z.object({
  items: z.array(syncJobSchema),
  total: z.number(),
  limit: z.number(),
  offset: z.number(),
});

export const connectionStatusSchema = z.object({
  connection_id: z.string(),
  status: z.string(),
  enabled: z.boolean(),
  last_tested_at: z.string().nullable(),
  last_success_at: z.string().nullable(),
  last_error_code: z.string().nullable(),
  record_count: z.number(),
  last_job: syncJobSchema.nullable(),
  connector_capabilities: capabilitiesSchema,
});

export const sourceRecordSchema = z.object({
  id: z.string(),
  connector_type: z.string(),
  source_record_id: z.string(),
  record_type: z.string(),
  schema_version: z.string(),
  source_timestamp: z.string().nullable(),
  captured_at: z.string(),
  content_hash: z.string(),
  payload: z.record(z.string(), z.unknown()),
  payload_truncated: z.boolean(),
  metadata: z.record(z.string(), z.unknown()),
  untrusted: z.boolean(),
});

export const sourceRecordListSchema = z.object({
  items: z.array(sourceRecordSchema),
  total: z.number(),
  limit: z.number(),
  offset: z.number(),
  notice: z.string(),
});

export const auditEventSchema = z.object({
  id: z.string(),
  actor_id: z.string().nullable(),
  actor_type: z.string(),
  action: z.string(),
  target_type: z.string().nullable(),
  target_id: z.string().nullable(),
  outcome: z.string(),
  correlation_id: z.string().nullable(),
  metadata: z.record(z.string(), z.unknown()),
  created_at: z.string(),
});

export const auditEventListSchema = z.object({
  items: z.array(auditEventSchema),
  total: z.number(),
  limit: z.number(),
  offset: z.number(),
});

export const overviewSchema = z.object({
  organization_id: z.string(),
  organization_name: z.string(),
  connections_total: z.number(),
  connections_active: z.number(),
  connections_error: z.number(),
  connections_disabled: z.number(),
  connector_types_available: z.number(),
  connector_types_not_configured: z.number(),
  records_captured: z.number(),
  last_sync: syncJobSchema.nullable(),
  recent_jobs: z.array(syncJobSchema),
  ingestion_sink: z.string(),
  layer2_configured: z.boolean(),
});

// --- Layer 4: work intelligence ---------------------------------------------------

export const workSourceReferenceSchema = z.object({
  canonical_record_id: z.string(),
  source_system: z.string(),
  source_record_key: z.string(),
  source_record_type: z.string(),
  relationship: z.enum(["primary", "supporting", "mentioned"]),
  confidence: z.number().nullable(),
});

export const workAssignmentSchema = z.object({
  participant_type: z.string(),
  user_id: z.string().nullable(),
  department_id: z.string().nullable(),
  external_name: z.string().nullable(),
  role: z.string(),
  verification: z.string(),
  confidence: z.number().nullable(),
});

export const workPrioritySchema = z.object({
  level: z.string(),
  source: z.string(),
  overdue: z.boolean(),
  due_soon: z.boolean(),
  blocked: z.boolean(),
  dependency_count: z.number(),
  waiting_duration_seconds: z.number().nullable(),
  commitment_status: z.string().nullable(),
  source_completeness: z.number(),
  related_detection_ids: z.array(z.string()),
  computed_at: z.string().nullable(),
});

export const workEntitySchema = z.object({
  work_id: z.string(),
  work_key: z.string(),
  work_type: z.string(),
  title: z.string(),
  description: z.string().nullable(),
  domain: z.string(),
  department_id: z.string().nullable(),
  owner_id: z.string().nullable(),
  assigned_user_id: z.string().nullable(),
  priority: z.string(),
  priority_source: z.string(),
  status: z.string(),
  source_status: z.string().nullable(),
  origin: z.string(),
  confidence: z.number().nullable(),
  start_at: z.string().nullable(),
  due_at: z.string().nullable(),
  due_source: z.string().nullable(),
  completed_at: z.string().nullable(),
  customer_ref: z.string().nullable(),
  supplier_ref: z.string().nullable(),
  order_ref: z.string().nullable(),
  project_ref: z.string().nullable(),
  machine_ref: z.string().nullable(),
  production_ref: z.string().nullable(),
  attributes: z.record(z.string(), z.unknown()),
  schema_version: z.string(),
  created_at: z.string(),
  updated_at: z.string(),
});

export const workTimelineEventSchema = z.object({
  event_id: z.string(),
  work_id: z.string(),
  event_type: z.string(),
  event_at: z.string(),
  source_timestamp: z.string().nullable(),
  ingested_at: z.string(),
  processed_at: z.string(),
  actor_id: z.string().nullable(),
  actor_type: z.string(),
  previous_state: z.record(z.string(), z.unknown()).nullable(),
  new_state: z.record(z.string(), z.unknown()).nullable(),
  source_reference: z.record(z.string(), z.unknown()).nullable(),
  provenance: z.string(),
  correlation_id: z.string(),
});

export const workCommitmentSchema = z.object({
  commitment_id: z.string(),
  work_id: z.string().nullable(),
  commitment_type: z.string(),
  description: z.string(),
  committing_party: z.string(),
  receiving_party: z.string(),
  responsible_owner: z.string().nullable(),
  related_customer_ref: z.string().nullable(),
  related_supplier_ref: z.string().nullable(),
  related_order_ref: z.string().nullable(),
  promised_at: z.string().nullable(),
  due_at: z.string().nullable(),
  actual_completion_at: z.string().nullable(),
  commitment_status: z.string(),
  verification_status: z.string(),
  confidence: z.number().nullable(),
  source_reference: z.string().nullable(),
  created_at: z.string(),
  updated_at: z.string(),
});

export const workDependencySchema = z.object({
  dependency_id: z.string(),
  source_work_id: z.string(),
  target_work_id: z.string().nullable(),
  target_entity_type: z.string(),
  target_entity_ref: z.string().nullable(),
  relationship: z.string(),
  status: z.string(),
  origin: z.string(),
  verification_status: z.string(),
  created_at: z.string(),
  updated_at: z.string(),
});

export const workBlockerSchema = z.object({
  blocker_id: z.string(),
  affected_work_id: z.string(),
  blocker_type: z.string(),
  description: z.string(),
  blocking_entity: z.string().nullable(),
  responsible_department_id: z.string().nullable(),
  detected_at: z.string(),
  expected_resolution_at: z.string().nullable(),
  blocker_status: z.string(),
  evidence_references: z.array(z.record(z.string(), z.unknown())),
  provenance: z.string(),
  verification_status: z.string(),
});

export const workFollowUpSchema = z.object({
  follow_up_id: z.string(),
  related_work_id: z.string(),
  target_type: z.string(),
  target_ref: z.string(),
  owner_id: z.string().nullable(),
  reason: z.string(),
  last_activity_at: z.string().nullable(),
  next_due_at: z.string().nullable(),
  urgency: z.string(),
  follow_up_status: z.string(),
  rule_id: z.string(),
  provenance: z.string(),
});

export const workConflictSchema = z.object({
  conflict_id: z.string(),
  work_id: z.string().nullable(),
  entity_type: z.string(),
  entity_key: z.string(),
  conflict_type: z.string(),
  detail: z.string(),
  candidates: z.array(z.record(z.string(), z.unknown())),
  status: z.string(),
  resolved_value: z.record(z.string(), z.unknown()).nullable(),
  resolved_by: z.string().nullable(),
  resolution_note: z.string().nullable(),
  detected_at: z.string(),
  resolved_at: z.string().nullable(),
});

export const workSummaryItemSchema = z.object({
  work_id: z.string(),
  title: z.string(),
  work_type: z.string(),
  domain: z.string(),
  status: z.string(),
  priority: z.string(),
  due_at: z.string().nullable(),
  overdue: z.boolean(),
  due_soon: z.boolean(),
  blocked: z.boolean(),
  owner_id: z.string().nullable(),
  department_id: z.string().nullable(),
  updated_at: z.string(),
});

export const workListSchema = z.object({
  items: z.array(workEntitySchema),
  total: z.number(),
  limit: z.number(),
  offset: z.number(),
});

export const workDetailSchema = workEntitySchema.extend({
  source_references: z.array(workSourceReferenceSchema),
  assignments: z.array(workAssignmentSchema),
  commitments: z.array(workCommitmentSchema),
  dependencies: z.array(workDependencySchema),
  blockers: z.array(workBlockerSchema),
  priorities: z.array(workPrioritySchema),
  timeline: z.array(workTimelineEventSchema),
});

export const myWorkSchema = z.object({
  as_of: z.string(),
  my_work: z.array(workSummaryItemSchema),
  overdue: z.array(workSummaryItemSchema),
  due_soon: z.array(workSummaryItemSchema),
  waiting_for_me: z.array(workSummaryItemSchema),
  waiting_on_others: z.array(workSummaryItemSchema),
  counts: z.record(z.string(), z.number()),
});

export const departmentSchema = z.object({
  department_id: z.string(),
  department_key: z.string(),
  name: z.string(),
  domain: z.string(),
  is_active: z.boolean(),
});

export const departmentListSchema = z.object({
  items: z.array(departmentSchema),
});

export const departmentOverviewSchema = z.object({
  department_id: z.string().nullable(),
  as_of: z.string(),
  status_counts: z.record(z.string(), z.number()),
  open_work: z.number(),
  overdue: z.number(),
  blocked: z.number(),
  unassigned: z.number(),
  items: z.array(workSummaryItemSchema),
});

export const executiveOverviewSchema = z.object({
  as_of: z.string(),
  critical_work: z.array(workSummaryItemSchema),
  overdue_high_priority: z.array(workSummaryItemSchema),
  blocked_items: z.array(workSummaryItemSchema),
  counts: z.record(z.string(), z.number()),
});

export const commitmentListSchema = z.object({
  items: z.array(workCommitmentSchema),
  total: z.number(),
  limit: z.number(),
  offset: z.number(),
});

export const dependencyListSchema = z.object({
  items: z.array(workDependencySchema),
  total: z.number(),
  limit: z.number(),
  offset: z.number(),
});

export const blockerListSchema = z.object({
  items: z.array(workBlockerSchema),
  total: z.number(),
  limit: z.number(),
  offset: z.number(),
});

export const followUpListSchema = z.object({
  items: z.array(workFollowUpSchema),
  total: z.number(),
  limit: z.number(),
  offset: z.number(),
});

export const conflictListSchema = z.object({
  items: z.array(workConflictSchema),
  total: z.number(),
  limit: z.number(),
  offset: z.number(),
});

export const workflowStepSchema = z.object({
  step_key: z.string(),
  step_name: z.string(),
  step_order: z.number(),
  responsible_role: z.string().nullable(),
  observed_state: z.string(),
  depends_on: z.array(z.string()),
});

export const workflowInstanceSchema = z.object({
  instance_id: z.string(),
  definition_id: z.string(),
  work_id: z.string(),
  workflow_key: z.string(),
  workflow_name: z.string(),
  workflow_type: z.string(),
  version: z.number(),
  status: z.string(),
  current_step_key: z.string().nullable(),
  steps: z.array(workflowStepSchema),
});

export const workflowInstanceListSchema = z.object({
  items: z.array(workflowInstanceSchema),
  total: z.number(),
});

export const workRunSchema = z.object({
  organization_id: z.string(),
  correlation_id: z.string(),
  job_type: z.string(),
  status: z.string(),
  counters: z.record(z.string(), z.number()),
  skips: z.array(z.record(z.string(), z.string())),
});
