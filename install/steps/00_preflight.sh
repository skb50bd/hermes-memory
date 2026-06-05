#!/usr/bin/env bash
# Step 0/11 — Preflight. Verify environment before doing anything destructive.
#
# Idempotent: yes (just checks, doesn't mutate state).
# Re-runnable: yes.
# Skip-if-done: no (always re-runs; cheap).

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="${HERE}/../lib"
source "${LIB}/ui.sh"
source "${LIB}/detect.sh"
source "${LIB}/state.sh"

ui::step "0/11" "Preflight"

# Tools
DOCKER_PATH="$(detect::docker || true)"
if [[ -z "$DOCKER_PATH" ]]; then
    ui::fail "docker not found in PATH. Install: https://docs.docker.com/engine/install/"
    exit 1
fi
ui::ok "docker: $DOCKER_PATH"

# Compose v2
if ! docker compose version >/dev/null 2>&1; then
    ui::fail "docker compose v2 not available. Install: https://docs.docker.com/compose/install/"
    exit 1
fi
ui::ok "docker compose v2: $(docker compose version --short 2>/dev/null)"

# hermes CLI
HERMES_PATH="$(detect::hermes || true)"
if [[ -z "$HERMES_PATH" ]]; then
    ui::fail "hermes CLI not found in PATH. Install: pip install hermes-agent (or your usual method)"
    exit 1
fi
ui::ok "hermes CLI: $HERMES_PATH"

# Repo
REPO_ROOT="$(detect::repo_root)"
if [[ ! -f "${REPO_ROOT}/compose/compose.yaml" ]]; then
    ui::fail "Repo root not found or compose/compose.yaml missing"
    ui::info "Expected: ${REPO_ROOT}/compose/compose.yaml"
    exit 1
fi
ui::ok "repo: $REPO_ROOT"

# Hermes home
HERMES_HOME_DIR="$(detect::hermes_home)"
if [[ ! -d "$HERMES_HOME_DIR" ]]; then
    ui::info "Creating $HERMES_HOME_DIR"
    mkdir -p "$HERMES_HOME_DIR"
fi
ui::ok "hermes home: $HERMES_HOME_DIR"

# Port — detect from running container if present, else use env, then default.
# Convention: regular port + 5000 (10432) so we don't collide with system
# Postgres. HERMES_PG_HOST_PORT wins if set. Last-resort fallbacks: 10432
# (new default), then 5444 (historic choice), then 5432 (literal default).
HOST_PORT="${HERMES_PG_HOST_PORT:-}"
CONTAINER_NAME="${HERMES_POSTGRES_CONTAINER:-hermes-postgres}"
if [[ -z "$HOST_PORT" ]]; then
    DETECTED="$(docker inspect "$CONTAINER_NAME" -f '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}' 2>/dev/null || true)"
    if [[ "$DETECTED" =~ ^[0-9]+$ ]]; then
        HOST_PORT="$DETECTED"
        ui::ok "detected running container '$CONTAINER_NAME' on port $HOST_PORT"
    else
        HOST_PORT="10432"
    fi
fi
if [[ "$(detect::port_free "$HOST_PORT")" == "free" ]]; then
    ui::ok "port $HOST_PORT free on host"
else
    ui::warn "port $HOST_PORT is in use. Set HERMES_PG_HOST_PORT to a free port."
fi

# Internet
if detect::internet; then
    ui::ok "github.com reachable (for image pull)"
else
    ui::warn "github.com unreachable — image pull will fail in a moment. Check your network."
fi

# OS / arch
ui::ok "OS: $(detect::os)  /  arch: $(detect::arch)"

# python3 (used by some helpers)
if ! command -v python3 >/dev/null 2>&1; then
    ui::fail "python3 not found in PATH. Required for the install wizard."
    exit 1
fi
ui::ok "python3: $(command -v python3)"

# Summary
ui::rule "─────────────────────────────────────────"
ui::info "Docker, hermes CLI, and repo are all present."
ui::info "Proceeding to step 1."

# Save preflight state
state::set "preflight.docker" "$DOCKER_PATH"
state::set "preflight.hermes" "$HERMES_PATH"
state::set "preflight.repo" "$REPO_ROOT"
state::set "preflight.os" "$(detect::os)"
state::set "preflight.arch" "$(detect::arch)"
state::set "preflight.host_port" "$HOST_PORT"
state::now
