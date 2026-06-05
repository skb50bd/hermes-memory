#!/usr/bin/env bash
# Internal dispatcher — used by install.sh and by the C# InstallCommand.
# Reads HERMES_STEP from env and execs the matching NN_*.sh shim.
set -euo pipefail
step="${HERMES_STEP:-}"
if [[ -z "$step" ]]; then
    echo "HERMES_STEP not set" >&2
    exit 2
fi
# Pad step to 2 digits
step_pad=$(printf "%02d" "$((10#$step))")
shim="$(dirname "${BASH_SOURCE[0]}")/${step_pad}_"*.sh
# shellcheck disable=SC2086
shim=$(ls $shim 2>/dev/null | head -1)
if [[ -z "$shim" || ! -f "$shim" ]]; then
    echo "no shim for step $step (looked for ${step_pad}_*.sh)" >&2
    exit 3
fi
exec bash "$shim"
