-- Runs once when the Postgres volume is first created.
-- Tables are owned by Alembic (see migrations/); this only prepares the schema.
CREATE SCHEMA IF NOT EXISTS idp_schema;
