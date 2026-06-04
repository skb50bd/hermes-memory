-- 01-schemas.sql
-- Idempotent. Creates all schemas for the hermes-memory platform.
-- Run once against `hermes_template` AFTER 00-extensions.sql.
-- The result is a self-contained template DB. Every profile DB created
-- from it via `hermes-memory profile create <name>` is a byte-perfect
-- clone and needs no further setup.

\ir ../../../migrations/0001_agent_memory.sql
\ir ../../../migrations/0002_wiki.sql
\ir ../../../migrations/0003_journal.sql
\ir ../../../migrations/0004_skills.sql
\ir ../../../migrations/0005_metrics.sql
\ir ../../../migrations/0006_kanban.sql

-- =====================================================================
-- 7. public.schema_migrations
-- =====================================================================
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz DEFAULT now(),
    checksum   text
);

-- =====================================================================
-- 8. pg_cron jobs
-- =====================================================================
-- pg_cron is configured (via postgresql.conf) to read jobs from
-- `hermes_template`. BUT — `hermes_template` must be clonable, and
-- `CREATE DATABASE ... TEMPLATE` refuses if the source has active
-- connections. pg_cron + TimescaleDB each keep one idle session pinned
-- to cron.database_name, which blocks the clone.
--
-- Workaround: don't keep ANY user-database pin in hermes_template. Move
-- the cron jobs to a dedicated `hermes_cron` database that's NOT cloned.
-- Profile DBs are clones of `hermes_template` (clean, no active sessions).
-- `hermes_cron` is created once by the bootstrap, never cloned.
-- ---------------------------------------------------------------------
-- We DON'T schedule the jobs here. They get scheduled by hermes-cron.sh
-- in a follow-up step that connects to `hermes_cron` (the one DB the
-- workers can safely idle in).
