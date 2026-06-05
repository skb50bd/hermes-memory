#!/usr/bin/env bash
# Step 10/11 — Smoke test: roundtrip a probe memory.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec env HERMES_STEP=10 python3 "${HERE}/_step_run.py"
