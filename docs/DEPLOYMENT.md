# Deployment

How to build and run Humisense Brain / OS outside a development machine. This document
describes the reference deployment in `ops/compose/docker-compose.yml` and states plainly
what it does and does not provide.

## What this deployment is

A **single-node reference deployment**: one PostgreSQL, one Redis, one API, one worker, and
one frontend, on private Docker networks. It is the artifact a pilot runs and the artifact
an image scanner scans. It is not a high-availability topology.

## What this deployment is not

- **Not high availability.** One database, one Redis, one API replica, one worker. A node
  failure is an outage. Zero-downtime upgrades are not provided. Making this highly
  available means managed PostgreSQL with automated failover, a replicated Redis (or a
  managed equivalent), at least two API replicas behind a load balancer, and a rolling
  migration strategy; none of that is in this repository.
- **Not a secrets manager.** Every credential is read from `ops/compose/.env`, which is
  git-ignored. A real deployment injects them from the platform's secret store. The
  `SECRETS_MASTER_KEY` is the exception that must be backed up: losing it makes stored
  connector credentials unrecoverable.
- **Not a TLS terminator for client traffic.** The frontend container serves plain HTTP on
  host loopback. A TLS-terminating reverse proxy fronts it in a real deployment (see
  "Client TLS" below).

## Prerequisites

- Docker Engine 24+ with the Compose v2 plugin.
- A host with at least 2 CPU cores and 2 GB RAM free for the stack.

## Build and run

```bash
# 1. Configuration
cp ops/compose/.env.example ops/compose/.env
# Edit ops/compose/.env and replace every value marked CHANGE-ME. Generate the random
# values with:
#   python -c "import secrets; print(secrets.token_urlsafe(48))"
#   python -c "import base64,os; print(base64.b64encode(os.urandom(32)).decode())"

# 2. Build the images
docker compose --env-file ops/compose/.env -f ops/compose/docker-compose.yml build

# 3. Start the stack
docker compose --env-file ops/compose/.env -f ops/compose/docker-compose.yml up -d

# 4. Confirm every service is healthy
docker compose --env-file ops/compose/.env -f ops/compose/docker-compose.yml ps
```

The frontend binds to `127.0.0.1:18081` by default and proxies `/api` to the backend. No
database or Redis port is published; both are reachable only on the private networks.

## Database TLS

The API runs with `APP_ENV=production`, so it is subject to the same startup gate as any
production deployment. That gate requires verified TLS to the database
(`database_require_tls`), and the requirement is enforced on the connection, not merely
checked at startup: the engine builds an `ssl.SSLContext` that verifies the server
certificate and its hostname.

The reference stack satisfies this with a `tls` init service that generates a self-signed
CA and a server certificate whose subject alternative name includes the service name `db`.
PostgreSQL is started with `ssl=on`, and the backend mounts the CA at
`/etc/ssl/humisense/ca.crt` (the path in `DATABASE_TLS_CA_PATH`).

**A real deployment replaces this.** Point PostgreSQL at certificates issued by your own
CA, and mount that CA into the backend at `DATABASE_TLS_CA_PATH`. The generator exists only
so the reference stack exercises the same verified-TLS code path that production uses;
the requirement is never disabled to make the stack boot.

To verify TLS is actually in use, not merely configured:

```bash
docker compose --env-file ops/compose/.env -f ops/compose/docker-compose.yml \
  exec db psql -U postgres -d humisense -tAc \
  "SELECT ssl, version, cipher FROM pg_stat_ssl WHERE pid <> pg_backend_pid();"
```

A row beginning with `t` (for example `t|TLSv1.3|TLS_AES_256_GCM_SHA384`) is the
application's connection over TLS.

## Client TLS

The frontend container serves HTTP. In production, a reverse proxy terminates TLS in front
of it and forwards to `127.0.0.1:18081`. Set `TRUSTED_PROXY_COUNT` in `ops/compose/.env` to
the number of proxies in front of the API, so the API resolves the client IP from
`X-Forwarded-For` correctly. Leave it at `0` if the API is reached directly, because the
trusted-proxy model ignores `X-Forwarded-For` unless the topology is declared.

## Release check

Before promoting an image, run the release gate against the target configuration. It
refuses a configuration that is merely not production-grade, not only one that is unsafe
given it claims production:

```bash
docker compose --env-file ops/compose/.env -f ops/compose/docker-compose.yml \
  run --rm --no-deps backend python -m humisense.devtools.cli release-check
```

Exit code `0` means the configuration is production-ready; `1` prints every unmet
requirement. The gate reads configuration only, opens no database connection, and prints no
secret, so it is safe to run in a pipeline.

## Migrations

The API service applies migrations on start (`HUMISENSE_RUN_MIGRATIONS=1`). The worker and
any scaled-out API replica do not, so several processes never race the same DDL. To apply
migrations on their own, before starting the new API:

```bash
docker compose --env-file ops/compose/.env -f ops/compose/docker-compose.yml \
  run --rm backend alembic upgrade head
```

## Backup and restore

- **Database.** `db-data` is a named volume. Back up with `pg_dump` against the
  application database; the application role owns the schema and can read every table it
  needs. Do not rely on copying the volume while PostgreSQL is running.
- **Secrets.** `SECRETS_MASTER_KEY` must be backed up in the platform's secret store.
  Without it, every stored connector credential is unrecoverable, even with a database
  backup.
- **Uploads.** `uploads` is a named volume for any captured files.

## Upgrades

1. Run `release-check` against the new configuration.
2. Take a database backup.
3. `docker compose pull` (or `build`) the new image tag.
4. `up -d`. The API applies migrations on start.

This is not zero-downtime: there is one API replica, so there is a brief gap while it
restarts. A zero-downtime upgrade needs at least two replicas and a rolling strategy.

## Operational visibility

The API exposes Prometheus metrics at `GET /api/v1/metrics`, authenticated and gated by the
`metrics:read` permission (granted to `admin` and `owner`). The route exists only when
`METRICS_ENABLED` is true (the default). The registry carries no tenant identifier, so the
endpoint exposes aggregate process metrics, not tenant data.

## What remains open

From the Layer 1 audit (`docs/AUDIT-LAYER1.md`), the following are not addressed by this
deployment and are required before onboarding multi-tenant enterprise data:

- **F4**: `semgrep` and SBOM generation are not in CI, and the images are not scanned.
- **F8**: frontend test coverage is thin.
- **F9**: the rate limiter fails open to a per-process backend when Redis is unavailable.
- A third-party penetration test has not been performed.
