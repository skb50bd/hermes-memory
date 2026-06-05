#!/usr/bin/env bash
# hermes-memory install/update/uninstall shim
#
# Usage:
#   ./install.sh                  Interactive install (default: full wizard)
#   ./install.sh --check          Idempotent: only run steps that aren't installed
#   ./install.sh --update         Same as --check (alias)
#   ./install.sh --from N         Resume from step N (0..10)
#   ./install.sh --uninstall      Reverse every step
#   ./install.sh --step N         Run a single step
#   ./install.sh --status         Print current install state
#   ./install.sh --yes            Non-interactive: take defaults
#
# This is a thin wrapper. All logic lives in install/steps/_step_run.py.

set -euo pipefail

# Resolve repo root from the script's location
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STEPS_DIR="$REPO_ROOT/install/steps"

# ─── Args ─────────────────────────────────────────────────────────────────
MODE="install"
FROM_STEP=0
SINGLE_STEP=""
STATUS_ONLY=0
ASSUME_YES=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check|--update)     MODE="check";    shift ;;
        --uninstall)          MODE="uninstall"; shift ;;
        --from)               FROM_STEP="${2:-0}"; shift 2 ;;
        --step)               SINGLE_STEP="${2:-0}"; shift 2 ;;
        --status)             STATUS_ONLY=1;   shift ;;
        --yes|-y)             ASSUME_YES=1;    shift ;;
        -h|--help)
            sed -n '2,20p' "$0"
            exit 0
            ;;
        *)
            echo "unknown flag: $1" >&2
            exit 2
            ;;
    esac
done

# ─── Env ──────────────────────────────────────────────────────────────────
export HERMES_REPO_ROOT="$REPO_ROOT"
export HERMES_INSTALL_MODE="$MODE"
export HERMES_ASSUME_YES="$ASSUME_YES"

# ─── Status only ──────────────────────────────────────────────────────────
if [[ "$STATUS_ONLY" == "1" ]]; then
    STATE_FILE="${HERMES_STATE_DIR:-$HOME/.hermes/state}/hermes-memory.json"
    if [[ -f "$STATE_FILE" ]]; then
        cat "$STATE_FILE"
    else
        echo "(no state file — nothing installed yet)"
        echo "  expected: $STATE_FILE"
    fi
    exit 0
fi

# ─── Single step ──────────────────────────────────────────────────────────
if [[ -n "$SINGLE_STEP" ]]; then
    HERMES_STEP="$SINGLE_STEP" bash "$STEPS_DIR/_dispatch.sh"
    exit $?
fi

# ─── Multi-step ───────────────────────────────────────────────────────────
STEPS=(00_preflight 10_postgres 20_extensions 30_template 40_profiles \
       50_dsn 60_embedder 70_binary 80_mcp 90_introduce 100_smoke 110_summary)

if [[ "$MODE" == "uninstall" ]]; then
    echo "=== uninstall: running steps in reverse ==="
    for s in $(printf '%s\n' "${STEPS[@]}" | tac); do
        n="${s%%_*}"
        echo "── $n ──"
        HERMES_STEP="$n" HERMES_INSTALL_MODE="uninstall" bash "$STEPS_DIR/_dispatch.sh" || {
            echo "step $n failed — continuing in reverse" >&2
        }
    done
    echo "=== uninstall complete ==="
    exit 0
fi

# install / check: run forward, optionally skipping completed steps
echo "=== ${MODE} ==="
for s in "${STEPS[@]}"; do
    n="${s%%_*}"
    if [[ "$MODE" == "check" ]] && [[ "$n" -lt "$FROM_STEP" ]]; then
        # in --check mode, FROM_STEP means "minimum step to consider"
        # (all earlier steps are checked/skipped)
        continue
    fi
    echo "── $n ──"
    HERMES_STEP="$n" bash "$STEPS_DIR/_dispatch.sh"
done
echo "=== ${MODE} complete ==="
