# Backend — Humisense Brain / OS, Layer 1

FastAPI modular monolith. Owns identity, tenant isolation, connector execution, sync jobs,
the source-record envelope, the Layer 2 handoff, and the audit trail.

See the repository root [`README.md`](../README.md) for setup, and
[`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) and
[`docs/THREAT_MODEL.md`](../docs/THREAT_MODEL.md) for the design and threat model.

## Module map

```
src/humisense/
  api/          routers (auth, connections, connectors, health), schemas, error mapping
  connectors/   SDK: base, types, manifest, static registry, csv, canary_labs
  services/     connections, sync, ingestion handoff (Layer 2), audit
  secrets/      SecretsProvider: EnvSecretsProvider | EncryptedDbSecretsProvider
  security/     OIDC/dev identity, authorization, CSRF, rate limiting, SSRF egress,
                path safety, redaction, response headers, crypto
  db/           SQLAlchemy 2.x models, session, RLS helpers, base
  workers/      arq worker: run_sync_job, reap_stale_jobs_task
  observability/ structured logging, metrics, correlation IDs
  devtools/     operator CLI: seed, demo, doctor
alembic/        migrations (0001_initial creates the schema and the RLS policies)
tests/          pytest suite
```

## Running

```bash
uv sync
uv run alembic upgrade head
uv run python -m humisense.devtools.cli seed
uv run uvicorn humisense.main:app --reload --port 8000   # API
uv run arq humisense.workers.worker.WorkerSettings        # background worker
```

`TASK_QUEUE_MODE=inline` runs sync work inside the API process for quick development. It
is refused in production; use the arq worker there.

## Quality gates

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

`pytest` needs PostgreSQL reachable at `HUMISENSE_TEST_DATABASE_URL` (default
`postgresql+asyncpg://humisense_app:humisense_app_dev_pw@localhost:5432/humisense_test`).
Create it with `ops/bootstrap_db.sh` from the repository root. The suite applies the
migrations to the test database itself.

## Tests by concern

| File | Concern |
| --- | --- |
| `test_api_security.py` | Authn/authz on every route, error shapes, no leaked internals |
| `test_security_rls.py` | RLS enabled and forced, cross-tenant read/write/update/delete |
| `test_security_tenant_isolation.py` | Tenant scoping through the service layer |
| `test_security_secrets.py` | Encrypted credentials, redaction, rotation |
| `test_security_ssrf.py` | Egress policy: loopback, private, link-local, metadata, redirects |
| `test_security_files.py` | Path traversal, symlinks, oversized and malformed files |
| `test_rate_limit.py` | Per-tenant limits on sensitive operations |
| `test_config_hardening.py` | Production startup rejects insecure configuration |
| `test_csv_connector.py` | CSV capture end to end, envelope, limits |
| `test_connector_registry.py` | Static allowlist, truthful capability flags |
| `test_connector_docs.py` | Every advertised documentation link resolves to a real file |
| `test_ingestion_handoff.py` | Layer 2 envelope, acknowledgment, idempotency, retries |
| `test_sync_resilience.py` | Retry classification, backoff, cancellation, checkpoints |
| `test_worker.py` | The arq worker's own code path (not reached by service tests) |
| `test_audit.py` | Append-only audit events for security-sensitive operations |
| `test_migrations.py` | Migration environment and generated DDL |

## Migrations

```bash
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head --sql        # offline: emit SQL without connecting
```

`alembic/env.py` builds the engine from the unwrapped DSN rather than writing it into the
Alembic `Config`, because configparser treats `%` as interpolation syntax and would mangle
a URL-encoded password.

## Secrets

`SECRETS_PROVIDER=encrypted_db` (default) stores connector credentials as authenticated
ciphertext keyed by `SECRETS_MASTER_KEY` (base64 of exactly 32 random bytes). Values are
never returned after saving; responses expose only `has_credentials`. `SECRETS_PROVIDER=env`
exists for development and is refused in production unless explicitly overridden. See
`docs/THREAT_MODEL.md` for the rotation and incident-response procedure.

## Configuration

`backend/.env.example` documents every setting with placeholders only. Copy it to `.env`,
which is git-ignored, and replace the placeholders.
