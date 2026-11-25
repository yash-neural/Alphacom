-- Runs once, on first start of an empty data volume (as the postgres superuser).
-- Tables are created by Alembic as the owner; the app logs in as "api", which the
-- migration grants only what it needs (no UPDATE/DELETE on audit_log, no DDL).
CREATE ROLE api LOGIN PASSWORD 'api-dev-password';
