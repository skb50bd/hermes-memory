-- 0005_metrics.sql
-- hermes_metrics schema: timescaledb hypertable for operational metrics.
-- This is what timescaledb is for: append-only, queried in aggregates.

CREATE SCHEMA IF NOT EXISTS hermes_metrics;

CREATE TABLE IF NOT EXISTS hermes_metrics.events (
    ts          timestamptz NOT NULL,
    profile     text NOT NULL,
    metric_name text NOT NULL,
    value       double precision NOT NULL,
    tags        jsonb DEFAULT '{}'::jsonb
);

SELECT create_hypertable('hermes_metrics.events', 'ts',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE);

ALTER TABLE hermes_metrics.events SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'profile,metric_name',
    timescaledb.compress_orderby   = 'ts DESC'
);

SELECT add_compression_policy('hermes_metrics.events', INTERVAL '30 days');
SELECT add_retention_policy('hermes_metrics.events', INTERVAL '90 days');
