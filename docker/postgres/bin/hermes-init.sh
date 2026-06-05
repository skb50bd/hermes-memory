#!/bin/bash
#
# /usr/local/bin/hermes-init.sh
# Bundled in the image. Idempotent. Initializes the hermes_template DB
# with the 5 extensions and 5 schemas. Run once after first boot:
#
#   docker exec <pg-container> /usr/local/bin/hermes-init.sh
#
# Or set HERMES_AUTO_INIT=1 in the container env to run automatically
# on first start (after initdb completes).
#
# Concurrency: this script can be invoked from two places on a fresh
# container — the /docker-entrypoint-initdb.d/ symlink (runs in
# docker_temp_server during first boot) AND an explicit `docker exec`
# call (e.g. from CI smoke tests). These race on the same DBs. We
# serialize via a session-level advisory lock that is held for the
# entire duration of this script's work, and use \gexec to avoid the
# check-then-create TOCTOU window inside a single session.

set -e

DB="${HERMES_TEMPLATE_DB:-hermes_template}"

echo "[hermes-init] Target database: $DB"

# Wait for postgres to be ready
until pg_isready -U "$POSTGRES_USER" -d postgres > /dev/null 2>&1; do
    echo "[hermes-init] Waiting for postgres..."
    sleep 1
done

# Acquire advisory lock + create the template DB + release the lock,
# all in one psql session. The lock prevents a concurrent invocation
# of this script (e.g. the init.d symlink) from racing with us.
#   lockid = hashtext('hermes_init') & 0x7FFFFFFF
psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 <<SQL
SELECT pg_advisory_lock(hashtext('hermes_init')::int);
SELECT 'CREATE DATABASE $DB'
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname='$DB')
\gexec
SELECT pg_advisory_unlock(hashtext('hermes_init')::int);
SQL

# Install the 5 extensions (minus pg_cron — it lives in hermes_cron)
echo "[hermes-init] Installing extensions..."
# pg_cron would pin a worker connection to this DB, blocking future
# `CREATE DATABASE ... TEMPLATE hermes_template` clones. It is installed
# separately by hermes-cron.sh in its own dedicated `hermes_cron` DB.
psql -U "$POSTGRES_USER" -d "$DB" -v ON_ERROR_STOP=1 <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS ltree;          -- hierarchical categories (PG 18+ moved this to an extension)
CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS age;
SQL

# Install the 5 schemas
echo "[hermes-init] Installing schemas in $DB..."
psql -U "$POSTGRES_USER" -d "$DB" -v ON_ERROR_STOP=1 -f /usr/local/share/hermes/01-schemas.sql

# Last step: install pg_cron in its own dedicated DB. This is what lets
# the workers idle without blocking profile DB clones.
echo "[hermes-init] Setting up pg_cron in hermes_cron..."
/usr/local/bin/hermes-cron.sh

echo "[hermes-init] Done. $DB is ready for 'hermes-memory profile create <name>'."
