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
