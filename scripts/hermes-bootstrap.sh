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
#   2. Ensures the `hermes` superuser role has a random password,
#      persisted at ~/.hermes/state/hermes-postgres.password
#   3. Creates `hermes_template` DB (if missing) and applies all 9
#      migrations from the repo
#   4. Discovers Hermes profiles (~/.hermes/profiles/<name>/)
#      and for each one:
#        a. Creates a `hermes_<name>` LOGIN role with its own random
#           password, persisted at ~/.hermes/state/hermes-pg-<name>.password
#        b. Creates a `hermes_<name>` DB as a TEMPLATE clone, owned by
#           that role, with CONNECTION LIMIT 20
#        c. Writes PG_MEM_DB_CONN_STR (Python plugin) and
#           HERMES_PG_CONN_STR (C# MCP) into the profile's .env
#   5. Falls back to creating a single `default` profile if no
#      profiles dir exists
#   6. Writes the superuser password into compose/.env so the
#      container can be restarted with the same credentials
#
# Usage:
#   ./scripts/hermes-bootstrap.sh                  # apply
#   ./scripts/hermes-bootstrap.sh --dry-run        # preview only
#
# Env overrides:
#   HERMES_PG_CONTAINER     container name (default: hermes-postgres)
#   HERMES_PG_USER          postgres role (default: hermes)
#   HERMES_PG_HOST          host the container's 5432 maps to (default: 127.0.0.1)
#   HERMES_PG_PORT          host port (default: 10432, regular + 5000)
#                           Pass 5444 here for the historic install.
#   HERMES_MEMORY_REPO      path to hermes-memory repo
#   HERMES_HOME             path to hermes-agent home
#   HERMES_PG_KEEP_PASSWORD  if "1", do not regenerate existing role passwords
#   HERMES_PG_PASSWORD_LEN  password length (default: 24)
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
REPO_ROOT="${HERMES_MEMORY_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
PG_CONTAINER="${HERMES_PG_CONTAINER:-hermes-postgres}"
PG_USER="${HERMES_PG_USER:-hermes}"
# If HERMES_PG_PASSWORD is set, the bootstrap uses it for the superuser
# (useful for restoring from a known password). Otherwise a random
# password is generated and persisted to ~/.hermes/state/.
PG_PASSWORD="${HERMES_PG_PASSWORD:-}"
PG_HOST="${HERMES_PG_HOST:-127.0.0.1}"
PG_PORT="${HERMES_PG_PORT:-10432}"
KEEP_PASSWORD="${HERMES_PG_KEEP_PASSWORD:-0}"
PW_LEN="${HERMES_PG_PASSWORD_LEN:-24}"

DRY_RUN=0
if [[ "${1:-}" == "--dry-run" || "${HERMES_DRY_RUN:-0}" == "1" ]]; then
    DRY_RUN=1
fi

log()  { printf '\033[1;34m[hermes-bootstrap]\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33m[hermes-bootstrap]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[hermes-bootstrap]\033[0m %s\n' "$*" >&2; exit 1; }

# Generate a cryptographically random alphanumeric password of length $1
# (defaults to global PW_LEN). Uses /dev/urandom via openssl when available,
# falls back to /dev/urandom + tr.
random_password() {
    local len="${1:-$PW_LEN}"
    if command -v openssl >/dev/null 2>&1; then
        # Base64 of $len bytes, then strip non-alphanumeric chars; pad to length
        openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c "$len"
    else
        tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$len"
    fi
}

# Quote a string for safe inclusion in a SQL literal. The bootstrap trusts
# the random_password() output (alphanumeric), but we still escape
# single quotes defensively for any caller-supplied values.
sql_quote() {
    local s="${1//\'/\'\'}"
    printf "'%s'" "$s"
}

# Wrapper that respects --dry-run. Connects via docker exec as the
# in-container PG_USER; this works because the hermes-postgres image
# grants trust auth on the unix socket to the bootstrapping role.
pg_exec() {
    if [[ "${DRY_RUN}" == "1" ]]; then
        echo "  [dry-run] docker exec -i ${PG_CONTAINER} psql -U ${PG_USER} $*" >&2
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

# ── 2. Superuser password (random, persisted) ─────────────────────────
STATE_DIR="${HERMES_HOME}/state"
mkdir -p "${STATE_DIR}"
chmod 700 "${STATE_DIR}"
SUPERUSER_PW_FILE="${STATE_DIR}/hermes-postgres.password"

if [[ -f "${SUPERUSER_PW_FILE}" && "${KEEP_PASSWORD}" == "1" ]]; then
    SUPERUSER_PW=$(cat "${SUPERUSER_PW_FILE}")
    log "reusing existing superuser password from ${SUPERUSER_PW_FILE}"
elif [[ -n "${PG_PASSWORD}" ]]; then
    # Caller supplied a password (e.g. restoring from backup). Honor it.
    SUPERUSER_PW="${PG_PASSWORD}"
    if [[ "${DRY_RUN}" == "0" ]]; then
        pg_exec -d postgres -c "ALTER USER ${PG_USER} WITH PASSWORD $(sql_quote "${SUPERUSER_PW}")" >/dev/null
        umask 077
        printf '%s' "${SUPERUSER_PW}" > "${SUPERUSER_PW_FILE}"
        chmod 600 "${SUPERUSER_PW_FILE}"
        log "applied caller-supplied superuser password → ${SUPERUSER_PW_FILE}"
    else
        log "[dry-run] would apply caller-supplied superuser password"
    fi
else
    SUPERUSER_PW="$(random_password)"
    if [[ "${DRY_RUN}" == "0" ]]; then
        # Apply the new password inside the container. This affects TCP
        # auth (md5/scram); unix-socket trust auth is unaffected.
        pg_exec -d postgres -c "ALTER USER ${PG_USER} WITH PASSWORD $(sql_quote "${SUPERUSER_PW}")" >/dev/null
        umask 077
        printf '%s' "${SUPERUSER_PW}" > "${SUPERUSER_PW_FILE}"
        chmod 600 "${SUPERUSER_PW_FILE}"
        log "generated random superuser password (${#SUPERUSER_PW} chars) → ${SUPERUSER_PW_FILE}"
    else
        log "[dry-run] would set random superuser password and write to ${SUPERUSER_PW_FILE}"
    fi
fi

# ── 2b. Persist superuser password into compose/.env ─────────────────
COMPOSE_ENV="${REPO_ROOT}/compose/.env"
if [[ "${DRY_RUN}" == "0" ]]; then
    if [[ -f "${COMPOSE_ENV}" ]]; then
        if grep -q "^HERMES_PG_PASSWORD=" "${COMPOSE_ENV}"; then
            sed -i "s#^HERMES_PG_PASSWORD=.*#HERMES_PG_PASSWORD=${SUPERUSER_PW}#" "${COMPOSE_ENV}"
        else
            printf '\nHERMES_PG_PASSWORD=%s\n' "${SUPERUSER_PW}" >> "${COMPOSE_ENV}"
        fi
    else
        printf 'HERMES_PG_PASSWORD=%s\n' "${SUPERUSER_PW}" > "${COMPOSE_ENV}"
        chmod 600 "${COMPOSE_ENV}"
    fi
    # Also write the host port so docker compose up picks it up
    if grep -q "^HERMES_PG_HOST_PORT=" "${COMPOSE_ENV}"; then
        sed -i "s#^HERMES_PG_HOST_PORT=.*#HERMES_PG_HOST_PORT=${PG_PORT}#" "${COMPOSE_ENV}"
    else
        printf 'HERMES_PG_HOST_PORT=%s\n' "${PG_PORT}" >> "${COMPOSE_ENV}"
    fi
    log "wrote superuser password + host port to ${COMPOSE_ENV}"
fi

# ── 3. Create hermes_template and apply all migrations ─────────────────
TEMPLATE_DB=hermes_template
log "ensuring template database '${TEMPLATE_DB}' exists"
exists=$(pg_exec -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='${TEMPLATE_DB}'" || true)
if [[ "${exists}" != "1" ]]; then
    pg_exec -d postgres -c "CREATE DATABASE ${TEMPLATE_DB}" >/dev/null
fi

# Enable the 6 extensions that don't require server-level config first.
# pg_cron is intentionally skipped here — it requires `cron.database_name`
# in postgresql.conf and is added to a dedicated `hermes_cron` DB by
# migration 0009_observability.sql (which knows how to wire it up).
log "enabling required extensions in ${TEMPLATE_DB}"
for ext in vector postgis timescaledb age pg_trgm ltree; do
    pg_exec -d "${TEMPLATE_DB}" -c "CREATE EXTENSION IF NOT EXISTS ${ext}" >/dev/null \
        || die "failed to enable extension ${ext}"
done
log "  enabled: vector, postgis, timescaledb, age, pg_trgm, ltree"

log "applying schema to ${TEMPLATE_DB} (using migrations bundled in image at /usr/local/share/hermes/01-schemas.sql)"
if [[ "${DRY_RUN}" == "1" ]]; then
    echo "  [dry-run] docker exec -i ${PG_CONTAINER} psql -U ${PG_USER} -d ${TEMPLATE_DB} -v ON_ERROR_STOP=1 -f /usr/local/share/hermes/01-schemas.sql"
else
    docker exec -i "${PG_CONTAINER}" \
        psql -U "${PG_USER}" -d "${TEMPLATE_DB}" -v ON_ERROR_STOP=1 \
        -f /usr/local/share/hermes/01-schemas.sql \
        || die "schema apply failed — see output above"
fi

# Apply the 3 incremental migrations (0007-0009) that aren't bundled in
# 01-schemas.sql because they're newer than the image was built.
for m in 0007_wiki_chunks 0008_sessions 0009_observability; do
    log "applying migration ${m}.sql"
    if [[ "${DRY_RUN}" == "1" ]]; then
        echo "  [dry-run] docker exec -i ${PG_CONTAINER} psql -U ${PG_USER} -d ${TEMPLATE_DB} -v ON_ERROR_STOP=1 -f /usr/migrations/${m}.sql"
    else
        docker exec -i "${PG_CONTAINER}" \
            psql -U "${PG_USER}" -d "${TEMPLATE_DB}" -v ON_ERROR_STOP=1 \
            -f "/usr/migrations/${m}.sql" \
            || die "migration ${m} failed — see output above"
    fi
done

# ── 4. Per-profile role + DB + .env writer ────────────────────────────
create_profile_role_and_db() {
    local profile="$1"
    local role_name="hermes_${profile}"
    local db_name="hermes_${profile}"
    local pw_file="${STATE_DIR}/hermes-pg-${profile}.password"

    # Password: reuse if KEEP_PASSWORD and file exists, else generate.
    local role_pw
    if [[ -f "${pw_file}" && "${KEEP_PASSWORD}" == "1" ]]; then
        role_pw=$(cat "${pw_file}")
        log "  role ${role_name}: reusing existing password"
    else
        role_pw="$(random_password)"
        if [[ "${DRY_RUN}" == "0" ]]; then
            umask 077
            printf '%s' "${role_pw}" > "${pw_file}"
            chmod 600 "${pw_file}"
        fi
    fi

    # Create role (idempotent).
    local role_exists
    role_exists=$(pg_exec -d postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='${role_name}'" || true)
    if [[ "${role_exists}" != "1" ]]; then
        log "  role ${role_name}: creating"
        pg_exec -d postgres -c "CREATE ROLE ${role_name} LOGIN PASSWORD $(sql_quote "${role_pw}") NOSUPERUSER NOCREATEDB NOCREATEROLE" >/dev/null \
            || die "failed to create role ${role_name}"
    else
        log "  role ${role_name}: already exists, updating password"
        pg_exec -d postgres -c "ALTER ROLE ${role_name} WITH LOGIN PASSWORD $(sql_quote "${role_pw}")" >/dev/null
    fi

    # Create or refresh DB. We DROP+CREATE so the DB is a fresh clone
    # of the template (idempotent for our use case — live plugin writes
    # go to hermes_default, not to profile DBs).
    log "  db ${db_name}: dropping + recreating as clone of ${TEMPLATE_DB}"
    # Terminate any active connections to the target DB before drop.
    pg_exec -d postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='${db_name}' AND pid <> pg_backend_pid()" >/dev/null 2>&1 || true
    pg_exec -d postgres -c "DROP DATABASE IF EXISTS ${db_name}" >/dev/null 2>&1
    pg_exec -d postgres -c "CREATE DATABASE ${db_name} TEMPLATE ${TEMPLATE_DB} OWNER ${role_name} CONNECTION LIMIT 20" >/dev/null \
        || die "failed to clone ${TEMPLATE_DB} → ${db_name}"

    # Grant the per-profile role read/write access to the application
    # schemas cloned from the template. Without this, the role owns the
    # DB but can't see the tables/sequences in agent_memory, hermes_*,
    # etc. (those schemas were created by the superuser in the template).
    # We grant ALL on every schema + every existing object, plus set
    # default privileges so future ALTER TABLE inside this DB also
    # defaults to granting the per-profile role.
    log "  ${role_name}: granting schema/object privileges in ${db_name}"
    for schema in agent_memory hermes_journal hermes_kanban hermes_metrics hermes_observability hermes_sessions hermes_skills hermes_wiki; do
        pg_exec -d "${db_name}" -c "GRANT ALL ON SCHEMA ${schema} TO ${role_name}" >/dev/null 2>&1 || true
        pg_exec -d "${db_name}" -c "GRANT ALL ON ALL TABLES IN SCHEMA ${schema} TO ${role_name}" >/dev/null 2>&1 || true
        pg_exec -d "${db_name}" -c "GRANT ALL ON ALL SEQUENCES IN SCHEMA ${schema} TO ${role_name}" >/dev/null 2>&1 || true
        pg_exec -d "${db_name}" -c "GRANT ALL ON ALL FUNCTIONS IN SCHEMA ${schema} TO ${role_name}" >/dev/null 2>&1 || true
    done
    # Default privileges: any future table/sequence/function created in
    # these schemas (by migrations or by the superuser) is auto-granted
    # to the per-profile role.
    pg_exec -d "${db_name}" -c "ALTER DEFAULT PRIVILEGES FOR ROLE ${PG_USER} IN SCHEMA agent_memory GRANT ALL ON TABLES TO ${role_name}" >/dev/null 2>&1 || true
    pg_exec -d "${db_name}" -c "ALTER DEFAULT PRIVILEGES FOR ROLE ${PG_USER} IN SCHEMA agent_memory GRANT ALL ON SEQUENCES TO ${role_name}" >/dev/null 2>&1 || true
    for schema in hermes_journal hermes_kanban hermes_metrics hermes_observability hermes_sessions hermes_skills hermes_wiki; do
        pg_exec -d "${db_name}" -c "ALTER DEFAULT PRIVILEGES FOR ROLE ${PG_USER} IN SCHEMA ${schema} GRANT ALL ON TABLES TO ${role_name}" >/dev/null 2>&1 || true
        pg_exec -d "${db_name}" -c "ALTER DEFAULT PRIVILEGES FOR ROLE ${PG_USER} IN SCHEMA ${schema} GRANT ALL ON SEQUENCES TO ${role_name}" >/dev/null 2>&1 || true
    done
    log "  ${role_name}: granted privileges on 8 schemas + default privileges"

    # Hand back the per-profile DSN info via stdout.
    # Format: db_name|role_pw
    printf '%s|%s\n' "${db_name}" "${role_pw}"
}

# Build the DSN prefix using the **superuser** password for backwards
# compatibility (the Python plugin and C# MCP use the role password in
# the per-profile case, written by write_profile_env).
SUPERUSER_DSN="postgresql://${PG_USER}:${SUPERUSER_PW}@${PG_HOST}:${PG_PORT}"

write_profile_env() {
    local profile="$1"
    local db_name="$2"
    local role_pw="$3"
    local env_file="$4"
    local dsn="${SUPERUSER_DSN}/${db_name}"
    local role_dsn="postgresql://hermes_${profile}:${role_pw}@${PG_HOST}:${PG_PORT}/${db_name}"
    if [[ "${DRY_RUN}" == "1" ]]; then
        echo "  [dry-run] write ${env_file}"
        echo "    PG_MEM_DB_CONN_STR=\"${role_dsn}\""
        echo "    HERMES_PG_CONN_STR=\"${role_dsn}\""
        return
    fi
    mkdir -p "$(dirname "${env_file}")"
    # Rewrite any prior PG_MEM_DB_CONN_STR / HERMES_PG_CONN_STR lines
    local tmp
    tmp="$(mktemp)"
    if [[ -f "${env_file}" ]]; then
        grep -v -E "^(PG_MEM_DB_CONN_STR|HERMES_PG_CONN_STR)=" "${env_file}" > "${tmp}" || true
    else
        : > "${tmp}"
    fi
    {
        printf '\n# --- hermes-memory bootstrap (added by hermes-bootstrap.sh) ---\n'
        printf 'PG_MEM_DB_CONN_STR="%s"\n' "${role_dsn}"
        printf 'HERMES_PG_CONN_STR="%s"\n' "${role_dsn}"
        printf '# --- end hermes-memory bootstrap ---\n'
    } >> "${tmp}"
    cat "${tmp}" > "${env_file}"
    rm -f "${tmp}"
    log "  wrote ${env_file}"
}

PROFILES_DIR="${HERMES_HOME}/profiles"
shopt -s nullglob
profile_dirs=("${PROFILES_DIR}"/*/)
shopt -u nullglob

if [[ ${#profile_dirs[@]} -gt 0 ]]; then
    for prof_dir in "${profile_dirs[@]}"; do
        profile="$(basename "${prof_dir}")"
        [[ "${profile}" == "_templates" ]] && continue
        log "profile: ${profile}"
        result="$(create_profile_role_and_db "${profile}")"
        db_name="${result%%|*}"
        role_pw="${result##*|}"
        env_file="${prof_dir}.env"
        write_profile_env "${profile}" "${db_name}" "${role_pw}" "${env_file}"
    done
else
    log "no profiles dir at ${PROFILES_DIR} (or it's empty) — using single default profile"
    result="$(create_profile_role_and_db default)"
    db_name="${result%%|*}"
    role_pw="${result##*|}"
    env_file="${HERMES_HOME}/.env"
    write_profile_env default "${db_name}" "${role_pw}" "${env_file}"
fi

log "done. restart Hermes Agent to pick up the new DSN:"
log "  hermes gateway restart"
log ""
log "Passwords are stored at ${STATE_DIR}/hermes-pg-*.password (mode 600)."
log "Back up this directory; if it is lost, the roles must be reset via:"
log "  docker exec -it ${PG_CONTAINER} psql -U ${PG_USER} -c 'ALTER ROLE ...'"
