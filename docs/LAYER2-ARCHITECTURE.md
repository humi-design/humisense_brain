# Humisense Brain — Layer 2: Data Ingestion & Normalization Engine

Status: implementation of milestone Layer 2, built on the released Layer 1
(`v0.1.0-rc.1`).
Audience: engineering, data architecture, security review, platform operations.

## 0. Pre-implementation findings (what actually exists)

Layer 1 was inspected before any code was written. The following are facts, verified in the
repository, not assumptions from documentation:

| Area | Verified reality |
| --- | --- |
| Handoff contract | `connectors/types.py` defines `SourceRecordEnvelope` (versioned, `SCHEMA_VERSION = "1.0"`), `IngestionBatch`, `IngestionAck`. `services/ingestion.py` defines the sink protocol and an `HttpIngestionSink` that POSTs to `INGEST_PATH = "/ingest/v1/records"`. |
| Idempotency key | `SourceRecordEnvelope.idempotency_key()` returns `connection_id:source_record_id:content_hash`. |
| Delivery semantics | Declared **at-least-once**; exactly-once is explicitly not claimed. |
| Persistence | `source_records` (raw payload JSONB, unique on `(connection_id, source_record_id, content_hash)`), `ingestion_receipts`, `sync_jobs`, `connections`. |
| Tenant isolation | `organizations`/`memberships`; `db/rls.py` applies `ENABLE` + `FORCE` RLS with `app_current_org()` reading a transaction-local GUC. `db/session.py` provides `tenant_session` / `system_session`. |
| Authorization | `security/permissions.py` — deny-by-default role matrix, enforced in both the API dependency layer and the services. |
| Queue | `arq` over Redis (`workers/queue.py`), `workers/worker.py` runs `run_sync_job` and a stale-job reaper. Payload carries only identifiers. |
| Retry | `services/ingestion.py` `RetryPolicy` — bounded attempts, exponential backoff with full jitter; `errors.is_retryable` classifies transient vs permanent. |
| Audit | `audit_events`, append-only enforced by a database trigger. |
| Metrics | `observability/metrics.py` — Prometheus counters/histograms; tenant ids are never labels. |
| Stack versions | Python ≥3.12,<3.14; FastAPI; SQLAlchemy 2.0 async; asyncpg; Alembic; arq; PostgreSQL; Pydantic v2. |
| Tests | pytest + pytest-asyncio against a **real** PostgreSQL database; each test truncates the schema; migrations are applied by the suite. `@pytest.mark.security` marks security acceptance tests. |
| Layer 1 gate | Release candidate `v0.1.0-rc.1`, approved for a controlled single-tenant pilot. Not blocked. |

**Reuse decision.** Layer 2 reuses, without change: the `SourceRecordEnvelope` contract, the
`IngestionBatch`/`IngestionAck` shapes, the queue, the audit service, the metrics registry,
the RLS/session machinery, the error taxonomy, and the permission catalogue. Layer 2 adds a
new package, `humisense/layer2/`, and its own tables. Layer 1 is modified in exactly two
places, both additive: a new sink mode so a single-node deployment can hand off in-process,
and a new permission set.

## 1. Scope

Layer 2 turns captured source records into trustworthy, traceable, tenant-safe canonical
records and publishes them downstream. It does **not** reason, summarize, detect anomalies,
build context graphs, or take actions. Ingested content is preserved as data and never
executed as instructions.

Pipeline (each stage independently testable, traceable, recoverable):

```
SOURCE → LAYER 1 CONNECTOR → INGESTION RECEIVER → VALIDATION → SOURCE PRESERVATION
      → NORMALIZATION → DEDUPLICATION → ENRICHMENT METADATA → CANONICAL RECORD
      → DURABLE STORAGE → TRANSACTIONAL OUTBOX → DOWNSTREAM EVENT
```

## 2. Data contract

`humisense/layer2/contract.py` defines the versioned canonical record (`CANONICAL_SCHEMA_VERSION
= "2.0"`). It carries, where applicable: tenant, source system, connector type, connection id,
source record id, record id, record/entity/event type, the four timestamps below, payload and
normalized payload, metadata, canonical and source schema versions, correlation id,
idempotency key, ingestion status, processing version, and provenance.

Four distinct timestamps, never conflated:

| Field | Meaning |
| --- | --- |
| `source_timestamp` | The value the source system reported. Never rewritten. |
| `event_timestamp` | When the event occurred, when the source distinguishes it from the sample time. |
| `observed_timestamp` | When Layer 1 captured the record (`captured_at`). |
| `received_timestamp` | When Layer 2 durably accepted the record. |
| `processed_timestamp` | When Layer 2 finished normalizing and storing it. |

Timestamps are stored as timezone-aware UTC. The original timezone and any source time
metadata are retained in `source_timezone` and `source_time_metadata`; the raw value is
never destroyed. A timestamp the source did not provide is left null, never inferred.

Schema evolution: canonical records carry both `schema_version` (canonical) and
`source_schema_version` (the Layer 1 envelope version). The receiver accepts envelope
versions it knows; an unknown version is quarantined rather than silently coerced.

## 3. Storage design

PostgreSQL only. No separate time-series store is introduced: the brief requires a benchmark
before adopting one, and none is justified at the current workload (see §9). JSONB holds
extensible payloads; strongly typed columns hold identity, timestamps, and state.

New tables (all tenant-owned, all under RLS):

| Table | Purpose | Mutability |
| --- | --- | --- |
| `ingest_batches` | One row per received batch, with per-batch counters and status. | Mutable counters |
| `raw_records` | Immutable preservation of the original source payload. | Append-only; `UPDATE` rejected by trigger, `DELETE` allowed for retention |
| `canonical_records` | The normalized, deduplicated canonical record with its processing state. | Mutable state/version columns |
| `record_quarantine` | Records that failed a stage, with bounded error detail and a payload preview. | Mutable resolution columns |
| `outbox_events` | Transactional outbox for downstream publication. | State transitions only |
| `record_processing_events` | Append-only ledger of processing-state transitions. | Append-only |

Indexes are chosen from query patterns (list by tenant, by connection, by source timestamp,
by record type, by state), not speculatively. Every new tenant table is added to
`db/rls.py::RLS_TABLES`.

## 4. Validation and normalization

Validation (`layer2/validation.py`) is policy-driven per data family — time-series/historian,
business, and generic structured — with structured error codes and per-record isolation so a
malformed record cannot abort a batch.

Normalization (`layer2/normalizers/`) uses a **static registry** populated at import time
from an explicit allowlist, mirroring the connector registry. No normalizer is imported from
user-controlled input. Each normalizer declares its supported record types and input/output
schema versions, validates before transforming, preserves the source, produces deterministic
output, records its transformation version, and returns structured errors.

Unit handling: a conversion never happens silently. The canonical record records the original
value, original unit, normalized value, normalized unit, and the conversion rule applied.
Missing values are never invented and no interpolation is performed in this layer.

## 5. Idempotency and delivery

Deduplication is tenant-scoped and deterministic: `raw_records` is unique on
`(organization_id, connection_id, source_record_id, content_hash)`. Content hashes alone are
never the identity, because two legitimate records can share content; the source record id is
part of the key. Four cases are distinguished: exact duplicate delivery (same key),
updated source record (same source id, new content hash → new raw row, supersedes the
previous canonical record), new source record, and conflicting source version (same source
version marker, different content → quarantined). Cross-tenant deduplication is impossible
by construction. At-least-once is the claimed semantic; exactly-once is not.

## 6. Reliability

`layer2/states.py` defines the processing states and the legal transitions between them:

```
RECEIVED → VALIDATING → VALIDATED → NORMALIZING → NORMALIZED → STORED → PUBLISHED
   ↘ FAILED   ↘ QUARANTINED   ↘ RETRY_PENDING (→ VALIDATING/NORMALIZING)   ↘ FAILED
```

The outbox is the publication boundary: a canonical record is written and its outbox event
inserted in the **same transaction**, so publication is never claimed before it is durable.
The publisher moves `pending → published` only after the downstream sink confirms. Retries
are bounded with exponential backoff and jitter; exhausted retries quarantine the record.
Replay is an explicit, authorized, audited operation.

## 7. Security

Reuses Layer 1 boundaries: RLS on every tenant table, deny-by-default permissions (new:
`ingestion:read`, `ingestion:replay`, `quarantine:read`, `quarantine:manage`), strict schema
validation, payload size and complexity limits, secrets never logged, audit events for
sensitive operations. The receiver authenticates service-to-service calls with a bearer
token compared in constant time. All ingested content is marked untrusted and preserved as
data. Layer 2 has no capability to execute commands, send messages, or write to business
systems.

## 8. Interfaces

- `POST /ingest/v1/records` — receive a batch from Layer 1 (service auth).
- `GET /ingestion/batches`, `GET /ingestion/batches/{id}` — batch status.
- `GET /ingestion/records`, `GET /ingestion/records/{id}` — canonical records (no raw
  payload by default).
- `GET /ingestion/quarantine`, `POST /ingestion/quarantine/{id}/replay` — quarantine review
  and authorized replay.
- `GET /ingestion/health` — pipeline health (counts and lag, no tenant data).

## 9. Performance

Benchmarked with the synthetic historian fixture and generated CSVs. Results, hardware, and
dataset sizes are recorded in `docs/LAYER2-RELEASE.md`. Bounded memory: batches are processed
in bounded chunks; no unbounded buffering. A separate time-series store is **not** adopted;
the benchmark informs the future decision.

## 10. Explicitly out of scope

Anomaly detection, predictive maintenance, BI, AI summaries, memory, context graphs,
reasoning, investigation, planning, agent orchestration, autonomous actions, and any write to
an external business system. Those belong to Layers 3+.
