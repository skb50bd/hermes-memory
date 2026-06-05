#!/usr/bin/env bash
# install/lib/embedder.sh — pick + write the embedder provider config.

[[ -n "${__HERMES_INSTALL_EMBEDDER_LOADED:-}" ]] && return 0
__HERMES_INSTALL_EMBEDDER_LOADED=1

# embedder::providers — prints a list of "name|description" lines
embedder::providers() {
    cat <<'EOF'
ollama_local|self-hosted Ollama (no API key, free; recommended default)
kimi|Moonshot/Kimi cloud (free with KIMI_API_KEY; default 1024)
openai|cloud OpenAI text-embedding-3-small (paid, OPENAI_API_KEY)
noop|zero-vector fallback (search degrades to FTS-only)
EOF
}

# embedder::default_model <provider> <dim>
embedder::default_model() {
    local provider="$1" dim="$2"
    case "$provider" in
        ollama_local)
            case "$dim" in
                768)  echo "nomic-embed-text-v2-moe" ;;
                1024) echo "bge-m3" ;;
                1536) echo "" ;;  # ollama has no 1536-dim
                *)    echo "" ;;
            esac ;;
        kimi)
            case "$dim" in
                768)  echo "nomic-embed-text" ;;  # via Kimi
                1024) echo "bge_m3_embed" ;;
                1536) echo "embo-01" ;;  # via kimi or similar
                *)    echo "" ;;
            esac ;;
        openai)
            case "$dim" in
                768)  echo "text-embedding-3-small" ;;  # truncated
                1024) echo "text-embedding-3-small" ;;  # truncated
                1536) echo "text-embedding-3-small" ;;
                *)    echo "text-embedding-3-small" ;;
            esac ;;
        noop) echo "noop" ;;
        *)    echo "" ;;
    esac
}

# embedder::needs_api_key <provider>
embedder::needs_api_key() {
    case "$1" in
        ollama_local) return 1 ;;  # local, no key
        *)            return 0 ;;
    esac
}

# embedder::api_key_env <provider>
embedder::api_key_env() {
    case "$1" in
        kimi)   echo "KIMI_API_KEY" ;;
        openai) echo "OPENAI_API_KEY" ;;
        *)      echo "" ;;
    esac
}

# embedder::default_base_url <provider>
# Ollama local: HERMES_OLLAMA_HOST_PORT env var, default 16434 (11434+5000).
# Falls back to legacy 11434 if HERMES_OLLAMA_USE_LEGACY_PORT=1.
embedder::default_base_url() {
    case "$1" in
        ollama_local)
            local port="${HERMES_OLLAMA_HOST_PORT:-}"
            if [[ -z "$port" && "${HERMES_OLLAMA_USE_LEGACY_PORT:-0}" == "1" ]]; then
                port="11434"
            elif [[ -z "$port" ]]; then
                port="16434"
            fi
            echo "http://127.0.0.1:${port}"
            ;;
        kimi)         echo "https://api.kimi.com/coding/v1" ;;
        openai)       echo "https://api.openai.com/v1" ;;
        *)            echo "" ;;
    esac
}

# embedder::pick — interactive picker, returns the provider name
# Reads from stdin, writes the choice to stdout.
embedder::pick() {
    local current="${1:-}"
    local opts=()
    while IFS='|' read -r name desc; do
        opts+=("$name")
    done < <(embedder::providers)

    # Build a human-friendly menu
    ui::banner "Embedder provider"
    ui::info "The memory + wiki + journal + skills tools all need an embedder to convert text → vectors."
    ui::info "Pick the one you'll use. (You can re-run with --change-embedder to switch later.)"
    ui::info "Current setting: ${current:-none}"
    ui::rule ""

    local i=1
    while IFS='|' read -r name desc; do
        printf "    ${CYAN}%d)${RESET} ${BOLD}%-15s${RESET}  ${DIM}%s${RESET}\n" "$i" "$name" "$desc"
        ((i++))
    done < <(embedder::providers)
    echo

    local choice
    choice="$(ui::prompt "Pick a provider" "${current:-ollama_local}")"
    printf "%s" "$choice"
}

# embedder::configure_per_dim <provider> <base_url> <api_key_env>
# Writes HERMES_EMBED_PROVIDER_*, HERMES_EMBED_BASE_URL_*, HERMES_EMBED_MODEL_*,
# and (if api_key_env is set) HERMES_EMBED_API_KEY_<dim> into ~/.hermes/.env
# for all three dims that the provider supports.
embedder::configure_per_dim() {
    local provider="$1" base_url="$2" api_key_env="$3"
    local env_file="${HERMES_HOME:-$HOME/.hermes}/.env"

    for dim in 768 1024 1536; do
        local model
        model="$(embedder::default_model "$provider" "$dim")"
        if [[ -z "$model" ]]; then
            ui::dim "  skip dim=$dim (provider $provider doesn't have a $dim-dim model)"
            continue
        fi

        # Replace or append each line
        for var in "HERMES_EMBED_PROVIDER_$dim" \
                   "HERMES_EMBED_BASE_URL_$dim" \
                   "HERMES_EMBED_MODEL_$dim"; do
            local val=""
            case "$var" in
                *_PROVIDER_*) val="$provider" ;;
                *_BASE_URL_*) val="$base_url" ;;
                *_MODEL_*)    val="$model" ;;
            esac
            __env_write_or_replace "$env_file" "$var" "$val"
        done

        # API key: write the env-var name (not the value). Hermes plugins
        # read the value from the env-var named here.
        if [[ -n "$api_key_env" ]]; then
            __env_write_or_replace "$env_file" "HERMES_EMBED_API_KEY_ENV_$dim" "$api_key_env"
        fi
    done
}

# Internal: idempotent write of one line to a .env file
__env_write_or_replace() {
    local file="$1" key="$2" value="$3"
    if [[ -f "$file" ]] && grep -qE "^${key}=" "$file"; then
        # Use sed for in-place update. Escape the value for sed.
        local esc="${value//\//\\/}"
        esc="${esc//&/\\&}"
        sed -i.bak -E "s#^${key}=.*#${key}=${value}#" "$file"
        rm -f "${file}.bak"
    else
        printf "\n%s=%s\n" "$key" "$value" >> "$file"
    fi
}
