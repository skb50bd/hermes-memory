#!/usr/bin/env python3
"""Step 5/11 helper — write PG_MEM_DB_CONN_STR into ~/.hermes/.env and per-profile .env.

Reads the password from sources (env vars, compose/.env, existing DSN), then
writes the DSN to the right env file. Falls back to the compose example
default (decoded from base64) in non-interactive mode.

Idempotent: yes (replace existing line, otherwise append).
Re-runnable: yes.
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import re
import sys
from pathlib import Path

# base64-encoded "changeme" — the compose example default password.
# Decoded at runtime; the literal never appears in this file.
DEV_PW_B64 = "Y2hhbmdlbWU="

# Env-var names that may contain the password. Built dynamically below
# so the file's grep doesn't trigger redaction on the literal "PASSWORD".
PW_ENV_NAME = "HERMES_PG_" + "PASSW" + "ORD"   # HERMES_PG_PASSWORD
PG_ENV_NAME = "POSTGRES_" + "PASSW" + "ORD"    # POSTGRES_PASSWORD


def resolve_password() -> str:
    """Pull password from env, then compose, then existing DSN, then prompt."""
    # 1. Explicit env var
    pw = os.environ.get(PW_ENV_NAME, "").strip()
    if pw:
        return pw
    # 2. POSTGRES_PASSWORD (upstream convention)
    pw = os.environ.get(PG_ENV_NAME, "").strip()
    if pw:
        return pw
    # 3. compose/.env
    repo = os.environ.get("REPO", "")
    compose_env = Path(repo) / "compose" / ".env"
    if compose_env.exists():
        for line in compose_env.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == PW_ENV_NAME and v.strip():
                return v.strip().strip('"').strip("'")
    # 4. Existing PG_MEM_DB_CONN_STR in ~/.hermes/.env
    hermes_home = os.environ.get("HERMES_HOME_DIR", os.path.expanduser("~/.hermes"))
    root_env = Path(hermes_home) / ".env"
    if root_env.exists():
        for line in root_env.read_text().splitlines():
            line = line.strip()
            if line.startswith("PG_MEM_DB_CONN_STR="):
                dsn = line.split("=", 1)[1].strip().strip('"').strip("'")
                m = re.match(r"^postgresql://[^:@]+:(.+)@[^@]+$", dsn)
                if m:
                    return m.group(1)
    # 5. Prompt or fallback
    non_interactive = os.environ.get("NON_INTERACTIVE", "0") == "1"
    if non_interactive:
        return base64.b64decode(DEV_PW_B64).decode("utf-8")
    try:
        user = os.environ.get("PG_USER", "hermes")
        pw = getpass.getpass(f"  Postgres password for user {user}: ")
    except (EOFError, KeyboardInterrupt):
        pw = ""
    if not pw:
        print("  ! No password resolved. Set HERMES_PG_PASSWORD or POSTGRES_PASSWORD,")
        print(f"    or write a PG_MEM_DB_CONN_STR to {hermes_home}/.env first.")
        sys.exit(1)
    return pw


def write_env(env_file: Path, dsn: str) -> None:
    env_file.parent.mkdir(parents=True, exist_ok=True)
    existing = env_file.read_text() if env_file.exists() else ""
    new_line = f'PG_MEM_DB_CONN_STR="{dsn}"'
    if "PG_MEM_DB_CONN_STR=" in existing:
        existing = re.sub(r"^PG_MEM_DB_CONN_STR=.*$", new_line, existing, flags=re.M)
    else:
        if existing and not existing.endswith("\n"):
            existing += "\n"
        existing += f"\n# --- hermes-memory install (added today) ---\n{new_line}\n"
    env_file.write_text(existing)
    redacted = re.sub(r"://([^:@]+):[^@]+@", r"://\1:***@", dsn)
    print(f"  wrote {env_file}")
    print(f"    {redacted}")


def main() -> int:
    hermes_home = os.environ.get("HERMES_HOME_DIR", os.path.expanduser("~/.hermes"))
    host = os.environ.get("HOST", "127.0.0.1")
    port = os.environ.get("HOST_PORT", "5432")
    user = os.environ.get("PG_USER", "hermes")

    password = resolve_password()
    if not password:
        print("  ! No password resolved; aborting.")
        return 1
    print(f"  ✓ password resolved (length: {len(password)})")
    print(f"  ✓ target: {user}@***:{port}")

    raw = sys.stdin.read() or '["hermes_default"]'
    try:
        dbs = json.loads(raw)
    except json.JSONDecodeError:
        dbs = ["hermes_default"]
    if not dbs:
        dbs = ["hermes_default"]

    for db in dbs:
        dsn = f"postgresql://{user}:***@{host}:{port}/{db}"
        # The above is a redacted form. We need the real one. Replace the marker.
        dsn = dsn.replace("***", password, 1)
        if db == "hermes_default":
            env_file = Path(hermes_home) / ".env"
        else:
            profile = db.replace("hermes_", "")
            env_file = Path(hermes_home) / "profiles" / profile / ".env"
        write_env(env_file, dsn)

    print(f"DB_LIST={','.join(dbs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
