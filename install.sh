#!/usr/bin/env bash
# hermes-memory install/update/uninstall shim
#
# Usage:
#   ./install.sh                       Interactive install (default: full wizard)
#   ./install.sh --check               Idempotent: only run steps that aren't installed
#   ./install.sh --update              Same as --check (alias)
#   ./install.sh --from N              Resume from step N (0..10)
#   ./install.sh --uninstall           Reverse every step
#   ./install.sh --step N              Run a single step
#   ./install.sh --status              Print current install state
#   ./install.sh --yes                 Non-interactive: take defaults
#   ./install.sh --profile NAME        Per-profile install: writes the DSN,
#                                      MCP server block, and memory.provider
#                                      to ~/.hermes/profiles/<NAME>/. Skip steps
#                                      0-4 (assume the shared container is up
#                                      and the per-profile role/DB already exist).
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
PROFILE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check|--update)     MODE="check";    shift ;;
        --uninstall)          MODE="uninstall"; shift ;;
        --from)               FROM_STEP="${2:-0}"; shift 2 ;;
        --step)               SINGLE_STEP="${2:-0}"; shift 2 ;;
        --status)             STATUS_ONLY=1;   shift ;;
        --yes|-y)             ASSUME_YES=1;    shift ;;
        --profile)            PROFILE="${2:-}"; shift 2 ;;
        -h|--help)
            sed -n '2,23p' "$0"
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
export HERMES_INSTALL_PROFILE="$PROFILE"

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
# In profile mode, skip the system-level steps (0-4: preflight, postgres,
# extensions, template, profiles). The shared container + per-profile role/DB
# are assumed to already exist (created by hermes-bootstrap.sh on the main
# profile). We only wire THIS profile's DSN, MCP block, and memory.provider.
if [[ -n "$PROFILE" ]]; then
    echo "=== profile mode: target=~/.hermes/profiles/${PROFILE}/ ==="
    PROFILE_HOME="$HOME/.hermes/profiles/${PROFILE}"
    if [[ ! -d "$PROFILE_HOME" ]]; then
        echo "profile dir not found: $PROFILE_HOME" >&2
        exit 4
    fi
    STEPS=(50_dsn 60_embedder 70_binary 80_mcp 90_introduce 100_smoke 110_summary)
else
    STEPS=(00_preflight 10_postgres 20_extensions 30_template 40_profiles \
           50_dsn 60_embedder 70_binary 80_mcp 90_introduce 100_smoke 110_summary)
fi

if [[ "$MODE" == "uninstall" ]]; then
    echo "=== uninstall: running steps in reverse ==="
    # _step_run.py is the orchestrator for uninstall — it iterates
    # REVERSE_STEPS in reverse step order. The per-step shims are
    # install-path only; calling them would duplicate work.
    export HERMES_STEP=""
    export HERMES_INSTALL_MODE="uninstall"
    export HERMES_REPO_ROOT="$REPO_ROOT"
    exec python3 "$STEPS_DIR/_step_run.py"
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
