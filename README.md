# Humisense Brain / OS — Layer 1: Secure Connector & Sensory Platform

Layer 1 connects to an organization's authorized source systems, captures records, and
transports them to Layer 2 in a validated envelope. It does not normalize, reason, plan,
or detect anomalies — those belong to Layer 2 and above.

This repository is a **multi-tenant, security-first** implementation, not a prototype.
Tenant isolation, authorization, auditability, and safe failure are enforced in the
backend, not by frontend visibility.

- Architecture and threat model: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md)
- Canary Labs status and access requirements: [`docs/CANARY.md`](docs/CANARY.md)

## Status at a glance

| Area | State |
| --- | --- |
| Backend API (FastAPI) | Working, tested |
| CSV connector | Working end to end (read-only) |
| Canary Labs adapter | `not_configured` by design — no interface is assumed |
| React operator UI | Working, talks to the real API |
| Tenant isolation (RLS) | Enforced and tested |
| Layer 2 handoff | Defined and tested (local test receiver) |
| Background sync (arq) | Working, tested |

## Layout

```
backend/    FastAPI application, connectors, services, workers, tests, migrations
frontend/   React + TypeScript (strict) + Vite + Tailwind operator interface
docs/       Architecture, threat model, Canary status
ops/        PostgreSQL bootstrap SQL and scripts
data/       Synthetic development data (clearly labelled, never live telemetry)
```

## Prerequisites

- Python 3.12 or newer, with [`uv`](https://docs.astral.sh/uv/)
- Node.js 20 or newer
- Docker (for PostgreSQL and Redis), or local PostgreSQL 16 and Redis 7
- macOS, Linux, or a Linux container

## Quick start

Run these from the repository root.

```bash
# 1. PostgreSQL 16 and Redis 7
docker run -d --name humisense-pg -p 5432:5432 \
  -e POSTGRES_PASSWORD=postgres \
  -v "$PWD/ops/postgres/init.sql:/docker-entrypoint-initdb.d/init.sql:ro" \
  postgres:16-alpine
docker run -d --name humisense-redis -p 6379:6379 redis:7-alpine

# 2. Test database (the container entrypoint does not create it)
ops/bootstrap_db.sh            # add DOCKER="sudo docker" if the socket needs privilege

# 3. Backend configuration
cd backend
cp .env.example .env
# Generate the two required secrets and paste them into .env:
#   python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"  # SECRETS_MASTER_KEY
#   python -c "import secrets;print(secrets.token_urlsafe(48))"                    # SESSION_SECRET
uv sync

# 4. Migrate, then seed demo tenants and users
uv run alembic upgrade head
uv run python -m humisense.devtools.cli seed

# 5. Start the API and the background worker (separate terminals)
uv run uvicorn humisense.main:app --reload --port 8000
uv run arq humisense.workers.worker.WorkerSettings
```

In a fourth terminal, start the frontend:

```bash
cd frontend
npm install
npm run dev        # http://localhost:5173, proxies /api to the backend
```

Sign in at `/sign-in` with the development issuer. The seeded identities are
`dev-owner`, `dev-admin`, `dev-operator`, `dev-viewer` in `humisense-demo`, and
`dev-owner-b` in `humisense-demo-b` (used for cross-tenant checks). Development sign-in
is refused when `AUTH_MODE=oidc`, so it cannot be reached in a production configuration.

## Tests and checks

```bash
# Backend
cd backend
uv run pytest                     # full suite
uv run ruff check . && uv run ruff format --check .
uv run mypy src

# Frontend
cd frontend
npm run typecheck
npm run lint
npm test
npm run build
```

The backend suite requires the test database. Set `HUMISENSE_TEST_DATABASE_URL` if it is not at the
default `postgresql+asyncpg://humisense_app:humisense_app_dev_pw@localhost:5432/humisense_test`.

## Configuration

All backend settings are documented in `backend/.env.example`. The values that most often
need attention:

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Application DSN. Uses a non-superuser role so RLS applies. |
| `AUTH_MODE` | `dev` or `oidc`. `dev` is refused in production. |
| `SECRETS_PROVIDER` | `encrypted_db` (production) or `env` (development only). |
| `SECRETS_MASTER_KEY` | Base64 of exactly 32 random bytes. |
| `CSV_ALLOWED_ROOTS` | Directories the CSV connector may read. |
| `EGRESS_ALLOWED_HOSTS` | Hostnames or CIDRs connectors may reach. |
| `LAYER2_MODE` | `disabled`, `local_test_receiver`, or `http`. |
| `TASK_QUEUE_MODE` | `arq` (production) or `inline` (development only). |

`backend/.env.example` contains placeholders only. Never commit a real credential.

## Security posture

Enforced in code and covered by tests: deny-by-default authorization on every protected
route; PostgreSQL Row-Level Security with `FORCE` on every tenant table; encrypted
credentials that are never returned after saving; an egress policy that blocks loopback,
private, link-local, reserved, and cloud-metadata addresses; path canonicalization and
allowlisting for file connectors; CSRF protection on cookie-authenticated writes;
per-tenant rate limits; and an append-only audit trail. See `docs/THREAT_MODEL.md` for the
threats, mitigations, and residual risk.

This is a controlled production-readiness candidate, not a certified or penetration-tested
system. Complete an independent security review and penetration test, and validate
deployment hardening, before onboarding real enterprise data. Known gaps are listed in
`docs/ARCHITECTURE.md`.
