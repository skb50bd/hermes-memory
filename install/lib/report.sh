#!/usr/bin/env bash
# install/lib/report.sh — print the post-install summary.

[[ -n "${__HERMES_INSTALL_REPORT_LOADED:-}" ]] && return 0
__HERMES_INSTALL_REPORT_LOADED=1

# report::summary
# Prints a multi-line summary of what was installed.
report::summary() {
    local repo; repo="$(_lib detect::repo_root)"
    # Pull what we need from the state file
    local image name dsn embedder mcp_bin mcp_registered
    image="$(state::get container.image)"
    name="$(state::get container.name)"
    dsn="$(state::get databases.dsn)"
    embedder_provider="$(state::get embedder.provider)"
    embedder_model="$(state::get embedder.model)"
    mcp_bin="$(state::get mcp.binary_path)"
    mcp_registered="$(state::get mcp.registered)"

    cat <<EOF

${BOLD}${CYAN}═══════════════════════════════════════════════════════════════${RESET}
${BOLD}  hermes-memory is installed.${RESET}

${BOLD}  Database${RESET}
    Container:  ${name:-hermes-postgres}  (image: ${image:-unknown})
    DSN:        ${dsn:-unknown}

${BOLD}  Embedder${RESET}
    Provider:   ${embedder_provider:-not set}
    Model:      ${embedder_model:-not set}

${BOLD}  Python plugin (in-process, live now)${RESET}
    pg_remember, pg_search, pg_recent, pg_forget, pg_status, pg_model_set
    These are available to the agent on next session start.

${BOLD}  MCP server (C# binary, stdio)${RESET}
    Binary:     ${mcp_bin:-not set}
    Registered: ${mcp_registered:-no}
    ${DIM}37 tools across 6 surfaces (memory, wiki, journal, kanban, metrics, skills).${RESET}
    ${DIM}These become available after \`hermes gateway restart\`.${RESET}

${BOLD}  Management commands${RESET}
    ${CYAN}hermes postgres status${RESET}                       # provider health
    ${CYAN}hermes postgres backfill --dim N${RESET}             # populate missing vectors
    ${CYAN}hermes postgres find-empty${RESET}                   # list rows without embeddings
    ${CYAN}./install.sh --check${RESET}                          # verify the install is healthy
    ${CYAN}./install.sh --update${RESET}                         # idempotent refresh
    ${CYAN}./install.sh --uninstall${RESET}                      # reverse
    ${CYAN}docker logs hermes-postgres${RESET}                   # container logs

${BOLD}${CYAN}═══════════════════════════════════════════════════════════════${RESET}

${BOLD}Next step${RESET}: restart the gateway to load the new MCP server.
    ${CYAN}hermes gateway restart${RESET}

EOF
}

# report::check — print a brief health report
report::check() {
    local health="OK"
    ui::banner "hermes-memory health check"
    # Container
    if compose::is_up; then
        ui::ok "Container $(state::get container.name) is running"
    else
        ui::fail "Container $(state::get container.name) is NOT running"
        health="DEGRADED"
    fi
    # Postgres ready
    if pg::isready; then
        ui::ok "Postgres is ready"
    else
        ui::fail "Postgres is not ready"
        health="DEGRADED"
    fi
    # Extensions
    local exts; exts="$(pg::extension_versions "${HERMES_TEMPLATE_DB:-hermes_template}")"
    local ext_count
    ext_count="$(echo "$exts" | grep -c .)"
    if (( ext_count >= 6 )); then
        ui::ok "$ext_count extensions enabled"
    else
        ui::warn "Only $ext_count extensions enabled (expected ≥6)"
        health="DEGRADED"
    fi
    # Migrations
    local applied; applied="$(pg::migrations_applied "${HERMES_TEMPLATE_DB:-hermes_template}")"
    if (( applied >= 5 )); then
        ui::ok "5 hermes_* schemas present (migrations applied)"
    else
        ui::fail "Only $applied/5 hermes_* schemas present (migrations incomplete)"
        health="DEGRADED"
    fi
    # MCP server
    if mcp::is_registered; then
        ui::ok "MCP server '${HERMES_MEMORY_MCP_NAME:-hermes-memory}' is registered"
    else
        ui::warn "MCP server '${HERMES_MEMORY_MCP_NAME:-hermes-memory}' is NOT registered"
        health="DEGRADED"
    fi
    # Embedder
    local prov; prov="$(state::get embedder.provider)"
    if [[ -n "$prov" ]]; then
        ui::ok "Embedder provider: $prov"
    else
        ui::warn "Embedder provider not configured"
        health="DEGRADED"
    fi
    echo
    if [[ "$health" == "OK" ]]; then
        printf "${GREEN}${BOLD}Overall: HEALTHY${RESET}\n"
    else
        printf "${YELLOW}${BOLD}Overall: DEGRADED${RESET}  (see warnings above)\n"
    fi
}
