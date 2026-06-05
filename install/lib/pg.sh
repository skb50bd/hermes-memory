#!/usr/bin/env bash
# install/lib/pg.sh — Postgres connection helpers.
#
# All commands run inside the hermes-postgres container via `docker exec`
# (not via psql over the network). This avoids needing the host's psql
# to match the server version, and avoids TCP/TLS pitfalls.

[[ -n "${__HERMES_INSTALL_PG_LOADED:-}" ]] && return 0
__HERMES_INSTALL_PG_LOADED=1

_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Defaults — can be overridden by step scripts.
PG_CONTAINER="${HERMES_PG_CONTAINER:-hermes-postgres}"
PG_USER="${HERMES_PG_USER:-hermes}"
PG_PASSWORD="${HERMES_PG_PASSWORD:-}"
PG_HOST="${HERMES_PG_HOST:-127.0.0.1}"
PG_PORT="${HERMES_PG_PORT:-5444}"
PG_TEMPLATE_DB="${HERMES_TEMPLATE_DB:-hermes_template}"
PG_CRON_DB="${HERMES_CRON_DB:-hermes_cron}"

# pg::exec [-d db] -c <sql>
# Run SQL inside the container. If -d is omitted, uses postgres.
pg::exec() {
    local db="postgres"; local sql=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -d) db="$2"; shift 2 ;;
            -c) sql="$2"; shift 2 ;;
            *)  shift ;;
        esac
    done
    if [[ "${HERMES_DRY_RUN:-0}" == "1" ]]; then
        printf "  [dry-run] docker exec -i %s psql -U %s -d %s -c %s\n" \
            "$PG_CONTAINER" "$PG_USER" "$db" "$sql" >&2
        return 0
    fi
    docker exec -i "$PG_CONTAINER" \
        env PGPASSWORD="$PG_PASSWORD" \
        psql -U "$PG_USER" -d "$db" -v ON_ERROR_STOP=1 -c "$sql"
}

# pg::exec_file [-d db] <sql-file>
pg::exec_file() {
    local db="postgres"; local file=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -d) db="$2"; shift 2 ;;
            *)  file="$1"; shift ;;
        esac
    done
    if [[ "${HERMES_DRY_RUN:-0}" == "1" ]]; then
        printf "  [dry-run] docker exec -i %s psql -U %s -d %s -f %s\n" \
            "$PG_CONTAINER" "$PG_USER" "$db" "$file" >&2
        return 0
    fi
    docker exec -i "$PG_CONTAINER" \
        env PGPASSWORD="$PG_PASSWORD" \
        psql -U "$PG_USER" -d "$db" -v ON_ERROR_STOP=1 -f "$file"
}

# pg::isready — exit 0 if the container is accepting connections
pg::isready() {
    docker exec -i "$PG_CONTAINER" pg_isready -U "$PG_USER" -d postgres >/dev/null 2>&1
}

# pg::wait_until_ready [timeout_seconds]
# Polls pg_isready every second up to timeout (default 60).
pg::wait_until_ready() {
    local timeout="${1:-60}"
    local elapsed=0
    while ! pg::isready; do
        sleep 1
        ((elapsed++)) || true
        if (( elapsed >= timeout )); then
            return 1
        fi
    done
    return 0
}

# pg::database_exists <dbname>
pg::database_exists() {
    local db="$1"
    local out
    out="$(pg::exec -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$db'" 2>/dev/null)" || return 1
    [[ "$out" == "1" ]]
}

# pg::create_database <dbname> [template]
pg::create_database() {
    local db="$1"; local tmpl="${2:-}"
    local sql="CREATE DATABASE \"$db\""
    [[ -n "$tmpl" ]] && sql="$sql TEMPLATE \"$tmpl\""
    pg::exec -d postgres -c "$sql" >/dev/null
}

# pg::drop_database <dbname> (with --if-exists)
pg::drop_database() {
    local db="$1"
    pg::exec -d postgres -c "DROP DATABASE IF EXISTS \"$db\"" >/dev/null 2>&1 || true
}

# pg::extension_enabled <extname> [dbname]
pg::extension_enabled() {
    local ext="$1"; local db="${2:-$PG_TEMPLATE_DB}"
    local out
    out="$(pg::exec -d "$db" -tAc "SELECT 1 FROM pg_extension WHERE extname='$ext'" 2>/dev/null)" || return 1
    [[ "$out" == "1" ]]
}

# pg::list_databases
pg::list_databases() {
    pg::exec -d postgres -tAc "SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname" 2>/dev/null
}

# pg::extension_versions [dbname] — prints "name version" pairs
pg::extension_versions() {
    local db="${1:-$PG_TEMPLATE_DB}"
    pg::exec -d "$db" -tAc "SELECT extname || ' ' || extversion FROM pg_extension ORDER BY extname" 2>/dev/null
}

# pg::migration_count — count of .sql files in the repo's migrations/ dir
pg::migration_count() {
    local repo; repo="$("${_LIB_DIR}/detect.sh" 2>/dev/null; detect::repo_root)" 2>/dev/null
    # The above is wrong; just use the var directly
    repo="${HERMES_MEMORY_REPO:-$(cd "${_LIB_DIR}/../.." && pwd)}"
    find "$repo/migrations" -maxdepth 1 -name '*.sql' -type f | wc -l
}

# pg::migrations_applied [dbname] — count rows in schema_migrations or equivalent
pg::migrations_applied() {
    local db="${1:-$PG_TEMPLATE_DB}"
    # We don't have a formal migration table. Use the number of our
    # hermes_* schemas as a proxy: 5 schemas (agent_memory, hermes_wiki,
    # hermes_journal, hermes_skills, hermes_metrics) means all migrations ran.
    local out
    out="$(pg::exec -d "$db" -tAc "
        SELECT count(*) FROM information_schema.schemata
        WHERE schema_name IN ('agent_memory','hermes_wiki','hermes_journal','hermes_skills','hermes_metrics')
    " 2>/dev/null)" || out="0"
    echo "$out"
}
