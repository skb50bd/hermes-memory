-- 0005_metrics.sql
-- hermes_metrics schema: timescaledb hypertable for operational metrics.
-- This is what timescaledb is for: append-only, queried in aggregates.
--
-- The TimescaleDB-specific calls are wrapped in a DO block so the
-- migration works on:
--   (a) plain Postgres (CI, dev, tests without timescaledb) → regular table
--   (b) OSS TimescaleDB (apache license) → hypertable, no compression
--   (c) Enterprise TimescaleDB → full hypertable + compression + retention
--
-- All branches are non-fatal — the migration always succeeds.

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
    IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
        RAISE NOTICE 'timescaledb not installed — hermes_metrics.events stays a plain table';
        RETURN;
    END IF;

    -- Promote to hypertable. Available in both OSS and Enterprise.
    BEGIN
        PERFORM create_hypertable('hermes_metrics.events', 'ts',
            chunk_time_interval => INTERVAL '7 days',
            if_not_exists => TRUE);
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'create_hypertable failed (%): %', SQLSTATE, SQLERRM;
        RETURN;
    END;

    -- Compression is Enterprise-only. Catch the "apache license" error
    -- and continue — the table still works as a hypertable without
    -- compression on OSS builds.
    BEGIN
        ALTER TABLE hermes_metrics.events SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'profile,metric_name',
            timescaledb.compress_orderby   = 'ts DESC'
        );
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'timescaledb.compress not available (%): % — continuing without compression', SQLSTATE, SQLERRM;
    END;

    -- Compression + retention policies depend on compression being enabled.
    -- Only schedule them if the compression option is actually set.
    IF EXISTS (
        SELECT 1 FROM pg_class c
        JOIN pg_options_to_table(c.reloptions) opt ON true
        WHERE c.relname = 'events'
          AND c.relnamespace = 'hermes_metrics'::regnamespace
          AND opt.option_name = 'timescaledb.compress'
    ) THEN
        BEGIN
            PERFORM add_compression_policy('hermes_metrics.events', INTERVAL '30 days');
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'add_compression_policy failed (%): %', SQLSTATE, SQLERRM;
        END;
    END IF;

    -- Retention policy is available in both editions.
    BEGIN
        PERFORM add_retention_policy('hermes_metrics.events', INTERVAL '90 days');
    EXCEPTION WHEN OTHERS THEN
        RAISE NOTICE 'add_retention_policy failed (%): %', SQLSTATE, SQLERRM;
    END;
END
$body$;
