#!/usr/bin/env bash
# install/lib/dsn.sh — build PG_MEM_DB_CONN_STR for the local DB.

[[ -n "${__HERMES_INSTALL_DSN_LOADED:-}" ]] && return 0
__HERMES_INSTALL_DSN_LOADED=1

# dsn::build <user> <password> <host> <port> <dbname>
dsn::build() {
    printf "postgresql://%s:%s@%s:%s/%s" "$1" "$2" "$3" "$4" "$5"
}

# dsn::redact <dsn>  — returns "postgresql://user:***@host:port/db"
dsn::redact() {
    printf "%s" "$1" | sed -E 's#://[^:@]+:[^@]+@#://\1:***@#'
}
