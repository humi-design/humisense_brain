#!/bin/sh
#
# Backend container entrypoint.
#
# Runs as a non-root user. Applies the database migrations when asked, then execs the
# container command so the process becomes PID 1's child directly and receives SIGTERM
# for a graceful shutdown.
#
# No secret is read, echoed, or written here. Every value comes from the environment the
# orchestrator injected.

set -eu

# Migrations are opt-in per service, so only one process runs them. The API service sets
# HUMISENSE_RUN_MIGRATIONS=1; the worker and a scaled-out API replica do not, which avoids
# several processes racing the same DDL.
if [ "${HUMISENSE_RUN_MIGRATIONS:-0}" = "1" ]; then
    echo "entrypoint: applying database migrations"
    alembic upgrade head
fi

exec "$@"
