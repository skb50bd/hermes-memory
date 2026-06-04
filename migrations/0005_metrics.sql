-- 0005_metrics.sql
-- hermes_metrics schema: timescaledb hypertable for operational metrics.
-- This is what timescaledb is for: append-only, queried in aggregates.
--
-- The TimescaleDB-specific calls are wrapped in a DO block so the
-- migration works on plain Postgres (CI, dev, tests). On plain
-- Postgres the table stays a regular table — fine for short-lived
-- test data, less efficient for production scale.

CREATE SCHEMA IF NOT EXISTS hermes_metrics;

CREATE TABLE IF NOT EXISTS hermes_metrics.events (
    ts          timestamptz NOT NULL,
    profile     text NOT NULL,
    metric_name text NOT NULL,
    value       double precision NOT NULL,
    tags        jsonb DEFAULT '{}'::jsonb
);

DO $body$
BEGIN
    -- create_hypertable / add_compression_policy / add_retention_policy
    -- are TimescaleDB-only. Skip silently on plain Postgres.
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        PERFORM create_hypertable('hermes_metrics.events', 'ts',
            chunk_time_interval => INTERVAL '7 days',
            if_not_exists => TRUE);

        ALTER TABLE hermes_metrics.events SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'profile,metric_name',
            timescaledb.compress_orderby   = 'ts DESC'
        );

        PERFORM add_compression_policy('hermes_metrics.events', INTERVAL '30 days');
        PERFORM add_retention_policy('hermes_metrics.events', INTERVAL '90 days');
    ELSE
        RAISE NOTICE 'timescaledb not installed — hermes_metrics.events stays a plain table';
    END IF;
END
$body$;
