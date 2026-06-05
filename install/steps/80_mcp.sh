#!/usr/bin/env bash
# Step 8/11 — Register the C# binary as an MCP server.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec env HERMES_STEP=8 python3 "${HERE}/_step_run.py"
