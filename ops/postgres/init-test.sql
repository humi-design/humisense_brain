-- Additional test database. Applied by ops/bootstrap_db.sh, not by the container entrypoint.
SELECT 'CREATE DATABASE humisense_test OWNER humisense_app'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'humisense_test')\gexec
GRANT ALL PRIVILEGES ON DATABASE humisense_test TO humisense_app;
