#!/usr/bin/env bash
# Step 4/11 — Create per-profile databases.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec env HERMES_STEP=4 python3 "${HERE}/_step_run.py"
