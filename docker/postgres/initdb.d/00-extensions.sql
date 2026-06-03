-- 00-extensions.sql
-- Runs in every newly created database (initdb.d/ is replayed on
-- `CREATE DATABASE ... TEMPLATE = template0`).
--
-- Extensions here MUST be idempotent. CREATE EXTENSION IF NOT EXISTS is
-- required because the template-clone path (hermes-memory profile create
-- <name>) creates databases from hermes_template, which has the extensions
-- baked in. The IF NOT EXISTS is the safety net.

CREATE EXTENSION IF NOT EXISTS vector;          -- pgvector: HNSW, IVFFlat
CREATE EXTENSION IF NOT EXISTS pg_trgm;         -- trigram indexes + similarity()
CREATE EXTENSION IF NOT EXISTS postgis;         -- geospatial
CREATE EXTENSION IF NOT EXISTS pg_cron;         -- scheduled jobs
CREATE EXTENSION IF NOT EXISTS timescaledb;     -- hypertables, compression, retention
CREATE EXTENSION IF NOT EXISTS age;             -- graph (Cypher); used later, installed now

-- ltree ships with Postgres, no extension needed
