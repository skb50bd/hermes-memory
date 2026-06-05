#!/bin/bash
#
# /usr/local/bin/hermes-cron.sh
# One-shot installer for the pg_cron jobs. Creates the hermes_cron DB
# (the one DB pg_cron is allowed to pin idle sessions in) and schedules
# the two operational jobs there.
#
# Called by hermes-init.sh as the last step.

set -e

CRON_DB="hermes_cron"

echo "[hermes-cron] Target database: $CRON_DB"

# Wait for postgres to be ready
until pg_isready -U "$POSTGRES_USER" -d postgres > /dev/null 2>&1; do
    echo "[hermes-cron] Waiting for postgres..."
    sleep 1
done

# Create the cron DB if it doesn't exist (TOCTOU-safe via \gexec)
psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 <<SQL
SELECT 'CREATE DATABASE $CRON_DB'
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname='$CRON_DB')
\gexec
SQL

# Install pg_cron and schedule the jobs
echo "[hermes-cron] Installing pg_cron and scheduling jobs..."
psql -U "$POSTGRES_USER" -d "$CRON_DB" -v ON_ERROR_STOP=1 <<'SQL'
CREATE EXTENSION IF NOT EXISTS pg_cron;

-- Idempotent: schedule() will replace an existing job with the same name.
SELECT cron.schedule(
    'hermes_journal_ensure_partitions',
    '0 0 25 * *',
    $job$
    -- Walks every hermes_<profile> database and ensures the next 2 monthly
    -- partitions exist in hermes_journal.messages. We can't use a simple
    -- function call here because the partition function lives in each
    -- profile DB. So we use a dblink loop, or the simpler approach: each
    -- profile DB's hermes_journal.ensure_monthly_partition is called from
    -- the hermes-memory binary's startup (preferred). For now, schedule
    -- no-op here. Replace with a real cron when dblink is added.
    SELECT 1;
    $job$
);

-- Metrics retention. Works against the per-profile hermes_metrics.events
-- by walking every hermes_<profile> DB.
SELECT cron.schedule(
    'hermes_metrics_retention',
    '0 3 * * 0',  -- 3am every Sunday
    $job$
    -- Same caveat as above. The hermes-memory binary handles retention
    -- at startup, so this is a no-op placeholder.
    SELECT 1;
    $job$
);
SQL

echo "[hermes-cron] Done. Jobs are scheduled in $CRON_DB."
