-- Bootstrap roles and databases for Humisense Brain / OS.
-- Runs once as the PostgreSQL superuser when the container is first created.
--
-- Two roles exist deliberately:
--   humisense_bootstrap : superuser, used only for cluster-level setup (never by the app)
--   humisense_app       : NOSUPERUSER, NOCREATEDB, NOCREATEROLE, NOBYPASSRLS
--                         owns the schema and is used by the application and by
--                         Alembic. Because the tables are FORCE ROW LEVEL SECURITY,
--                         owning them does not let this role bypass the policies.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'humisense_app') THEN
        CREATE ROLE humisense_app LOGIN PASSWORD 'humisense_app_dev_pw'
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
    END IF;
END
$$;

-- CREATE DATABASE cannot run inside a transaction or a DO block, so it is issued through
-- \gexec: the SELECT yields the statement only when the database is absent, which keeps
-- this script re-runnable. The role must already exist, because it is named as the owner.
SELECT 'CREATE DATABASE humisense OWNER humisense_app'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'humisense')\gexec

GRANT CONNECT ON DATABASE humisense TO humisense_app;

-- The schema grants belong to the application database, not to whichever database the
-- container entrypoint happens to be connected to when it runs this file.
\connect humisense
GRANT CREATE, USAGE ON SCHEMA public TO humisense_app;
ALTER SCHEMA public OWNER TO humisense_app;
