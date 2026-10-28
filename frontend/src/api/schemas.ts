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
