#!/usr/bin/env bash
# Step 9/11 — Tool introduction: show the agent what just got installed.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec env HERMES_STEP=9 python3 "${HERE}/_step_run.py"
