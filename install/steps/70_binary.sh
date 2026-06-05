#!/usr/bin/env bash
# Step 7/11 — Build/locate the C# binary (or download a release).

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec env HERMES_STEP=7 python3 "${HERE}/_step_run.py"
