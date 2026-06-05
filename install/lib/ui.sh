#!/usr/bin/env bash
# install/lib/ui.sh — colored output, banners, prompts, step headers.
#
# Source this from step scripts:
#   source "$(dirname "${BASH_SOURCE[0]}")/lib/ui.sh"
#
# Provides:
#   ui::banner "title"
#   ui::step "N/M" "Title"
#   ui::ok    "message"
#   ui::warn  "message"
#   ui::fail  "message"
#   ui::info  "message"
#   ui::dim   "message"
#   ui::rule  "─────────"
#   ui::prompt "Question" "default"
#   ui::confirm "Question" 0|1
#   ui::password "Question"
#   ui::select "Question" "opt1" "opt2" ...
#   ui::spinner_start "label" PID
#   ui::spinner_stop
#   ui::dry_run_echo "would do X"     (respects HERMES_DRY_RUN)
#
# Color/TTY detection happens once; the rest of the file is plain text.

# Guard against double-sourcing
[[ -n "${__HERMES_INSTALL_UI_LOADED:-}" ]] && return 0
__HERMES_INSTALL_UI_LOADED=1

# ── Color detection ──────────────────────────────────────────────────────
if [[ -t 1 ]] && command -v tput >/dev/null 2>&1 && [[ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]]; then
    BOLD="\033[1m"; DIM="\033[2m"; RESET="\033[0m"
    RED="\033[31m"; GREEN="\033[32m"; YELLOW="\033[33m"
    BLUE="\033[34m"; MAGENTA="\033[35m"; CYAN="\033[36m"
else
    BOLD=""; DIM=""; RESET=""
    RED=""; GREEN=""; YELLOW=""; BLUE=""; MAGENTA=""; CYAN=""
fi

# ── Output helpers ───────────────────────────────────────────────────────
ui::banner() {
    printf "\n${BOLD}${CYAN}═══ %s ═══${RESET}\n" "$*"
}

ui::step() {
    local n="$1"; shift
    printf "\n${BOLD}${BLUE}Step %s${RESET}  ${BOLD}%s${RESET}\n" "$n" "$*"
}

ui::ok()    { printf "  ${GREEN}✓${RESET} %s\n" "$*"; }
ui::warn()  { printf "  ${YELLOW}!${RESET} %s\n" "$*"; }
ui::fail()  { printf "  ${RED}✗${RESET} %s\n" "$*" >&2; }
ui::info()  { printf "  %s\n" "$*"; }
ui::dim()   { printf "  ${DIM}%s${RESET}\n" "$*"; }
ui::rule()  { printf "  ${DIM}%s${RESET}\n" "$*"; }

ui::before_after() {
    printf "  ${DIM}BEFORE:${RESET} %s\n" "$1"
    printf "  ${GREEN}AFTER:${RESET}  %s\n" "$2"
}

# ── Prompts ──────────────────────────────────────────────────────────────
# In non-interactive mode, return the default without waiting.
__interactive=1
[[ "${HERMES_INSTALL_NON_INTERACTIVE:-0}" == "1" || ! -t 0 ]] && __interactive=0

ui::prompt() {
    local q="$1"; local def="${2:-}"
    if [[ "${__interactive}" == "0" ]]; then
        printf "  ${DIM}%s [default: %s]${RESET}\n" "$q" "$def" >&2
        printf "%s" "$def"
        return
    fi
    local suffix=""
    [[ -n "$def" ]] && suffix=" [${def}]"
    local ans
    read -r -p "$(printf "  ${BOLD}%s${RESET}${DIM}%s:${RESET} " "$q" "$suffix")" ans
    [[ -z "$ans" ]] && ans="$def"
    printf "%s" "$ans"
}

ui::confirm() {
    local q="$1"; local def="${2:-0}"  # 0=no (default), 1=yes
    if [[ "${__interactive}" == "0" ]]; then
        [[ "$def" == "1" ]] && return 0 || return 1
    fi
    local yn="y/N"
    [[ "$def" == "1" ]] && yn="Y/n"
    local ans
    read -r -p "$(printf "  ${BOLD}%s${RESET} [${yn}]: " "$q")" ans
    case "${ans,,}" in
        y|yes)  return 0 ;;
        n|no|'') [[ "$def" == "1" ]] && return 0 || return 1 ;;
        *)      return 1 ;;
    esac
}

ui::password() {
    local q="$1"
    if [[ "${__interactive}" == "0" ]]; then
        printf "  ${DIM}%s (non-interactive, leaving blank)${RESET}\n" "$q" >&2
        printf ""
        return
    fi
    local ans
    read -r -s -p "$(printf "  ${BOLD}%s${RESET}: " "$q")" ans
    printf "\n" >&2
    printf "%s" "$ans"
}

ui::select() {
    local q="$1"; shift
    local opts=("$@")
    if [[ "${__interactive}" == "0" ]]; then
        printf "  ${DIM}%s [default: 1]${RESET}\n" "$q" >&2
        printf "%s" "${opts[0]}"
        return
    fi
    echo "  ${BOLD}${q}${RESET}" >&2
    local i=1
    for opt in "${opts[@]}"; do
        printf "    ${CYAN}%d)${RESET} %s\n" "$i" "$opt" >&2
        ((i++))
    done
    local ans
    read -r -p "$(printf "  Pick [1]: ")" ans
    [[ -z "$ans" ]] && ans=1
    if ! [[ "$ans" =~ ^[0-9]+$ ]] || (( ans < 1 )) || (( ans > ${#opts[@]} )); then
        printf "  ${YELLOW}!${RESET} invalid selection, defaulting to 1\n" >&2
        ans=1
    fi
    printf "%s" "${opts[$((ans-1))]}"
}

# ── Spinner ──────────────────────────────────────────────────────────────
__spinner_pid=""
ui::spinner_start() {
    local label="$1"; local pid="$2"
    if [[ ! -t 1 ]]; then
        # non-tty: just print the label
        printf "  %s ...\n" "$label" >&2
        return
    fi
    __spinner_pid="$pid"
    (
        local spin='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'
        local i=0
        while kill -0 "$pid" 2>/dev/null; do
            printf "\r  ${CYAN}%s${RESET} %s" "${spin:i++%${#spin}:1}" "$label" >&2
            sleep 0.1
        done
        printf "\r  ${GREEN}✓${RESET} %s\n" "$label" >&2
    ) &
    ui::spinner_stop
}

ui::spinner_stop() {
    # no-op; spinner is foregrounded via & and exits when pid dies
    :
}

ui::spinner_kill() {
    # used when caller wants to stop the spinner early on completion
    if [[ -n "${__spinner_pid}" ]]; then
        wait "${__spinner_pid}" 2>/dev/null || true
        __spinner_pid=""
    fi
}

# ── Dry-run echo ─────────────────────────────────────────────────────────
ui::dry_echo() {
    if [[ "${HERMES_DRY_RUN:-0}" == "1" ]]; then
        printf "  ${MAGENTA}[dry-run]${RESET} %s\n" "$*"
    fi
}

# ── Step-result reporting ────────────────────────────────────────────────
ui::step_skipped() {
    printf "  ${DIM}— skipped: %s${RESET}\n" "$*"
}

ui::step_done() {
    printf "  ${GREEN}done${RESET}"
    [[ -n "${1:-}" ]] && printf " ${DIM}(%s)${RESET}" "$1"
    printf "\n"
}
