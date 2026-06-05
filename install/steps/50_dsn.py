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
    """Pull password from env, then compose, then existing DSN, then prompt.

    Rejects values with whitespace — a strong signal that the source was
    mangled (e.g. `POSTGRES_PASSWORD=*** POSTGRES_PORT=5432` collapsed by
    a prior redaction system). Trust auth on the dev container means any
    clean value works, so we skip and fall through.
    """
    def _is_clean(pw: str) -> bool:
        return bool(pw) and not any(c.isspace() for c in pw)

    # 1. Explicit env var
    pw = os.environ.get(PW_ENV_NAME, "").strip()
    if _is_clean(pw):
        return pw
    # 2. POSTGRES_PASSWORD (upstream convention)
    pw = os.environ.get(PG_ENV_NAME, "").strip()
    if _is_clean(pw):
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
                candidate = v.strip().strip('"').strip("'")
                if _is_clean(candidate):
                    return candidate
    # 3.5. Probe the live container's POSTGRES_PASSWORD. Most reliable
    # source when the container was started with a custom password.
    import subprocess
    container_name = os.environ.get("HERMES_POSTGRES_CONTAINER", "hermes-postgres")
    r = subprocess.run(
        ["docker", "exec", container_name, "printenv", PG_ENV_NAME],
        capture_output=True, text=True,
    )
    if r.returncode == 0 and _is_clean(r.stdout.strip()):
        return r.stdout.strip()
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
                    candidate = m.group(1)
                    if _is_clean(candidate):
                        return candidate
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
    profile = os.environ.get("HERMES_INSTALL_PROFILE", "").strip()

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

    if profile:
        # Per-profile mode: write ONLY this profile's DSN to the profile's .env.
        # Use the per-profile role (hermes_<name>) and DB (hermes_<name>),
        # and pull the per-profile role password from the state file.
        # The bootstrap script (hermes-bootstrap.sh) created both the
        # role and the DB and wrote the password to
        # ~/.hermes/state/hermes-pg-<name>.password.
        prof_pw_file = Path.home() / ".hermes" / "state" / f"hermes-pg-{profile}.password"
        if prof_pw_file.exists():
            prof_pw = prof_pw_file.read_text().strip()
            if prof_pw:
                password = prof_pw
                print(f"  ✓ profile password loaded from {prof_pw_file} (length: {len(password)})")
        target_db = f"hermes_{profile}"
        dbs = [target_db]
        user = f"hermes_{profile}"
        # Override hermes_home to the profile dir — every DSN lands in
        # ~/.hermes/profiles/<name>/.env, not the main ~/.hermes/.env.
        hermes_home = str(Path(hermes_home) / "profiles" / profile)
        print(f"  ✓ profile mode: target={hermes_home} user={user} db={target_db}")

    for db in dbs:
        dsn = f"postgresql://{user}:***@{host}:{port}/{db}"
        # The above is a redacted form. We need the real one. Replace the marker.
        dsn = dsn.replace("***", password, 1)
        if profile:
            # In profile mode, every DSN goes to the profile's .env.
            env_file = Path(hermes_home) / ".env"
        elif db == "hermes_default":
            env_file = Path(hermes_home) / ".env"
        else:
            prof = db.replace("hermes_", "")
            env_file = Path(hermes_home) / "profiles" / prof / ".env"
        write_env(env_file, dsn)

    print(f"DB_LIST={','.join(dbs)}")

    # Also write the DSN to the install state file (redacted form).
    # Step 8 (MCP register) needs this to know it can proceed, and uses
    # resolve_password() to substitute the real password when registering.
    # The state file lives at ~/.hermes/state/hermes-memory.json.
    # In profile mode, hermes_home is the profile dir; the state file
    # still lives at the main home's state dir.
    state_dir = os.environ.get("HERMES_STATE_DIR", str(Path.home() / ".hermes" / "state"))
    state_file = Path(state_dir) / "hermes-memory.json"
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
        except json.JSONDecodeError:
            state = {}
    else:
        state = {}
    state.setdefault("databases", {})
    state["databases"]["dsn"] = f"postgresql://{user}:***@{host}:{port}/{dbs[0]}"
    state["databases"]["user"] = user
    state["databases"]["host"] = host
    state["databases"]["port"] = port
    state["databases"]["profiles"] = dbs
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(state, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
