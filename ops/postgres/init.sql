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

GRANT CONNECT ON DATABASE humisense TO humisense_app;
GRANT CREATE, USAGE ON SCHEMA public TO humisense_app;
ALTER SCHEMA public OWNER TO humisense_app;
