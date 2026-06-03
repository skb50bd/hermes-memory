#!/bin/bash
#
# /usr/local/bin/hermes-init.sh
# Bundled in the image. Idempotent. Initializes the hermes_template DB
# with the 6 extensions and 5 schemas. Run once after first boot:
#
#   docker exec <pg-container> /usr/local/bin/hermes-init.sh
#
# Or set HERMES_AUTO_INIT=1 in the container env to run automatically
# on first start (after initdb completes).

set -e

DB="${HERMES_TEMPLATE_DB:-hermes_template}"

echo "[hermes-init] Target database: $DB"

echo "[hermes-init] Target database: $DB"

# Wait for postgres to be ready
until pg_isready -U "$POSTGRES_USER" -d postgres > /dev/null 2>&1; do
    echo "[hermes-init] Waiting for postgres..."
    sleep 1
done

# Create the template DB if it doesn't exist
DB_EXISTS=$(psql -U "$POSTGRES_USER" -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$DB'" 2>/dev/null || echo "")

if [ -z "$DB_EXISTS" ]; then
    echo "[hermes-init] Creating database $DB..."
    psql -U "$POSTGRES_USER" -d postgres -c "CREATE DATABASE $DB"
else
    echo "[hermes-init] Database $DB already exists, skipping create."
fi

# Install the 6 extensions (minus pg_cron — it lives in hermes_cron)
echo "[hermes-init] Installing extensions..."
# pg_cron would pin a worker connection to this DB, blocking future
# `CREATE DATABASE ... TEMPLATE hermes_template` clones. It is installed
# separately by hermes-cron.sh in its own dedicated `hermes_cron` DB.
psql -U "$POSTGRES_USER" -d "$DB" -v ON_ERROR_STOP=1 <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS ltree;          -- hierarchical categories (PG 18+ moved this to an extension)
CREATE EXTENSION IF NOT EXISTS postgis;
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
