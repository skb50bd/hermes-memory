#!/usr/bin/env bash
# Step 11/11 — Post-install summary.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec env HERMES_STEP=11 python3 "${HERE}/_step_run.py"
