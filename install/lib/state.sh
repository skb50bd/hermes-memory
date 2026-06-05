#!/usr/bin/env bash
# install/lib/state.sh — read/write the install state file.
#
# State file: $HERMES_STATE_DIR/hermes-memory.json
# Default state dir: $HOME/.hermes/state
# Format: JSON. All reads/writes are funneled through a single python
# helper to avoid bash↔python string-escaping bugs.

[[ -n "${__HERMES_INSTALL_STATE_LOADED:-}" ]] && return 0
__HERMES_INSTALL_STATE_LOADED=1

STATE_DIR="${HERMES_STATE_DIR:-$HOME/.hermes/state}"
STATE_FILE="${STATE_DIR}/hermes-memory.json"

# Initialize the state file with a known-good empty shape.
state::init() {
    mkdir -p "${STATE_DIR}"
    if [[ ! -f "${STATE_FILE}" ]]; then
        python3 - <<'PY' > "${STATE_FILE}"
import json
print(json.dumps({
    "version": "0.1.0",
    "installed_at": None,
    "last_checked_at": None,
    "container": {},
    "databases": {},
    "embedder": {},
    "mcp": {},
    "python_plugin": {}
}, indent=2))
PY
    fi
}

# Internal: run python3 with the state file path. Used by all helpers.
# The state.py script lives next to this file.
__state_py() {
    local cmd="$1"; shift
    python3 "${BASH_SOURCE[0]%/*}/state.py" "${STATE_FILE}" "$cmd" "$@"
}

# state::get <dotted.path>
# Prints the value at the path, or empty string if missing.
state::get() {
    __state_py get "$1"
}

# state::has <dotted.path> — returns 0 if present, 1 if not
state::has() {
    local v
    v="$(state::get "$1")"
    [[ -n "$v" ]]
}

# state::set <dotted.path> <value>  (value is treated as a string)
state::set() {
    __state_py set "$1" "$2"
}

# state::set_json <dotted.path> <json-value>
# Stores a JSON-decoded value (dict, list, number, etc.)
state::set_json() {
    __state_py set_json "$1" "$2"
}

# state::get_json <dotted.path>
# Prints the value as JSON, or empty if missing
state::get_json() {
    __state_py get_json "$1"
}

# state::now — set last_checked_at to current ISO timestamp
state::now() {
    local now
    now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    __state_py set "last_checked_at" "$now" >/dev/null
}

# state::set_installed_at — same but for installed_at
state::set_installed_at() {
    local now
    now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    __state_py set "installed_at" "$now" >/dev/null
}

# state::load / state::save — no-ops; reads/writes happen on every set/get.
# Kept for source compat with the step scripts that call them.
state::load() { :; }
state::save() { :; }
