#!/usr/bin/env bash
# Step 3/11 — Create hermes_template DB and apply all 9 migrations.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec env HERMES_STEP=3 python3 "${HERE}/_step_run.py"
