#!/usr/bin/env bash
# install/lib/detect.sh — environment detection.
#
# Provides:
#   detect::docker            — prints docker path, dies if missing
#   detect::hermes            — prints hermes CLI path, dies if missing
#   detect::hermes_home       — prints HERMES_HOME
#   detect::repo_root         — prints the hermes-memory repo root
#   detect::arch              — linux-x64 | linux-arm64 | darwin-arm64
#   detect::os                — debian | ubuntu | fedora | alpine | macos | other
#   detect::has_systemd       — 0/1
#   detect::port_free <port>  — 0 if free, 1 if in use
#   detect::internet          — 0 if reachable, 1 if not

[[ -n "${__HERMES_INSTALL_DETECT_LOADED:-}" ]] && return 0
__HERMES_INSTALL_DETECT_LOADED=1

# Where is this file? Use it to find repo root.
_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

detect::docker() {
    if ! command -v docker >/dev/null 2>&1; then
        echo ""
        return 1
    fi
    command -v docker
}

detect::hermes() {
    if ! command -v hermes >/dev/null 2>&1; then
        echo ""
        return 1
    fi
    command -v hermes
}

detect::hermes_home() {
    local h="${HERMES_HOME:-$HOME/.hermes}"
    # Strip /hermes-agent if it's there
    if [[ "$h" == */hermes-agent ]]; then
        h="${h%/hermes-agent}"
    fi
    printf "%s" "$h"
}

detect::repo_root() {
    # The repo root is two levels up from install/lib/
    local dir
    dir="$(cd "${_LIB_DIR}/../.." && pwd)"
    printf "%s" "$dir"
}

detect::arch() {
    local m="$(uname -m)"
    case "$m" in
        x86_64|amd64)   echo "linux-x64" ;;
        aarch64|arm64)  echo "linux-arm64" ;;
        *)              echo "unknown-$m" ;;
    esac
}

detect::os() {
    if [[ "$(uname -s)" == "Darwin" ]]; then
        echo "macos"
        return
    fi
    if [[ -f /etc/os-release ]]; then
        local id
        id="$(grep '^ID=' /etc/os-release | cut -d= -f2 | tr -d '"')"
        case "$id" in
            debian|ubuntu) echo "$id" ;;
            fedora|centos|rhel|rocky) echo "rhel" ;;
            alpine) echo "alpine" ;;
            *)       echo "$id" ;;
        esac
        return
    fi
    echo "unknown"
}

detect::has_systemd() {
    if [[ "$(uname -s)" == "Linux" ]] && command -v systemctl >/dev/null 2>&1; then
        return 0
    fi
    return 1
}

detect::port_free() {
    local port="$1"
    # Use python for portability (no nc / ss dependency)
    python3 - "$port" <<'PY'
import socket, sys
port = int(sys.argv[1])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("0.0.0.0", port))
    print("free")
except OSError:
    print("busy")
finally:
    s.close()
PY
}

detect::internet() {
    # Try a HEAD on a known-good host. 5s timeout.
    if command -v curl >/dev/null 2>&1; then
        if curl -fsS --max-time 5 -I https://github.com/ >/dev/null 2>&1; then
            return 0
        fi
    fi
    if command -v wget >/dev/null 2>&1; then
        if wget -q --timeout=5 --spider https://github.com/ 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}
