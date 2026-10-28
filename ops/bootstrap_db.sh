#!/usr/bin/env bash
#
# Apply the additional-database bootstrap that the PostgreSQL container entrypoint does
# not run.
#
# The container entrypoint executes ops/postgres/init.sql once, when the data directory is
# first created. It creates the application role and the `humisense` database. The test
# database is created here instead, so it can be added, dropped, and re-added without
# recreating the container or losing the development data in `humisense`.
#
# Usage:
#   ops/bootstrap_db.sh                 # create/refresh the test database
#   PGHOST=db.example.com ops/bootstrap_db.sh
#
# Environment (with development defaults):
#   PGHOST      default 127.0.0.1
#   PGPORT      default 5432
#   PGUSER      default postgres           (must be able to CREATE DATABASE)
#   PGPASSWORD  default postgres
#   PGCONTAINER default humisense-pg        (used when psql is not installed locally)
#   DOCKER      default docker              (set to "sudo docker" if the socket needs it)
#
# This script never prints a password and never writes one to disk.

set -o errexit
set -o nounset
set -o pipefail

readonly PGHOST="${PGHOST:-127.0.0.1}"
readonly PGPORT="${PGPORT:-5432}"
readonly PGUSER="${PGUSER:-postgres}"
readonly PGCONTAINER="${PGCONTAINER:-humisense-pg}"
# Some environments require privilege to reach the Docker socket (for example a shared
# development host where the user is not in the docker group). Override with DOCKER.
readonly DOCKER="${DOCKER:-docker}"
readonly APP_ROLE="${APP_ROLE:-humisense_app}"
readonly TEST_DB="${TEST_DB:-humisense_test}"

readonly HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly INIT_TEST_SQL="${HERE}/postgres/init-test.sql"

if [[ ! -f "${INIT_TEST_SQL}" ]]; then
    echo "bootstrap_db: cannot find ${INIT_TEST_SQL}" >&2
    exit 1
fi

# Prefer a local psql. Fall back to the container's client so the script works on a
# machine that has Docker but no PostgreSQL client installed.
run_psql() {
    if command -v psql >/dev/null 2>&1; then
        PGPASSWORD="${PGPASSWORD:-postgres}" psql \
            --host="${PGHOST}" --port="${PGPORT}" --username="${PGUSER}" \
            --no-password --set=ON_ERROR_STOP=1 "$@"
        return
    fi

    if command -v "${DOCKER%% *}" >/dev/null 2>&1; then
        # Pass the password through the container environment, never as an argument, so it
        # does not appear in the process list or in shell history.
        ${DOCKER} exec --interactive \
            --env "PGPASSWORD=${PGPASSWORD:-postgres}" \
            "${PGCONTAINER}" \
            psql --host=127.0.0.1 --port=5432 --username="${PGUSER}" \
            --no-password --set=ON_ERROR_STOP=1 "$@"
        return
    fi

    echo "bootstrap_db: neither psql nor docker is available" >&2
    exit 1
}

echo "bootstrap_db: applying ${INIT_TEST_SQL##*/} against ${PGHOST}:${PGPORT} as ${PGUSER}"
run_psql --dbname=postgres --file=/dev/stdin <"${INIT_TEST_SQL}"

echo "bootstrap_db: ${TEST_DB} is ready (owner ${APP_ROLE})"
