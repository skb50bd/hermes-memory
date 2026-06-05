#!/usr/bin/env bash
# Step 6/11 — Configure embedder provider.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec env HERMES_STEP=6 python3 "${HERE}/_step_run.py"
