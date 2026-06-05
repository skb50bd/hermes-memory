#!/usr/bin/env bash
# install/lib/compose.sh — docker compose wrappers.

[[ -n "${__HERMES_INSTALL_COMPOSE_LOADED:-}" ]] && return 0
__HERMES_INSTALL_COMPOSE_LOADED=1

_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# detect::repo_root
# Source detect.sh for the helper if not already loaded.
[[ -z "${__HERMES_INSTALL_DETECT_LOADED:-}" ]] && source "${_LIB_DIR}/detect.sh"

COMPOSE_FILE="${HERMES_MEMORY_REPO:-$(detect::repo_root)}/compose/compose.yaml"
COMPOSE_PROJECT="hermes-memory"

# compose::cmd <args...> — invokes `docker compose -f <file> -p <project> ...`
# Uses v2 (`docker compose` with space). Errors if v1 (with hyphen) is the only one.
compose::cmd() {
    if ! docker compose version >/dev/null 2>&1; then
        echo "  ! docker compose v2 not found. Install: https://docs.docker.com/compose/install/" >&2
        return 1
    fi
    if [[ "${HERMES_DRY_RUN:-0}" == "1" ]]; then
        echo "  [dry-run] docker compose -f $COMPOSE_FILE -p $COMPOSE_PROJECT $*" >&2
        return 0
    fi
    docker compose -f "$COMPOSE_FILE" -p "$COMPOSE_PROJECT" "$@"
}

# compose::up [-d]
compose::up() {
    compose::cmd up "$@"
}

# compose::down [-v]
compose::down() {
    compose::cmd down "$@"
}

# compose::ps — list container status
compose::ps() {
    compose::cmd ps 2>/dev/null
}

# compose::logs <service> [args...]
compose::logs() {
    compose::cmd logs "$@"
}

# compose::is_up — returns 0 if the hermes-postgres container is running
compose::is_up() {
    local name="${HERMES_PG_CONTAINER:-hermes-postgres}"
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx "$name"; then
        return 0
    fi
    # Also check via compose project label, which is more robust
    if docker ps -a --format '{{.Names}} {{.Label "com.docker.compose.service"}}' 2>/dev/null \
        | awk '$2 == "postgres" {print $1}' | grep -qx "$name"; then
        # Container exists; check if it's actually running
        docker ps --format '{{.Names}}' | grep -qx "$name" && return 0 || return 1
    fi
    return 1
}

# compose::image_running — print the image name of the running hermes-postgres container
compose::image_running() {
    local name="${HERMES_PG_CONTAINER:-hermes-postgres}"
    docker inspect "$name" --format '{{.Config.Image}}' 2>/dev/null
}

# compose::container_id — print the container ID
compose::container_id() {
    local name="${HERMES_PG_CONTAINER:-hermes-postgres}"
    docker inspect "$name" --format '{{.Id}}' 2>/dev/null
}
