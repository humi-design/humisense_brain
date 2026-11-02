# syntax=docker/dockerfile:1.7
#
# Backend image: FastAPI API, arq worker, and Alembic migrations share one artifact.
# The image is built from the repository root context so the lockfile and the ops/
# directory are both available; see .dockerignore for what is excluded.
#
# Multi-stage: the builder resolves the locked dependency set, the runtime stage carries
# only the virtual environment and the application. No compiler, no uv cache, and no
# build toolchain reach the final image.

# Base images are pinned to an exact patch release. A digest pin is stronger still and is
# expected in a controlled registry; the tag is used here so the build is reproducible
# without requiring the operator to look up digests first.
FROM ghcr.io/astral-sh/uv:0.5.14-python3.12-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, so a source-only change does not invalidate the dependency layer.
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Application source, then the project itself.
COPY backend/ ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.12.7-slim-bookworm AS runtime

# A dedicated non-root account. The application never needs to write to its own code
# directory, and running unprivileged means a compromise in the process does not own the
# container.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    APP_ENV=production

WORKDIR /app

COPY --from=builder --chown=app:app /app /app
COPY --chown=app:app ops/docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

# Writable locations, owned by the runtime user. The rest of the image stays read-only at
# run time (the compose service sets `read_only: true`).
RUN mkdir -p /app/var/uploads && chown -R app:app /app/var

USER app

EXPOSE 8000

# Readiness, not just liveness: /health/ready reports whether the database is reachable.
# The probe uses the Python already in the image rather than adding curl.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/ready', timeout=4).status==200 else 1)"]

# The entrypoint execs the given command, so SIGTERM reaches uvicorn directly and shutdown
# is graceful.
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "humisense.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
