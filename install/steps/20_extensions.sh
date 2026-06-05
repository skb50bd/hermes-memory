#!/usr/bin/env bash
# Step 2/11 — Verify all 5 extensions are present.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec env HERMES_STEP=2 python3 "${HERE}/_step_run.py"
