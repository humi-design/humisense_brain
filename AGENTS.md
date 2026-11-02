# AGENTS.md — repository knowledge for future sessions

Humisense Brain / OS, Layer 1 (Secure Connector & Sensory Platform).
Multi-tenant FastAPI + React platform. Security is an architectural requirement, not a
feature: tenant isolation, authorization, auditability, and safe failure are enforced in
the backend and covered by tests.

## Layout

- `backend/` FastAPI modular monolith, connectors, services, arq worker, tests, migrations.
- `frontend/` React + TypeScript (strict) + Vite + Tailwind operator UI.
- `docs/` architecture, threat model, Canary status.
- `ops/postgres/` bootstrap SQL; `ops/bootstrap_db.sh` creates the test database.
- `data/synthetic/` synthetic development data (never live telemetry).

## Commands

Run backend commands from `backend/`:

```bash
uv sync
uv run pytest                        # full suite (needs PostgreSQL)
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run bandit -c pyproject.toml -r src
uv run pip-audit --progress-spinner off
uv run alembic upgrade head
uv run python -m humisense.devtools.cli seed
uv run python -m humisense.devtools.cli benchmark --records 5000 --batch-size 500
uv run uvicorn humisense.main:app --reload --port 8000
uv run arq humisense.workers.worker.WorkerSettings
```

Run frontend commands from `frontend/`:

```bash
npm ci && npm run dev
npm run typecheck && npm run lint && npm test && npm run build
```

## Environment facts that are easy to get wrong

- The test database DSN variable is `HUMISENSE_TEST_DATABASE_URL`, not `TEST_DATABASE_URL`.
  It defaults to `postgresql+asyncpg://humisense_app:humisense_app_dev_pw@localhost:5432/humisense_test`.
- `ops/postgres/init.sql` runs only when the PostgreSQL data directory is first created.
  The test database is created by `ops/bootstrap_db.sh`; it is not created by the container
  entrypoint. `DOCKER="sudo docker"` is needed where the socket requires privilege.
- The application connects as `humisense_app`, which is `NOSUPERUSER NOBYPASSRLS`. Tables
  are `FORCE ROW LEVEL SECURITY`, so owning them does not bypass the policies. Do not run
  the app or Alembic as a superuser — it would silently defeat tenant isolation.
- `alembic/env.py` builds the engine from the unwrapped DSN instead of putting it in the
  Alembic `Config`, because configparser interpolates `%` and would mangle a URL-encoded
  password.
- Connector documentation links are repo-relative paths (`docs/CANARY.md`) served by the
  frontend from `frontend/public/docs`, a symlink to the repository `docs/`. Vite copies
  the symlink target into `dist/docs` at build time. A missing file is not a 404 — the SPA
  fallback returns `index.html` and the link opens a blank page, so `test_connector_docs.py`
  asserts the file exists rather than trusting a 200 response.
- `npm run typecheck` must stay plain `tsc -b`. Passing `--noEmit false` overrides the
  config's `noEmit: true`, emits `.js` files next to the `.tsx` sources, and then breaks
  `npm run lint`.
- The frontend needs Node 22.19 or newer. Vitest 5 declares `^22.12.0 || ^24.0.0`, and its
  `undici` dependency declares `>=22.19.0`; on Node 20 the test worker fails to start with
  `Cannot read properties of undefined (reading 'bind')` in
  `undici/lib/interceptor/decompress.js`, and the suite reports "no tests" rather than a
  failure in the code under test.
- gitleaks reads `.gitleaks.toml` automatically. The config keeps the default ruleset and
  allows only specific synthetic fixture values, matched on the secret itself (not by file
  or path), so a real credential committed in the same test files is still reported.
- The reference stack (`ops/compose/docker-compose.yml`) runs the API with
  `APP_ENV=production`, so it must satisfy the production gate, including verified database
  TLS. The `tls` init service generates a CA and server certificate; a real deployment
  replaces it with its own CA at `DATABASE_TLS_CA_PATH`. Do not disable
  `database_require_tls` to make the stack start.
- `Settings.database_connect_args` builds an `ssl.SSLContext` for asyncpg when
  `database_require_tls` is set. asyncpg rejects a plain dict for `ssl`, so a dict would
  silently connect in plaintext. Both the app engine and `alembic/env.py` pass it.
- `require_production_ready()` is stricter than `validate_for_production()`: it also
  refuses a configuration that is merely not production-grade. `uv run python -m
  humisense.devtools.cli release-check` is the caller; it exits non-zero and prints every
  unmet requirement. Use it as the release gate.
- Prometheus metrics are exposed at `GET /api/v1/metrics`, gated by the `metrics:read`
  permission and by `settings.metrics_enabled`. The route is not registered at all when
  metrics are disabled.
- `ops/compose/.env` is git-ignored; `ops/compose/.env.example` is committed and must
  contain placeholders only (CI enforces this).
- arq resolves worker settings from the **class** `__dict__` (`arq.worker.get_kwargs`
  returns `settings_cls.__dict__`), so it reads class attributes only. Assigning
  `redis_settings` or `cron_jobs` in `WorkerSettings.__init__` puts them on the instance
  and arq silently ignores them, falling back to `localhost:6379`. Keep every arq setting a
  class attribute; `test_worker.py` asserts what `get_kwargs` actually resolves. The worker
  service also needs `CORS_ORIGINS` (it shares the production validator) and an arq
  `--check` healthcheck (it serves no HTTP).

## Layer 2 (ingestion engine)

- The ingestion receiver is the only endpoint that does not use a session principal. It is
  registered as `machine_router` only when `layer2_ingest_enabled` is true, and it
  authenticates with a service bearer token (`layer2_ingest_token`). The tenant is never
  trusted from the request body: the batch's connection is looked up inside a tenant-scoped
  session. Do not add a session-based path to this router.
- `layer2_max_batch_bytes` bounds the serialized payload + metadata of a whole batch, and the
  receiver enforces it (a record-count limit alone lets a caller send the maximum number of
  maximum-size records). It must stay at or below `max_request_body_bytes`; the production
  gate refuses a larger value, because the transport would reject the request first and the
  receiver's own check would be dead code.
- A retransmit of the same `batch_id` must not rewrite the durable batch counters. The
  counters returned to a caller describe what *that delivery* did (a retransmit reports
  every record as a duplicate); the `ingest_batches` row keeps the first processing's
  counters. Seeding the response from the row and then adding the per-record outcomes
  double-counts.
- `layer2_publish_mode` is `disabled` | `log` | `http`. Publication is durable-first: the
  canonical record and its outbox event are written in one transaction, and the publisher
  moves `pending → published` only after the sink confirms. `http` publication requires both
  `layer2_publish_base_url` and a token in production.
- Retention is **not** implemented. Raw records are preserved indefinitely. Do not add a
  retention-looking knob until a purge job exists to honour it — a knob that promises
  retention without a purge is worse than no knob (see `docs/LAYER2-RELEASE.md`, D2).
- `uv run python -m humisense.devtools.cli benchmark` measures pipeline throughput and the
  idempotency path against a real database. It is a measurement tool: it asserts nothing and
  its numbers are recorded in `docs/LAYER2-RELEASE.md`.

## Conventions

- Python: 3.12+, Ruff (line length and rules in `backend/pyproject.toml`), mypy strict on
  `src`. Tests use pytest with `anyio`. Security-relevant tests carry
  `@pytest.mark.security`.
- TypeScript: strict, plus `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`,
  `verbatimModuleSyntax`. `verbatimModuleSyntax` requires `import type` for type-only
  imports.
- Errors: typed exceptions and structured result objects. Do not swallow exceptions or
  return inconsistent dictionaries from connector methods.
- Connector capability flags must be truthful. Never advertise a capability that is not
  implemented and tested — `test_connector_registry.py` checks this.
- Secrets never appear in API responses, logs, audit metadata, or the frontend bundle.
  Responses expose `has_credentials` only.
- Comments explain non-obvious invariants and deliberate trade-offs. Do not narrate the
  change or restate the code.

## Layer boundaries

Layer 1 captures and transports records; it does not normalize, deduplicate, reason, plan,
remember, or detect anomalies. Do not add Layer 2+ behaviour here. The Layer 2 handoff
(`services/` ingestion, `LAYER2_MODE`) is the contract boundary.
