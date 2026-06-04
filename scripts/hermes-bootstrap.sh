#!/usr/bin/env bash
# hermes-memory self-hostable bootstrap.
#
# Idempotent. Assumes a hermes-postgres container is already running
# (the image built in CI and published to GHCR, run via
# `docker compose -f compose/compose.yaml up -d`).
#
# What it does:
#   1. Verifies the hermes-postgres container is reachable AND is the
#      hermes-postgres image (refuses to operate on stock pgvector)
#   2. Creates `hermes_template` DB (if missing) and applies all 9
#      migrations from the repo
#   3. Discovers Hermes profiles (~/.hermes/profiles/<name>/)
#      and creates `hermes_<profile>` DB as a TEMPLATE clone
#   4. Writes PG_MEM_DB_CONN_STR into per-profile .env files pointing
#      at the right DB
#   5. Falls back to writing to ~/.hermes/.env if no profiles dir
#
# Usage:
#   ./scripts/hermes-bootstrap.sh                  # apply
#   ./scripts/hermes-bootstrap.sh --dry-run        # preview only
#
# Env overrides:
#   HERMES_PG_CONTAINER     container name (default: hermes-postgres)
#   HERMES_PG_USER          postgres role (default: hermes)
#   HERMES_PG_PASSWORD      role password (default: changeme)
#   HERMES_PG_HOST          host the container's 5432 maps to (default: 127.0.0.1)
#   HERMES_PG_PORT          host port (default: 5432)
#   HERMES_MEMORY_REPO      path to hermes-memory repo
#   HERMES_HOME             path to hermes-agent home
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
REPO_ROOT="${HERMES_MEMORY_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
PG_CONTAINER="${HERMES_PG_CONTAINER:-hermes-postgres}"
PG_USER="${HERMES_PG_USER:-hermes}"
PG_PASSWORD="${HERMES_PG_PASSWORD:-changeme}"
PG_HOST="${HERMES_PG_HOST:-127.0.0.1}"
PG_PORT="${HERMES_PG_PORT:-5432}"

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" || "${HERMES_DRY_RUN:-0}" == "1" ]]; then
    DRY_RUN=1
fi

log()  { printf '\033[1;34m[hermes-bootstrap]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[hermes-bootstrap]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[hermes-bootstrap]\033[0m %s\n' "$*" >&2; exit 1; }

# Wrapper that respects --dry-run
pg_exec() {
    if [[ "${DRY_RUN}" == "1" ]]; then
        echo "  [dry-run] docker exec -i ${PG_CONTAINER} psql -U ${PG_USER} $*"
    else
        docker exec -i "${PG_CONTAINER}" psql -U "${PG_USER}" "$@"
    fi
}

[[ "${DRY_RUN}" == "1" ]] && log "DRY RUN — no changes will be made"

# ── 1. Verify the hermes-postgres container is up and is the right image ─
log "checking hermes-postgres container is running"
if ! docker ps --format '{{.Names}}' | grep -qx "${PG_CONTAINER}"; then
    die "container '${PG_CONTAINER}' not running. Start it with:
  cd ${REPO_ROOT}
  docker compose -f compose/compose.yaml up -d
  # or, if you've already customized compose in ~/infra/, your usual command.
  # If the container has a different name, set HERMES_PG_CONTAINER=<name>."
fi

# Image sanity check — refuse to operate on stock pgvector because it
# lacks the postgis/timescaledb/age extensions the hermes_template
# migration expects.
image_name=$(docker inspect "${PG_CONTAINER}" --format '{{.Config.Image}}' 2>/dev/null || echo "")
if [[ "${image_name}" != hermes-postgres* ]] && [[ "${image_name}" != ghcr.io/skb50bd/hermes-memory/hermes-postgres* ]]; then
    die "container '${PG_CONTAINER}' is using image '${image_name}', which is not the hermes-postgres image built in CI.
  Run the hermes-postgres container instead. The bootstrap needs the
  extensions (postgis, timescaledb, age) bundled into the hermes-postgres image.
  Suggested override:
    HERMES_PG_CONTAINER=hermes-postgres  # or the actual container name"
fi
log "container image: ${image_name} ✓"

docker exec -i "${PG_CONTAINER}" pg_isready -U "${PG_USER}" >/dev/null \
    || die "postgres not ready in container '${PG_CONTAINER}'"

# ── 2. Create hermes_template and apply all migrations ─────────────────
TEMPLATE_DB=hermes_template
log "ensuring template database '${TEMPLATE_DB}' exists"
exists=$(pg_exec -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='${TEMPLATE_DB}'" || true)
if [[ "${exists}" != "1" ]]; then
    pg_exec -d postgres -c "CREATE DATABASE ${TEMPLATE_DB}" >/dev/null
fi

log "applying schema to ${TEMPLATE_DB} (using migrations bundled in image at /usr/migrations)"
if [[ "${DRY_RUN}" == "1" ]]; then
    echo "  [dry-run] docker exec -i ${PG_CONTAINER} psql -U ${PG_USER} -d ${TEMPLATE_DB} -v ON_ERROR_STOP=1 -f /usr/local/share/hermes/01-schemas.sql"
else
    docker exec -i "${PG_CONTAINER}" \
        psql -U "${PG_USER}" -d "${TEMPLATE_DB}" -v ON_ERROR_STOP=1 \
        -f /usr/local/share/hermes/01-schemas.sql \
        || die "schema apply failed — see output above"
fi

# ── 3. Discover profiles and create per-profile DBs ────────────────────
create_profile_db() {
    local profile="$1"
    local db_name="hermes_${profile}"
    log "creating per-profile database '${db_name}' (clone of ${TEMPLATE_DB})"
    pg_exec -d postgres -c "DROP DATABASE IF EXISTS ${db_name}" >/dev/null
    pg_exec -d postgres -c "CREATE DATABASE ${db_name} TEMPLATE ${TEMPLATE_DB} CONNECTION LIMIT 20" \
        || die "failed to clone ${TEMPLATE_DB} → ${db_name}"
    echo "${db_name}"
}

PG_DSN="postgresql://${PG_USER}:***@${PG_HOST}:${PG_PORT}"

write_profile_env() {
    local profile="$1"
    local db_name="$2"
    local env_file="$3"
    local line="PG_MEM_DB_CONN_STR=\"${PG_DSN}/${db_name}\""
    if [[ "${DRY_RUN}" == "1" ]]; then
        echo "  [dry-run] write ${env_file} → ${line}"
        return
    fi
    mkdir -p "$(dirname "${env_file}")"
    if [[ -f "${env_file}" ]] && grep -q "^PG_MEM_DB_CONN_STR=" "${env_file}"; then
        sed -i "s#^PG_MEM_DB_CONN_STR=.*#${line}#" "${env_file}"
    else
        printf '\n%s\n' "${line}" >> "${env_file}"
    fi
}

PROFILES_DIR="${HERMES_HOME}/profiles"
shopt -s nullglob
profile_dirs=("${PROFILES_DIR}"/*/)
shopt -u nullglob

if [[ ${#profile_dirs[@]} -gt 0 ]]; then
    for prof_dir in "${profile_dirs[@]}"; do
        profile="$(basename "${prof_dir}")"
        [[ "${profile}" == "_templates" ]] && continue
        db_name="$(create_profile_db "${profile}")"
        env_file="${prof_dir}.env"
        write_profile_env "${profile}" "${db_name}" "${env_file}"
        log "wrote ${env_file} → ${PG_DSN}/${db_name}"
    done
else
    log "no profiles dir at ${PROFILES_DIR} (or it's empty) — using single default profile"
    db_name="$(create_profile_db default)"
    env_file="${HERMES_HOME}/.env"
    write_profile_env default "${db_name}" "${env_file}"
    log "wrote ${env_file} → ${PG_DSN}/${db_name}"
fi

log "done. restart Hermes Agent to pick up the new DSN:"
log "  hermes gateway restart"
