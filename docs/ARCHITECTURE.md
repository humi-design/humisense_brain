# Humisense Brain / OS — Layer 1: Secure Connector & Sensory Platform

Status: implementation of milestone Layer 1.
Audience: engineering, security review, platform operations.

## 1. Scope

Layer 1 is the **sensory layer**. It connects to authorized source systems, captures
records, and transports them to Layer 2 using a stable, validated envelope. Layer 1 does
**not** normalize, deduplicate, reason, plan, remember, detect anomalies, or run
autonomous agents. Those belong to Layer 2+.

### In scope

- Multi-tenant identity, membership, roles, and permissions.
- Connector SDK, registry, manifests, and capability declaration.
- Tenant-owned connection instances with encrypted credentials.
- Secure CSV connector (end to end, tested).
- Canary Labs adapter that reports its true configuration state.
- Sync jobs with bounded retries, cancellation, checkpoints, and partial-failure reporting.
- Standard source-record envelope and Layer 2 handoff interface.
- Audit events, structured logs, metrics, correlation IDs.
- React + TypeScript operator interface.
- CI pipeline with tests, linting, type checking, and security scanning.

### Explicitly out of scope

- Downstream intelligence (Layer 2+).
- Write-back to connected business systems.
- Webhook ingestion from source systems.
- OAuth interactive connector authorization flows.
- Kubernetes or service decomposition.

## 2. Runtime topology

```
                    ┌──────────────────────────────────────────────┐
   Browser          │  React SPA (Vite, TS strict, Tailwind)        │
   (operator)  ───► │  Session cookie only. No tokens in storage.   │
                    └───────────────┬──────────────────────────────┘
                                    │ HTTPS, cookie auth + CSRF header
                                    ▼
                    ┌──────────────────────────────────────────────┐
                    │  Reverse proxy / TLS termination              │
                    │  (nginx or managed LB; trusted proxy list)    │
                    └───────────────┬──────────────────────────────┘
                                    ▼
   ┌─────────────────────────────────────────────────────────────────────────┐
   │  FastAPI application (modular monolith)                                 │
   │                                                                         │
   │  api/            routers, request/response schemas, error mapping       │
   │  security/       authn (OIDC verifier | dev issuer), authz, CSRF,       │
   │                  rate limiting, SSRF egress policy, path safety,        │
   │                  redaction, headers                                     │
   │  connectors/     SDK (base, types, manifest, registry) + implementations│
   │  services/       connection, sync, ingestion handoff, audit             │
   │  secrets/        SecretsProvider: EnvSecretsProvider | EncryptedDbStore │
   │  db/             SQLAlchemy 2.x models, session, RLS enforcement        │
   │  workers/        arq worker: sync jobs, retry policy, cancellation      │
   └───────┬───────────────────────┬───────────────────────┬─────────────────┘
           │                       │                       │
           ▼                       ▼                       ▼
   ┌───────────────┐       ┌───────────────┐       ┌──────────────────────┐
   │ PostgreSQL 16 │       │ Redis 7       │       │ Egress broker        │
   │ app DB        │       │ job queue,    │       │ (SSRF policy +       │
   │ + RLS         │       │ rate limits   │       │  allowlist)          │
   └───────────────┘       └───────────────┘       └───────┬──────────────┘
                                                            ▼
                                          ┌──────────────────────────────────┐
                                          │ External sources:                │
                                          │  - allowlisted CSV directories   │
                                          │  - Canary Labs historian (future)│
                                          │  - Layer 2 ingestion endpoint    │
                                          └──────────────────────────────────┘
```

### Module dependency rule

`api → services → connectors/secrets/db` and `connectors → SDK types only`.
Connectors never import `api` or `services`. The connector runtime receives an
`EgressPolicy` and a `SecretRef`; it never receives a database session, the tenant
model, or another tenant's credentials.

## 3. Trust boundaries

| # | Boundary | Untrusted side | Controls |
|---|----------|----------------|----------|
| B1 | Browser ↔ API | Browser, all request input | Session cookie auth, CSRF token, strict Pydantic validation, CORS allowlist, rate limits, size limits, security headers |
| B2 | API ↔ PostgreSQL | none (internal) | Credentials from secrets provider, RLS as defence in depth, least-privilege DB role, no public exposure |
| B3 | API ↔ Redis | none (internal) | No public exposure, no secrets in job payloads |
| B4 | App ↔ external source systems | Source system responses, source URLs | SSRF egress policy, destination allowlist, timeouts, size caps, redirect revalidation |
| B5 | App ↔ filesystem (CSV) | File paths, filenames, file contents | Root allowlist, canonical path check, symlink resolution, size/row/time caps, no execution |
| B6 | App ↔ Layer 2 | Layer 2 endpoint | Bearer token from secrets provider, bounded retry, idempotency key, no PII in URLs |
| B7 | Third-party connector code ↔ platform | Connector implementation | Allowlisted registry (no dynamic import), bounded resource use, scoped secret access, no DB session |
| B8 | Connector payloads ↔ LLM/agent (future) | Email/document/record content | Layer 1 only transports; payloads marked untrusted; never executed, never rendered as HTML |

## 4. Data model

```
organizations ──< memberships >── users
      │                              │
      │                              └──< sessions (dev mode only)
      ├──< connections ──< sync_jobs ──< source_records ──< ingestion_receipts
      │         │
      │         └── secrets (encrypted credential blobs, by secret_ref)
      └──< audit_events
```

- `organizations` — tenant. Every tenant-owned table carries `organization_id`.
- `users` — identity, keyed by `(issuer, subject)` from the verified token.
- `memberships` — `(user_id, organization_id, role)`; roles are `owner`, `admin`, `operator`, `viewer`.
- `connections` — a tenant's configured source instance: `connector_type`, non-secret
  `config` JSONB, `secret_ref`, `status`, `enabled`, capability snapshot, checkpoint.
- `sync_jobs` — a bounded unit of capture work with `status`, `attempt`, `cursor_before`,
  `cursor_after`, counters, `error_code`, `correlation_id`.
- `source_records` — captured records in the standard envelope; unique on
  `(connection_id, source_record_id, content_hash)` for idempotency.
- `ingestion_receipts` — Layer 2 acceptance record per source record.
- `audit_events` — append-only, DB-enforced immutability.
- `secrets` — authenticated-encrypted credential blobs, never returned after write.

## 5. Connector platform

Four distinct concepts, deliberately separated:

1. **Connector type** — a class implementing `BaseConnector`, registered in a static allowlist.
2. **Connector manifest** — declarative metadata: `type`, `display_name`, `description`,
   `auth_modes`, `config_schema`, `capabilities`, `availability`, `unavailable_reason`.
3. **Connection instance** — one tenant's configured source, referencing a `secret_ref`.
4. **Connector runtime** — the execution context handed to a connector: egress policy,
   resolved credentials, cancellation token, resource limits, structured logger.

Capabilities are truthful and machine-readable (`ConnectorCapabilities`). A connector
advertises `supports_write` / `supports_webhook` / `supports_search` /
`supports_incremental` / `supports_live` only when the implementation exists and is tested.
`get_capabilities()` is the single source of truth; the API and UI render what the
registry reports and nothing more.

### Contract

```python
class BaseConnector(ABC):
    manifest: ClassVar[ConnectorManifest]

    def validate_config(self, config: Mapping[str, Any]) -> ValidatedConfig
    async def test_connection(self, ctx: ConnectorContext) -> ConnectionTestResult
    def get_capabilities(self) -> ConnectorCapabilities
    async def fetch_records(self, ctx, cursor, limit) -> FetchPage
    async def health_check(self, ctx) -> HealthResult      # optional
    async def close(self) -> None
```

Results are structured dataclasses/Pydantic models, never bare dicts. Failures raise
`ConnectorError` subclasses with a machine-readable `code` and a safe `public_message`;
raw exception text is never returned to clients.

## 6. Record envelope

`SourceRecordEnvelope` fields: `schema_version`, `connector_type`, `connection_id`,
`organization_id`, `source_record_id`, `record_type`, `source_timestamp`, `captured_at`,
`payload`, `metadata`, `content_hash`, `correlation_id`.

Layer 1 preserves source meaning. It does not convert units, infer missing timestamps,
or normalize business semantics. `schema_version` is explicit so Layer 2 can evolve
independently.

Delivery is **at-least-once with idempotent processing**. Exactly-once is not claimed.
The idempotency key is `(connection_id, source_record_id, content_hash)`.

## 7. Execution boundary

Long-running external capture never runs inside a request handler. `POST .../sync`
enqueues an `arq` job on Redis and returns `202` with the job id. The worker:

1. Re-establishes tenant context and re-authorizes the operation.
2. Resolves the secret through the secrets provider.
3. Runs the connector under egress policy, timeouts, size caps, and cancellation.
4. Persists records and receipts in batches.
5. Emits audit events and metrics; updates the checkpoint only on success.

## 8. Layer 2 handoff

`IngestionClient` posts `IngestionBatch` to the configured Layer 2 endpoint:
`POST /ingest/v1/records`, bearer auth, `Idempotency-Key` header, bounded retries with
exponential backoff and jitter, classification of transient vs permanent errors.
`InProcessIngestionSink` and `HttpIngestionSink` are the two implementations;
`LocalTestIngestionSink` in `backend/src/humisense/services/ingestion.py` is clearly marked
a test implementation.

Ownership split: Layer 1 owns capture, envelope validation, transport, retries, and
provenance. Layer 2 owns normalization, deduplication, semantic mapping, and retention
of normalized data.

## 9. Observability

- Structured JSON logs with `correlation_id`, `organization_id`, `actor_id`, `action`.
- `RedactionFilter` scrubs known credential keys and bearer/token patterns.
- Prometheus metrics: sync counts/duration/failures/retries, records ingested,
  queue depth. No payload values as label values.
  Note: metric definitions exist and are recorded in-process, but no scrape endpoint
  (for example `/metrics`) is exposed yet, and `METRICS_ENABLED` is not yet consulted.
  Metrics are therefore not observable from outside the process until an authenticated
  scrape route is added.
- OpenTelemetry-compatible tracing hooks (`trace_id` propagated as correlation id).
- `GET /api/v1/health` (liveness, no detail) and `GET /api/v1/health/ready`
  (dependency status without topology or credentials).

## 10. Known architectural limitations

- Single-region, single-database deployment. No HA claim is made.
- In-process cancellation relies on the worker's cooperative checks; a hard kill
  leaves a job `running` until the stale-job reaper reclaims it.
- RLS uses a transaction-local GUC; connections must be per-transaction. The app
  enforces this and tests it.
- Dev identity provider is for local development only and is refused in production
  configuration validation.
