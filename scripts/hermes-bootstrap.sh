#!/usr/bin/env bash
# hermes-memory self-hostable bootstrap.
#
# Idempotent. Run once on any host that has:
#   - docker (postgres container running with pgvector)
#   - psql client (only needed inside the container; not on host)
#   - hermes-agent installed at ~/.hermes/hermes-agent
#
# What it does:
#   1. Verifies the postgres container is reachable
#   2. Installs the 3 minimum extensions (vector, pg_trgm, ltree)
#   3. Creates `hermes_template` DB (if missing) and applies all migrations
#   4. Discovers Hermes profiles (~/.hermes/profiles/<name>/)
#      and creates `hermes_<profile>` DB as a TEMPLATE clone
#   5. Updates ~/.hermes/profiles/<name>/.env with PG_MEM_DB_CONN_STR
#      pointing at the right per-profile DB
#   6. Falls back to writing to ~/.hermes/.env if no profiles dir
#
# Re-run safely: existing DBs are dropped & recloned (picks up schema
# changes), per-profile .env is updated in place.
#
# Usage:
#   ./scripts/hermes-bootstrap.sh                  # apply
#   ./scripts/hermes-bootstrap.sh --dry-run        # preview only
#
# Env overrides:
#   HERMES_PG_CONTAINER  container name (default: postgres)
#   HERMES_PG_USER       postgres role (default: postgres)
#   HERMES_PG_PASSWORD   role password (default: postgres)
#   HERMES_PG_HOST       host the container's 5432 maps to (default: 127.0.0.1)
#   HERMES_PG_PORT       host port (default: 5432)
#   HERMES_MEMORY_REPO   path to hermes-memory repo (default: parent of script)
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
REPO_ROOT="${HERMES_MEMORY_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
PG_CONTAINER="${HERMES_PG_CONTAINER:-postgres}"
PG_USER="${HERMES_PG_USER:-postgres}"
PG_PASSWORD="${HERMES_PG_PASSWORD:-postgres}"
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

# ── 1. Verify postgres is reachable ─────────────────────────────────────
log "checking postgres container '${PG_CONTAINER}' is running"
if ! docker ps --format '{{.Names}}' | grep -qx "${PG_CONTAINER}"; then
    die "container '${PG_CONTAINER}' not running. Start it with:
  docker run -d --name ${PG_CONTAINER} \\
    -e POSTGRES_PASSWORD=${PG_PASSWORD} \\
    -e POSTGRES_USER=${PG_USER} \\
    -e POSTGRES_DB=postgres \\
    -p ${PG_PORT}:5432 \\
    pgvector/pgvector:pg18"
fi
docker exec -i "${PG_CONTAINER}" pg_isready -U "${PG_USER}" >/dev/null \
    || die "postgres not ready in container '${PG_CONTAINER}'"

# ── 2. Install extensions on the default 'postgres' DB (idempotent) ────
log "ensuring extensions (vector, pg_trgm, ltree) on postgres DB"
if [[ "${DRY_RUN}" == "1" ]]; then
    echo "  [dry-run] CREATE EXTENSION IF NOT EXISTS vector / pg_trgm / ltree"
else
    docker exec -i "${PG_CONTAINER}" psql -U "${PG_USER}" -d postgres -v ON_ERROR_STOP=1 >/dev/null <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS ltree;
SQL
fi

# ── 3. Create hermes_template and apply all migrations ─────────────────
TEMPLATE_DB=hermes_template
log "ensuring template database '${TEMPLATE_DB}' exists"
exists=$(pg_exec -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='${TEMPLATE_DB}'" || true)
if [[ "${exists}" != "1" ]]; then
    pg_exec -d postgres -c "CREATE DATABASE ${TEMPLATE_DB}" >/dev/null
fi

log "copying migrations into container"
if [[ "${DRY_RUN}" == "1" ]]; then
    echo "  [dry-run] docker cp ${REPO_ROOT}/migrations ${PG_CONTAINER}:/tmp/hermes-mem-migrations"
    echo "  [dry-run] docker cp ${REPO_ROOT}/docker/postgres/bin/01-schemas.sql ${PG_CONTAINER}:/tmp/01-schemas.sql"
else
    docker cp "${REPO_ROOT}/migrations" "${PG_CONTAINER}:/tmp/hermes-mem-migrations"
    docker cp "${REPO_ROOT}/docker/postgres/bin/01-schemas.sql" "${PG_CONTAINER}:/tmp/01-schemas.sql"
fi

log "applying schema to ${TEMPLATE_DB}"
if [[ "${DRY_RUN}" == "1" ]]; then
    echo "  [dry-run] psql -U ${PG_USER} -d ${TEMPLATE_DB} -f /tmp/01-schemas.sql"
else
    docker exec -w /tmp/hermes-mem-migrations -i "${PG_CONTAINER}" \
        psql -U "${PG_USER}" -d "${TEMPLATE_DB}" -v ON_ERROR_STOP=1 \
        -f /tmp/01-schemas.sql \
        || die "schema apply failed — see output above"
fi

# ── 4. Discover profiles and create per-profile DBs ────────────────────
create_profile_db() {
    local profile="$1"
    local db_name="hermes_${profile}"
    log "creating per-profile database '${db_name}' (clone of ${TEMPLATE_DB})"
    pg_exec -d postgres -c "DROP DATABASE IF EXISTS ${db_name}" >/dev/null
    pg_exec -d postgres -c "CREATE DATABASE ${db_name} TEMPLATE ${TEMPLATE_DB} CONNECTION LIMIT 20" \
        || die "failed to clone ${TEMPLATE_DB} → ${db_name}"
    echo "${db_name}"
}

PG_DSN="postgresql://${PG_USER}:${PG_PASSWORD}@${PG_HOST}:${PG_PORT}"

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
        # In-place update
        sed -i "s|^PG_MEM_DB_CONN_STR=.*|${line}|" "${env_file}"
    else
        printf '\n%s\n' "${line}" >> "${env_file}"
    fi
}

PROFILES_DIR="${HERMES_HOME}/profiles"
if [[ -d "${PROFILES_DIR}" ]]; then
    shopt -s nullglob
    for prof_dir in "${PROFILES_DIR}"/*/; do
        profile="$(basename "${prof_dir}")"
        [[ "${profile}" == "_templates" ]] && continue
        db_name="$(create_profile_db "${profile}")"
        env_file="${prof_dir}.env"
        write_profile_env "${profile}" "${db_name}" "${env_file}"
        log "wrote ${env_file} → ${PG_DSN}/${db_name}"
    done
    shopt -u nullglob
else
    log "no profiles dir found at ${PROFILES_DIR} — using single default profile"
    db_name="$(create_profile_db default)"
    env_file="${HERMES_HOME}/.env"
    write_profile_env default "${db_name}" "${env_file}"
    log "wrote ${env_file} → ${PG_DSN}/${db_name}"
fi

log "done. restart Hermes Agent to pick up the new DSN:"
log "  hermes gateway restart"
