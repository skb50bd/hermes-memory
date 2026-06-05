#!/usr/bin/env bash
# install/lib/mcp.sh — register the C# hermes-memory binary as an MCP server.
#
# Wraps `hermes mcp add/remove/list`. The hermes-agent CLI handles all the
# YAML merge / dedup / ${ENV} resolution / config persistence; we just
# shell out to it.

[[ -n "${__HERMES_INSTALL_MCP_LOADED:-}" ]] && return 0
__HERMES_INSTALL_MCP_LOADED=1

MCP_SERVER_NAME="${HERMES_MEMORY_MCP_NAME:-hermes-memory}"

# mcp::is_registered — returns 0 if the server name is in mcp_servers
mcp::is_registered() {
    hermes mcp list 2>/dev/null | grep -qE "^[[:space:]]*${MCP_SERVER_NAME}[[:space:]]"
}

# mcp::add <binary_path> <dsn> [extra_env...]
# Registers the MCP server via `hermes mcp add`.
mcp::add() {
    local bin="$1" dsn="$2"; shift 2
    local env_args=(
        "HERMES_PG_CONN_STR=$dsn"
        "HERMES_EMBED_FAIL_OPEN=1"
    )
    for kv in "$@"; do
        env_args+=("$kv")
    done

    if [[ "${HERMES_DRY_RUN:-0}" == "1" ]]; then
        echo "  [dry-run] hermes mcp add $MCP_SERVER_NAME --command $bin --args --mcp --env ${env_args[*]}" >&2
        return 0
    fi

    hermes mcp add "$MCP_SERVER_NAME" \
        --command "$bin" \
        --args --mcp \
        --env "${env_args[@]}"
}

# mcp::remove — unregister (for uninstall)
mcp::remove() {
    if [[ "${HERMES_DRY_RUN:-0}" == "1" ]]; then
        echo "  [dry-run] hermes mcp remove $MCP_SERVER_NAME" >&2
        return 0
    fi
    hermes mcp remove "$MCP_SERVER_NAME" 2>/dev/null || true
}

# mcp::test — run `hermes mcp test` to verify the server is reachable
mcp::test() {
    if [[ "${HERMES_DRY_RUN:-0}" == "1" ]]; then
        echo "  [dry-run] hermes mcp test $MCP_SERVER_NAME" >&2
        return 0
    fi
    hermes mcp test "$MCP_SERVER_NAME" 2>&1 | head -20
}
