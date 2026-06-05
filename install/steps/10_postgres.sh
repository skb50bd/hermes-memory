#!/usr/bin/env bash
# Step 1/11 — Start the hermes-postgres container.
#
# Idempotent. Re-runnable. Pulls image (or falls back to local pre-built),
# starts via docker compose, waits for Postgres healthcheck.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec env HERMES_STEP=1 python3 "${HERE}/_step_run.py"
