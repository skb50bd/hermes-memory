#!/usr/bin/env bash
# Step 5/11 — Wire PG_MEM_DB_CONN_STR into ~/.hermes/.env (and per-profile .env).
#
# This step is a thin bash shim around a Python helper, because the
# pipeline-level redaction mangles bash quoted strings that contain
# env-var names like HERMES_PG_PASSWORD. The Python code reads the
# password from sources at runtime and never puts a literal password
# in the file.
#
# Idempotent: yes. Re-runnable: yes.

set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load UI helpers only. Anything that touches env-var names happens in Python.
LIB="${HERE}/../lib"
source "${LIB}/ui.sh"
source "${LIB}/detect.sh"
source "${LIB}/state.sh"

ui::step "5/11" "Wire DSN into .env files"

HERMES_HOME_DIR="$(detect::hermes_home)"
REPO="$(detect::repo_root)"
HOST_PORT="${HERMES_PG_HOST_PORT:-5432}"
HOST="127.0.0.1"
PG_USER="${HERMES_PG_USER:-hermes}"
PROMPT="${HERMES_INSTALL_NON_INTERACTIVE:-0}"

# Dispatch to Python. The python reads the password from sources at runtime.
HERMES_HOME_DIR="$HERMES_HOME_DIR" \
    REPO="$REPO" \
    HOST="$HOST" \
    HOST_PORT="$HOST_PORT" \
    PG_USER="$PG_USER" \
    NON_INTERACTIVE="$PROMPT" \
    python3 "${HERE}/50_dsn.py"
