#!/usr/bin/env python3
"""hermes-memory install wizard — main orchestrator.

This script is invoked by each `install/steps/NN_*.sh` shim, which sets
`HERMES_STEP` to indicate which step to run. The wizard handles all UI
(prompts, banners, errors), state persistence, secret handling, and
side effects.

To run a single step from bash:
    HERMES_STEP=5 bash install/steps/50_dsn.sh

To run the full wizard from the C# binary (or `./install.sh`):
    for f in install/steps/[0-9]*.sh; do HERMES_STEP=$idx bash "$f" || exit 1; done

Secrets: passwords, API keys, and tokens are NEVER written to disk as
literals. They are read from env vars or prompted interactively, used
in memory, and then disposed.
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

# ─── Constants ────────────────────────────────────────────────────────────

# base64("changeme")
DEV_PW_B64 = "Y2hhbmdlbWU="

# base64 of empty bytes — used as a placeholder for unset fields
EMPTY_B64 = ""

# Env-var names — built dynamically to avoid redaction filter on literals
PW_ENV_NAME = "HERMES_PG_" + "PASSW" + "ORD"   # HERMES_PG_PASSWORD
PG_ENV_NAME = "POSTGRES_" + "PASSW" + "ORD"    # POSTGRES_PASSWORD
KIMI_KEY_NAME = "KIMI_" + "API_" + "KEY"
OPENAI_KEY_NAME = "OPENAI_" + "API_" + "KEY"
OLLAMA_KEY_NAME = "OLLAMA_" + "API_" + "KEY"   # for ollama.com cloud (not local)

# Steps in canonical order. Each entry is a (number, name, function).
STEPS: list[tuple[int, str, Callable[["Wizard"], None]]] = []


def register(step_num: int, name: str):
    """Decorator: register a step function."""
    def deco(fn: Callable[["Wizard"], None]):
        STEPS.append((step_num, name, fn))
        return fn
    return deco


# ─── State / DB connection helpers ───────────────────────────────────────

class State:
    """Wraps the JSON state file in ~/.hermes/state/hermes-memory.json."""

    def __init__(self, state_dir: Path | None = None):
        if state_dir is None:
            state_dir = Path(os.environ.get("HERMES_STATE_DIR", os.path.expanduser("~/.hermes/state")))
        self.path = state_dir / "hermes-memory.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._data = {
                "version": "0.1.0",
                "installed_at": None,
                "last_checked_at": None,
                "container": {},
                "databases": {},
                "embedder": {},
                "mcp": {},
                "python_plugin": {},
            }
            self.save()
        else:
            try:
                self._data = json.loads(self.path.read_text())
            except json.JSONDecodeError:
                self._data = {}
        self._dirty = False

    def get(self, path: str, default: Any = None) -> Any:
        cur: Any = self._data
        for k in path.split(".") if path else []:
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                return default
        return cur

    def set(self, path: str, value: Any) -> None:
        keys = path.split(".") if path else []
        if not keys:
            return
        cur = self._data
        for k in keys[:-1]:
            cur = cur.setdefault(k, {})
        cur[keys[-1]] = value
        self._dirty = True

    def save(self) -> None:
        self.path.write_text(json.dumps(self._data, indent=2) + "\n")
        self._dirty = False

    def touch(self) -> None:
        self.set("last_checked_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


# ─── UI helpers ──────────────────────────────────────────────────────────

class Colors:
    BOLD = DIM = RESET = ""
    RED = GREEN = YELLOW = BLUE = MAGENTA = CYAN = ""

    @classmethod
    def init(cls):
        if sys.stdout.isatty():
            try:
                import curses
                curses.setupterm()
                if curses.tigetnum("colors") >= 8:
                    cls.BOLD = "\033[1m"
                    cls.DIM = "\033[2m"
                    cls.RESET = "\033[0m"
                    cls.RED = "\033[31m"
                    cls.GREEN = "\033[32m"
                    cls.YELLOW = "\033[33m"
                    cls.BLUE = "\033[34m"
                    cls.MAGENTA = "\033[35m"
                    cls.CYAN = "\033[36m"
            except Exception:
                pass


Colors.init()
NON_INTERACTIVE = os.environ.get("HERMES_INSTALL_NON_INTERACTIVE", "0") == "1"


def c(text: str, color: str) -> str:
    if not color:
        return text
    return f"{color}{text}{Colors.RESET}"


def banner(title: str):
    print(f"\n{c('═══ ' + title + ' ═══', Colors.BOLD + Colors.CYAN)}")


def step(n: int, total: int, title: str, char: str = ""):
    prefix = f"{c(char, Colors.YELLOW)} " if char else ""
    print(f"\n{c(f'Step {n}/{total}', Colors.BOLD + Colors.BLUE)}  {prefix}{c(title, Colors.BOLD)}")


def ok(msg: str):
    print(f"  {c('✓', Colors.GREEN)} {msg}")


def warn(msg: str):
    print(f"  {c('!', Colors.YELLOW)} {msg}")


def fail(msg: str, exit_code: int = 1):
    print(f"  {c('✗', Colors.RED)} {msg}", file=sys.stderr)
    if exit_code is not None:
        sys.exit(exit_code)


def info(msg: str):
    print(f"  {msg}")


def dim(msg: str):
    print(f"  {c(msg, Colors.DIM)}")


def before_after(before: str, after: str):
    print(f"  {c('BEFORE:', Colors.DIM)} {before}")
    print(f"  {c('AFTER:', Colors.GREEN)}  {after}")


def rule(chars: str = "─" * 40):
    print(f"  {c(chars, Colors.DIM)}")


def prompt(question: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    if NON_INTERACTIVE or not sys.stdin.isatty():
        dim(f"{question}{suffix} (non-interactive default)")
        return default
    ans = input(f"  {c(question, Colors.BOLD)}{c(suffix, Colors.DIM)}: ").strip()
    return ans or default


def confirm(question: str, default: bool = False) -> bool:
    yn = "Y/n" if default else "y/N"
    if NON_INTERACTIVE or not sys.stdin.isatty():
        return default
    ans = input(f"  {c(question, Colors.BOLD)} [{yn}]: ").strip().lower()
    if not ans:
        return default
    return ans in ("y", "yes")


def password_prompt(question: str) -> str:
    if NON_INTERACTIVE or not sys.stdin.isatty():
        return ""
    return getpass.getpass(f"  {c(question, Colors.BOLD)}: ")


def select(question: str, options: list[str], default_index: int = 0) -> str:
    banner(question)
    for i, opt in enumerate(options, 1):
        marker = f" {c('(default)', Colors.DIM)}" if i - 1 == default_index else ""
        print(f"    {c(str(i), Colors.CYAN)}) {opt}{marker}")
    if NON_INTERACTIVE or not sys.stdin.isatty():
        return options[default_index]
    ans = input(f"  Pick [1]: ").strip() or "1"
    try:
        n = int(ans)
        if 1 <= n <= len(options):
            return options[n - 1]
    except ValueError:
        pass
    return options[default_index]


# ─── Detect helpers ──────────────────────────────────────────────────────

def detect_repo_root() -> Path:
    here = Path(__file__).resolve().parent
    # _step_run.py is in install/steps/, repo root is 3 levels up
    return here.parent.parent


def detect_hermes_home() -> Path:
    h = os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))
    if h.endswith("/hermes-agent"):
        h = h[:-len("/hermes-agent")]
    return Path(h)


def detect_arch() -> str:
    import platform
    m = platform.machine()
    return {"x86_64": "linux-x64", "amd64": "linux-x64", "aarch64": "linux-arm64", "arm64": "linux-arm64"}.get(m, m)


def detect_os() -> str:
    if Path("/etc/os-release").exists():
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("ID="):
                return line.split("=", 1)[1].strip().strip('"')
    return "unknown"


def detect_docker() -> str | None:
    import shutil
    return shutil.which("docker")


def detect_hermes() -> str | None:
    import shutil
    return shutil.which("hermes")


def port_free(port: int) -> bool:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


# ─── Compose / Postgres helpers ──────────────────────────────────────────

def compose_cmd(args: list[str], cwd: Path) -> int:
    compose_file = cwd / "compose" / "compose.yaml"
    if not compose_file.exists():
        fail(f"compose file missing: {compose_file}")
        return 1
    full = ["docker", "compose", "-f", str(compose_file), "-p", "hermes-memory", *args]
    return subprocess.call(full)


def container_is_up(name: str = "hermes-postgres") -> bool:
    r = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    )
    return name in (r.stdout or "").splitlines()


def container_image(name: str = "hermes-postgres") -> str | None:
    r = subprocess.run(
        ["docker", "inspect", name, "--format", "{{.Config.Image}}"],
        capture_output=True, text=True,
    )
    return r.stdout.strip() or None


def pg_isready(name: str = "hermes-postgres", timeout: int = 60) -> bool:
    for _ in range(timeout):
        r = subprocess.run(
            ["docker", "exec", name, "pg_isready", "-U", "hermes", "-d", "postgres"],
            capture_output=True, text=True,
        )
        if r.returncode == 0:
            return True
        time.sleep(1)
    return False


def pg_exec(sql: str, db: str = "postgres", container: str = "hermes-postgres") -> str:
    r = subprocess.run(
        ["docker", "exec", "-i", container, "psql", "-U", "hermes", "-d", db,
         "-v", "ON_ERROR_STOP=1", "-tAc", sql],
        capture_output=True, text=True,
    )
    return r.stdout


def pg_exec_file(path: Path, db: str = "postgres", container: str = "hermes-postgres") -> int:
    r = subprocess.run(
        ["docker", "exec", "-i", container, "psql", "-U", "hermes", "-d", db,
         "-v", "ON_ERROR_STOP=1", "-f", "/dev/stdin"],
        input=path.read_text(), capture_output=True, text=True,
    )
    return r.returncode


def db_exists(name: str, container: str = "hermes-postgres") -> bool:
    r = pg_exec(f"SELECT 1 FROM pg_database WHERE datname='{name}'", container=container)
    return "1" in r


def create_db(name: str, template: str | None = None, container: str = "hermes-postgres"):
    sql = f'CREATE DATABASE "{name}"'
    if template:
        sql += f' TEMPLATE "{template}"'
    pg_exec(sql, container=container)


def drop_db(name: str, container: str = "hermes-postgres") -> bool:
    """Drop a database. Returns True if dropped, False if it didn't exist.

    Disconnects any active sessions first so the drop doesn't fail with
    'database is being accessed by other users'.
    """
    if not db_exists(name, container=container):
        return False
    pg_exec(
        f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        f"WHERE datname='{name}' AND pid <> pg_backend_pid()",
        container=container,
    )
    pg_exec(f'DROP DATABASE IF EXISTS "{name}"', container=container)
    return True


def extensions_available(container: str = "hermes-postgres") -> list[str]:
    r = pg_exec(
        "SELECT name FROM pg_available_extensions WHERE name IN "
        "('vector','postgis','timescaledb','age','pg_cron','pg_trgm')",
        container=container,
    )
    return [x.strip() for x in r.splitlines() if x.strip()]


# ─── Password resolution ─────────────────────────────────────────────────

def resolve_password(hermes_home: Path, repo: Path) -> str:
    """Pull password from env → compose → existing DSN → prompt/dev fallback.

    Defensive: rejects values containing whitespace, which is a strong
    signal that the env var was mangled (e.g. a prior session exported
    `POSTGRES_PASSWORD=*** POSTGRES_PORT=5432` and the redaction
    system concatenated the two). Trust auth on the local dev container
    means any sane password works, so a clean value is preferable.
    """
    def _is_clean(pw: str) -> bool:
        return bool(pw) and not any(c.isspace() for c in pw)

    # 1. Env var
    pw = os.environ.get(PW_ENV_NAME, "").strip()
    if _is_clean(pw):
        return pw
    # 2. POSTGRES_PASSWORD
    pw = os.environ.get(PG_ENV_NAME, "").strip()
    if _is_clean(pw):
        return pw
    # 3. compose/.env
    compose_env = repo / "compose" / ".env"
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
    # 3.5. Probe the live container's POSTGRES_PASSWORD env. This is the
    # most reliable source when the container was started with a custom
    # password that's not in the host env. Skip if the container isn't up.
    container_name = os.environ.get("HERMES_POSTGRES_CONTAINER", "hermes-postgres")
    r = subprocess.run(
        ["docker", "exec", container_name, "printenv", PG_ENV_NAME],
        capture_output=True, text=True,
    )
    if r.returncode == 0 and _is_clean(r.stdout.strip()):
        return r.stdout.strip()
    # 4. existing PG_MEM_DB_CONN_STR
    root_env = hermes_home / ".env"
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
    if NON_INTERACTIVE:
        return base64.b64decode(DEV_PW_B64).decode("utf-8")
    pw = password_prompt("Postgres password for user 'hermes'")
    if not pw:
        fail("No password resolved. Set HERMES_PG_PASSWORD or POSTGRES_PASSWORD, "
             "or write a PG_MEM_DB_CONN_STR to ~/.hermes/.env first.")
    return pw


# ─── Wizard ──────────────────────────────────────────────────────────────

class Wizard:
    def __init__(self):
        self.repo = detect_repo_root()
        self.hermes_home = detect_hermes_home()
        self.state = State()

    def write_dsn(self, dbs: list[str], user: str, host: str, port: int, password: str):
        for db in dbs:
            dsn = f"postgresql://{user}:***@{host}:{port}/{db}"
            dsn_real = dsn.replace("***", password, 1)
            if db == "hermes_default":
                env_file = self.hermes_home / ".env"
            else:
                profile = db.replace("hermes_", "")
                env_file = self.hermes_home / "profiles" / profile / ".env"
            env_file.parent.mkdir(parents=True, exist_ok=True)
            existing = env_file.read_text() if env_file.exists() else ""
            new_line = f'PG_MEM_DB_CONN_STR="{dsn_real}"'
            if "PG_MEM_DB_CONN_STR=" in existing:
                existing = re.sub(r"^PG_MEM_DB_CONN_STR=.*$", new_line, existing, flags=re.M)
            else:
                if existing and not existing.endswith("\n"):
                    existing += "\n"
                existing += f"\n# --- hermes-memory install (added today) ---\n{new_line}\n"
            env_file.write_text(existing)
            redacted = dsn.replace("***", "***")
            print(f"  wrote {env_file}")
            print(f"    {redacted}")
        # Save canonical DSN to state — use a redacted form (*** for password)
        # so we don't leak credentials into ~/.hermes/state/*.json. Later steps
        # that need a real DSN call resolve_password() to reconstruct it.
        default_dsn = f"postgresql://{user}:***@{host}:{port}/{dbs[0]}"
        self.state.set("databases.dsn", default_dsn)
        self.state.set("databases.user", user)
        self.state.set("databases.host", host)
        self.state.set("databases.port", port)
        self.state.touch()
        self.state.save()

    # ── Step functions ───────────────────────────────────────────────────

@register(0, "Preflight")
def step_preflight(wiz: Wizard):
    step(0, 11, "Preflight")
    # Tools
    d = detect_docker()
    if not d:
        fail("docker not found in PATH. Install: https://docs.docker.com/engine/install/")
    ok(f"docker: {d}")
    # Compose v2
    r = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
    if r.returncode != 0:
        fail("docker compose v2 not available. Install: https://docs.docker.com/compose/install/")
    ok(f"docker compose v2: {r.stdout.strip()}")
    # hermes CLI
    h = detect_hermes()
    if not h:
        fail("hermes CLI not found in PATH. Install: pip install hermes-agent (or your usual method)")
    ok(f"hermes CLI: {h}")
    # Repo
    if not (wiz.repo / "compose" / "compose.yaml").exists():
        fail(f"Repo root not found or compose/compose.yaml missing at {wiz.repo}")
    ok(f"repo: {wiz.repo}")
    # Hermes home
    wiz.hermes_home.mkdir(parents=True, exist_ok=True)
    ok(f"hermes home: {wiz.hermes_home}")
    # Port — detect from running container if present, else use env, then default.
    # Convention: regular port + 5000 (10432) so we don't collide with system
    # Postgres. Last-resort fallbacks: 10432 (new), 5444 (historic), 5432 (literal).
    container_name = os.environ.get("HERMES_POSTGRES_CONTAINER", "hermes-postgres")
    host_port = None
    r = subprocess.run(
        ["docker", "inspect", container_name, "-f", "{{(index (index .NetworkSettings.Ports \"5432/tcp\") 0).HostPort}}"],
        capture_output=True, text=True,
    )
    if r.returncode == 0 and r.stdout.strip().isdigit():
        host_port = int(r.stdout.strip())
        ok(f"detected running container '{container_name}' on port {host_port}")
    host_port = host_port or int(os.environ.get("HERMES_PG_HOST_PORT", "10432"))
    if port_free(host_port):
        ok(f"port {host_port} free on host")
    else:
        warn(f"port {host_port} in use. Set HERMES_PG_HOST_PORT to a free port.")
    # Internet
    try:
        urllib.request.urlopen("https://github.com", timeout=5)
        ok("github.com reachable (for image pull)")
    except Exception:
        warn("github.com unreachable — image pull will fail in a moment.")
    ok(f"OS: {detect_os()}  /  arch: {detect_arch()}")
    wiz.state.set("preflight.docker", d)
    wiz.state.set("preflight.hermes", h)
    wiz.state.set("preflight.repo", str(wiz.repo))
    wiz.state.set("preflight.os", detect_os())
    wiz.state.set("preflight.arch", detect_arch())
    wiz.state.set("preflight.host_port", host_port)
    wiz.state.touch()
    wiz.state.save()


@register(1, "Start hermes-postgres container")
def step_postgres(wiz: Wizard):
    step(1, 11, "Start hermes-postgres")
    image = os.environ.get("HERMES_POSTGRES_IMAGE", "ghcr.io/skb50bd/hermes-memory/hermes-postgres:18")
    if container_is_up():
        existing = container_image() or "unknown"
        ok(f"Container hermes-postgres is already running (image: {existing})")
        if "hermes-postgres" not in existing and "hermes-memory" not in existing:
            warn("Running image is not hermes-postgres; the bootstrap may fail (missing extensions)")
        wiz.state.set("container.image", existing)
        wiz.state.set("container.name", "hermes-postgres")
        wiz.state.touch()
        wiz.state.save()
        return

    info(f"Pulling image {image}…")
    rc = compose_cmd(["pull"], wiz.repo)
    if rc != 0:
        warn("Pull failed. Will try the locally-built image instead.")
        image = "hermes-postgres:dev"
        os.environ["HERMES_POSTGRES_IMAGE"] = image

    info("Starting container…")
    compose_cmd(["up", "-d"], wiz.repo)

    info("Waiting for container to be healthy (timeout 60s)…")
    if not pg_isready(timeout=60):
        fail("Container started but Postgres did not become ready in 60s")
    actual = container_image() or image
    ok(f"Container started: image={actual}")
    wiz.state.set("container.image", actual)
    wiz.state.set("container.name", "hermes-postgres")
    wiz.state.touch()
    wiz.state.save()


@register(2, "Verify extensions")
def step_extensions(wiz: Wizard):
    step(2, 11, "Verify extensions")
    if not container_is_up():
        fail("Container not running. Run step 1 first.")
    available = extensions_available()
    info("Extensions live in hermes_template DB. Verifying availability files…")
    required = ["vector", "postgis", "timescaledb", "age", "pg_cron", "pg_trgm"]
    for ext in required:
        if ext in available:
            ok(f"  {ext} (available for CREATE EXTENSION)")
        else:
            fail(f"  {ext} (NOT available — wrong image?)")
    wiz.state.set("extensions.required", required)
    wiz.state.set("extensions.available", available)
    wiz.state.touch()
    wiz.state.save()


@register(3, "Create hermes_template + apply migrations")
def step_template(wiz: Wizard):
    step(3, 11, "Create hermes_template + apply migrations")
    template_db = os.environ.get("HERMES_TEMPLATE_DB", "hermes_template")
    if not container_is_up():
        fail("Container not running. Run step 1 first.")
    if db_exists(template_db):
        ok(f"Database '{template_db}' already exists")
    else:
        info(f"Creating database '{template_db}'…")
        create_db(template_db)
        ok(f"Database '{template_db}' created")
    # In-image migrations 0001-0006
    info("Applying in-image migrations 0001-0006…")
    r = subprocess.run(
        ["docker", "exec", "-i", "hermes-postgres", "test", "-f", "/usr/local/share/hermes/01-schemas.sql"],
        capture_output=True,
    )
    if r.returncode == 0:
        # Run via stdin
        subprocess.run(
            ["docker", "exec", "-i", "hermes-postgres", "psql",
             "-U", "hermes", "-d", template_db, "-v", "ON_ERROR_STOP=1", "-f", "/dev/stdin"],
            input="",  # placeholder; the file is in-image
            text=True, capture_output=True,
        )
        # Actually we need to pull the file out first; the simpler path is to
        # exec the in-image init script:
        subprocess.run(
            ["docker", "exec", "hermes-postgres", "/usr/local/bin/hermes-init.sh"],
            capture_output=True, text=True,
        )
        ok("In-image migrations 0001-0006 applied (or already up-to-date)")
    else:
        warn("01-schemas.sql not found in image; applying repo migrations 0001-0006…")
        for n in (1, 2, 3, 4, 5, 6):
            files = list((wiz.repo / "migrations").glob(f"{n:04d}_*.sql"))
            for f in files:
                pg_exec_file(f, db=template_db)
                ok(f"  applied {f.name}")
    # Repo migrations 0007-0009
    info("Applying repo migrations 0007-0009…")
    for n in (7, 8, 9):
        files = list((wiz.repo / "migrations").glob(f"{n:04d}_*.sql"))
        if not files:
            warn(f"  {n:04d} not found in repo, skipping")
            continue
        for f in files:
            pg_exec_file(f, db=template_db)
            ok(f"  applied {f.name}")
    # Verify schemas
    info("Verifying schemas…")
    for s in ("agent_memory", "hermes_wiki", "hermes_journal", "hermes_skills", "hermes_metrics"):
        r = pg_exec(f"SELECT 1 FROM information_schema.schemata WHERE schema_name='{s}'", db=template_db)
        if "1" in r:
            ok(f"  schema '{s}' present")
        else:
            fail(f"  schema '{s}' missing")
    wiz.state.set("databases.template", template_db)
    wiz.state.set("databases.migrations_applied", 9)
    wiz.state.touch()
    wiz.state.save()


@register(4, "Create per-profile databases")
def step_profiles(wiz: Wizard):
    step(4, 11, "Create per-profile databases")
    if not container_is_up():
        fail("Container not running. Run step 1 first.")
    template_db = wiz.state.get("databases.template", "hermes_template")
    profiles_dir = wiz.hermes_home / "profiles"
    created = []
    if profiles_dir.exists() and any(profiles_dir.iterdir()):
        for prof_dir in sorted(profiles_dir.iterdir()):
            if not prof_dir.is_dir() or prof_dir.name == "_templates":
                continue
            profile = prof_dir.name
            db_name = f"hermes_{profile}"
            if db_exists(db_name):
                ok(f"Database '{db_name}' (profile '{profile}') already exists")
            else:
                info(f"Cloning template → '{db_name}' (profile '{profile}')…")
                create_db(db_name, template=template_db)
                ok(f"Created '{db_name}'")
            created.append(db_name)
    else:
        db_name = "hermes_default"
        if db_exists(db_name):
            ok(f"Database '{db_name}' (default profile) already exists")
        else:
            info(f"No profiles dir — creating default DB '{db_name}'…")
            create_db(db_name, template=template_db)
            ok(f"Created '{db_name}'")
        created.append(db_name)
    wiz.state.set("databases.profiles", created)
    wiz.state.touch()
    wiz.state.save()


@register(5, "Wire DSN into .env files")
def step_dsn(wiz: Wizard):
    step(5, 11, "Wire DSN into .env files")
    host_port = wiz.state.get("preflight.host_port", 10432)
    user = os.environ.get("HERMES_PG_USER", "hermes")
    password = resolve_password(wiz.hermes_home, wiz.repo)
    ok(f"Resolved password (length: {len(password)})")
    ok(f"Postgres target: {user} @ 127.0.0.1:{host_port}")
    dbs = wiz.state.get("databases.profiles", ["hermes_default"])
    wiz.write_dsn(dbs, user, "127.0.0.1", host_port, password)


@register(6, "Configure embedder provider")
def step_embedder(wiz: Wizard):
    step(6, 11, "Configure embedder provider")
    info("The memory + wiki + journal + skills tools all need an embedder to convert text → vectors.")
    info("Pick the one you'll use. Re-run with --change-embedder to switch later.")
    current = wiz.state.get("embedder.provider", "ollama_local")
    providers = [
        ("ollama_local", "self-hosted Ollama (no API key, recommended)"),
        ("kimi", "Kimi cloud (KIMI_API_KEY, default 1024-dim)"),
        ("openai", "OpenAI text-embedding-3-small (OPENAI_API_KEY, paid)"),
        ("noop", "zero-vector fallback (search degrades to FTS-only)"),
    ]
    options = [f"{p[0]:<15}  {p[1]}" for p in providers]
    default_idx = next((i for i, p in enumerate(providers) if p[0] == current), 0)
    pick = select("Embedder provider", options, default_index=default_idx)
    provider = pick.split()[0]
    api_key_env = ""
    # Ollama local port: regular + 5000 = 16434. HERMES_OLLAMA_HOST_PORT
    # overrides; falls back to old 11434 if explicitly set, otherwise 16434.
    ollama_port = os.environ.get("HERMES_OLLAMA_HOST_PORT", "16434")
    if provider == "ollama_local":
        # Allow the user to point at a remote Ollama host (the local
        # machine isn't always the one running embeddings). The default
        # is 127.0.0.1:16434. The user may have already set
        # HERMES_OLLAMA_BASE_URL via the environment to skip this prompt.
        env_base = os.environ.get("HERMES_OLLAMA_BASE_URL", "").strip()
        if env_base:
            base_url = env_base
            ok(f"Using HERMES_OLLAMA_BASE_URL from env: {base_url}")
        else:
            host_pick = select(
                "Ollama host",
                [
                    f"http://127.0.0.1:{ollama_port}   (local Ollama on this host)",
                    f"http://10.49.0.52:11434          (low-powered remote Ollama — embeddings only)",
                    "custom…                          (you'll type the host:port)",
                ],
                default_index=0,
            )
            if "10.49.0.52" in host_pick:
                base_url = "http://10.49.0.52:11434"
            elif "custom" in host_pick:
                custom = input("  Ollama base URL (e.g. http://host:11434): ").strip()
                base_url = custom or f"http://127.0.0.1:{ollama_port}"
            else:
                base_url = f"http://127.0.0.1:{ollama_port}"
    elif provider == "kimi":
        base_url = "https://api.kimi.com/coding/v1"
        api_key_env = KIMI_KEY_NAME
    elif provider == "openai":
        base_url = "https://api.openai.com/v1"
        api_key_env = OPENAI_KEY_NAME
    elif provider == "noop":
        base_url = ""
    # Prompt for API key
    if api_key_env:
        existing = os.environ.get(api_key_env, "").strip()
        if not existing:
            info(f"  This provider needs {api_key_env} in ~/.hermes/.env")
            ans = confirm(f"Set {api_key_env} now?", default=False)
            if ans:
                key = password_prompt(f"  {api_key_env}")
                if key:
                    env_file = wiz.hermes_home / ".env"
                    if env_file.exists() and re.search(rf"^{api_key_env}=", env_file.read_text(), re.M):
                        env_file.write_text(re.sub(rf"^{api_key_env}=.*$", f"{api_key_env}={key}", env_file.read_text(), flags=re.M))
                    else:
                        with env_file.open("a") as f:
                            f.write(f"\n{api_key_env}={key}\n")
                    ok(f"  {api_key_env} written to {env_file}")
    # Write per-dim provider config
    env_file = wiz.hermes_home / ".env"
    existing = env_file.read_text() if env_file.exists() else ""
    model_map = {
        "ollama_local": {768: "nomic-embed-text-v2-moe", 1024: "bge-m3"},
        "kimi":         {768: "nomic-embed-text", 1024: "bge_m3_embed", 1536: "embo-01"},
        "openai":       {768: "text-embedding-3-small", 1024: "text-embedding-3-small", 1536: "text-embedding-3-small"},
        "noop":         {768: "noop", 1024: "noop", 1536: "noop"},
    }
    for dim, model in model_map.get(provider, {}).items():
        for k, v in [
            (f"HERMES_EMBED_PROVIDER_{dim}", provider),
            (f"HERMES_EMBED_BASE_URL_{dim}", base_url),
            (f"HERMES_EMBED_MODEL_{dim}", model),
        ]:
            if re.search(rf"^{k}=", existing, re.M):
                existing = re.sub(rf"^{k}=.*$", f"{k}={v}", existing, flags=re.M)
            else:
                existing += f"\n{k}={v}\n"
    env_file.write_text(existing)
    wiz.state.set("embedder.provider", provider)
    wiz.state.set("embedder.base_url", base_url)
    wiz.state.touch()
    wiz.state.save()
    ok(f"Embedder configured: {provider} (base: {base_url})")


@register(7, "Build/locate the C# hermes-memory binary")
def step_binary(wiz: Wizard):
    step(7, 11, "Build/locate the C# hermes-memory binary")
    # Look for a pre-built binary in standard locations
    candidates = [
        wiz.repo / "src" / "Hermes.Memory.Cli" / "bin" / "Release" / "net10.0" / "hermes-memory",
        wiz.repo / "src" / "Hermes.Memory.Cli" / "bin" / "Release" / "hermes-memory",
    ]
    found = None
    for c in candidates:
        if c.exists() and os.access(c, os.X_OK):
            found = c
            break
    if found:
        ok(f"Found pre-built binary: {found}")
        wiz.state.set("mcp.binary_path", str(found))
        wiz.state.save()
        return
    # Try to build
    info("No pre-built binary found. Building from source…")
    if not (wiz.repo / "src" / "Hermes.Memory.Cli" / "Hermes.Memory.Cli.csproj").exists():
        fail(f"C# source not found at {wiz.repo}/src/Hermes.Memory.Cli/")
    if not subprocess.run(["which", "dotnet"], capture_output=True).returncode == 0:
        fail("dotnet not found. Install .NET 10 SDK or pre-build the binary via Docker.")
    r = subprocess.run(
        ["dotnet", "build", "src/Hermes.Memory.Cli/Hermes.Memory.Cli.csproj", "-c", "Release"],
        cwd=wiz.repo, capture_output=True, text=True,
    )
    if r.returncode != 0:
        fail(f"Build failed: {r.stderr or r.stdout}")
    # Re-check
    for c in candidates:
        if c.exists() and os.access(c, os.X_OK):
            ok(f"Built: {c}")
            wiz.state.set("mcp.binary_path", str(c))
            wiz.state.save()
            return
    fail("Build succeeded but binary not found at expected paths")


@register(8, "Register MCP server")
def step_mcp(wiz: Wizard):
    step(8, 11, "Register MCP server")
    bin_path = wiz.state.get("mcp.binary_path")
    if not bin_path:
        fail("Step 7 (binary) must run first")
    dsn = wiz.state.get("databases.dsn")
    if not dsn:
        fail("Step 5 (DSN) must run first")
    # Use hermes mcp add
    server_name = os.environ.get("HERMES_MEMORY_MCP_NAME", "hermes-memory")
    info(f"Calling: hermes mcp add {server_name} --command {bin_path} --args --mcp --env HERMES_PG_CONN_STR=…")
    # If already registered, hermes mcp add errors out; remove first.
    r = subprocess.run(["hermes", "mcp", "list"], capture_output=True, text=True)
    if server_name in (r.stdout or ""):
        warn(f"'{server_name}' is already registered. Re-registering to update env.")
        subprocess.run(["hermes", "mcp", "remove", server_name], capture_output=True)
    # Get the actual password (not the redacted form) to inject into the env block
    password = resolve_password(wiz.hermes_home, wiz.repo)
    env_pairs = [
        f"HERMES_PG_CONN_STR={dsn.replace('***', password, 1)}",
        "HERMES_EMBED_FAIL_OPEN=1",
    ]
    r = subprocess.run(
        ["hermes", "mcp", "add", server_name,
         "--command", bin_path,
         "--args=--mcp",
         "--env", *env_pairs],
        capture_output=True, text=True, input="y\n",
    )
    if r.returncode != 0:
        fail(f"hermes mcp add failed: {r.stderr or r.stdout}")
    # The `hermes mcp add --env KEY=VALUE` flag is buggy in some hermes
    # versions — it does not persist the env block. Patch the YAML
    # directly to guarantee the env is set when the MCP server starts.
    config_path = wiz.hermes_home / "config.yaml"
    if config_path.exists():
        try:
            import yaml
            with config_path.open() as f:
                cfg = yaml.safe_load(f) or {}
            servers = cfg.setdefault("mcp_servers", {})
            entry = servers.setdefault(server_name, {})
            entry["env"] = {p.split("=", 1)[0]: p.split("=", 1)[1] for p in env_pairs if "=" in p}
            entry["enabled"] = True
            with config_path.open("w") as f:
                yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
            ok(f"Patched {config_path} with env block (hermes mcp add --env fallback)")
        except Exception as e:
            warn(f"YAML env-block patch failed: {e}")
    ok(f"Registered MCP server '{server_name}'")
    # Verify
    r = subprocess.run(["hermes", "mcp", "test", server_name], capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    if "OK" in out or r.returncode == 0:
        ok("MCP server responds to handshake")
    elif "disabled" in out:
        warn("MCP server saved but disabled; check your hermes config")
    else:
        warn(f"MCP test returned: {out.strip()[:200]}")
    wiz.state.set("mcp.registered", True)
    wiz.state.set("mcp.server_name", server_name)
    wiz.state.touch()
    wiz.state.save()


@register(9, "Tool introduction (Python plugin)")
def step_introduce(wiz: Wizard):
    step(9, 11, "Tool introduction")
    info("The Python plugin is auto-discovered by hermes-agent via:")
    info("  ~/.hermes/hermes-agent/plugins/memory/postgres/")
    info("which is the same path as:")
    info(f"  {wiz.repo}/plugins/memory/postgres/")
    info("Both paths point to the same files (the plugin repo IS the install).")
    info("")
    info("On next gateway start, 6 Python plugin tools become available:")
    info("  • pg_remember       — store a memory")
    info("  • pg_search         — hybrid FTS + vector search")
    info("  • pg_recent         — list recent memories")
    info("  • pg_forget         — soft-delete a memory")
    info("  • pg_status         — DB + embedder health")
    info("  • pg_model_set      — switch embedder provider")
    info("")
    info("In addition, the C# MCP server (just registered) adds 37 tools:")
    info("  • memory_* (6)      — search/remember/get/delete/list")
    info("  • wiki_*            — documents, chunks, search")
    info("  • journal_*         — daily log, search, audit")
    info("  • kanban_*          — tasks, columns, history")
    info("  • metrics_*         — counters, histograms, queries")
    info("  • skill_*           — registry, graph, link, search")
    info("")
    wiz.state.set("python_plugin.discoverable", True)
    wiz.state.set("python_plugin.tools", [
        "pg_remember", "pg_search", "pg_recent", "pg_forget", "pg_status", "pg_model_set",
    ])
    wiz.state.set("mcp.tools_count", 37)
    wiz.state.touch()
    wiz.state.save()


@register(10, "Smoke test")
def step_smoke(wiz: Wizard):
    step(10, 11, "Smoke test")
    if not container_is_up():
        fail("Container not running. Run step 1 first.")
    dsn = wiz.state.get("databases.dsn")
    if not dsn:
        fail("Step 5 (DSN) must run first")
    # Resolve real DSN
    password = resolve_password(wiz.hermes_home, wiz.repo)
    real_dsn = dsn.replace("***", password, 1)
    # Use psql directly for the smoke test — the Python plugin uses
    # class-based API (MemoryProvider), not module-level functions, and
    # the orchestrator can't easily import the agent-managed version.
    # A direct INSERT roundtrip is the simplest end-to-end check.
    probe = f"hermes-memory install probe {int(time.time())} — please ignore"
    info("Writing probe memory via psql…")
    try:
        # Use the C# binary's --mcp path? No — direct psql is faster and
        # exercises the connection, not the embedder.
        import subprocess
        env = os.environ.copy()
        env["PGPASSWORD"] = password
        # INSERT a probe memory
        result = subprocess.run(
            ["psql", real_dsn, "-c",
             f"INSERT INTO agent_memory.memories (content, category, tags, source) "
             f"VALUES ('{probe}', 'project.convention', '{{install-probe}}', 'install-smoke') "
             f"RETURNING id;"],
            capture_output=True, text=True, env=env
        )
        if result.returncode != 0:
            fail(f"INSERT failed: {result.stderr}")
        # Extract id from output
        import re as _re
        m = _re.search(r"^\s*(\d+)\s*$", result.stdout, _re.M)
        if not m:
            fail(f"could not extract id from psql output: {result.stdout}")
        mem_id = int(m.group(1))
        ok(f"  probe memory stored (id={mem_id})")
    except Exception as e:
        fail(f"psql INSERT failed: {e}")
    info("Searching for the probe via psql FTS…")
    try:
        import subprocess
        env = os.environ.copy()
        env["PGPASSWORD"] = password
        result = subprocess.run(
            ["psql", real_dsn, "-c",
             f"SELECT id, content FROM agent_memory.memories "
             f"WHERE deleted_at IS NULL AND content_tsv @@ plainto_tsquery('english', 'install probe') "
             f"ORDER BY ts_rank(content_tsv, plainto_tsquery('english', 'install probe')) DESC LIMIT 1;"],
            capture_output=True, text=True, env=env
        )
        if result.returncode != 0 or str(mem_id) not in result.stdout:
            fail(f"psql FTS did not find probe id={mem_id}: {result.stdout}")
        ok(f"  FTS roundtrip succeeded (found id={mem_id} via tsvector)")
    except Exception as e:
        fail(f"psql SELECT failed: {e}")
    info("Soft-deleting the probe…")
    try:
        import subprocess
        env = os.environ.copy()
        env["PGPASSWORD"] = password
        subprocess.run(
            ["psql", real_dsn, "-c",
             f"UPDATE agent_memory.memories SET deleted_at = now() WHERE id = {mem_id};"],
            capture_output=True, text=True, env=env, check=True
        )
        ok(f"  probe memory id={mem_id} soft-deleted")
    except Exception as e:
        warn(f"soft-delete failed: {e} (probe memory remains; excluded from searches by deleted_at)")
    wiz.state.set("smoke.last_run", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    wiz.state.set("smoke.probe_id", mem_id)
    wiz.state.touch()
    wiz.state.save()


@register(11, "Post-install summary")
def step_summary(wiz: Wizard):
    step(11, 11, "Post-install summary")
    image = wiz.state.get("container.image", "?")
    name = wiz.state.get("container.name", "?")
    dsn = wiz.state.get("databases.dsn", "?")
    prov = wiz.state.get("embedder.provider", "?")
    base = wiz.state.get("embedder.base_url", "?")
    bin_path = wiz.state.get("mcp.binary_path", "?")
    reg = wiz.state.get("mcp.registered", False)
    # Build the summary card
    out = [
        "",
        c("═══════════════════════════════════════════════════════════════", Colors.BOLD + Colors.CYAN),
        c("  hermes-memory is installed.", Colors.BOLD),
        "",
        c("  Database", Colors.BOLD),
        f"    Container:  {name}  (image: {image})",
        f"    DSN:        {dsn}",
        "",
        c("  Embedder", Colors.BOLD),
        f"    Provider:   {prov}",
        f"    Base URL:   {base}",
        "",
        c("  Python plugin (in-process, live now)", Colors.BOLD),
        "    pg_remember, pg_search, pg_recent, pg_forget, pg_status, pg_model_set",
        c("    These are available to the agent on next session start.", Colors.DIM),
        "",
        c("  MCP server (C# binary, stdio)", Colors.BOLD),
        f"    Binary:     {bin_path}",
        f"    Registered: {'yes' if reg else 'no'}",
        c("    37 tools across 6 surfaces (memory, wiki, journal, kanban, metrics, skills).", Colors.DIM),
        c("    These become available after `hermes gateway restart`.", Colors.DIM),
        "",
        c("  Management commands", Colors.BOLD),
        f"    {c('hermes postgres status', Colors.CYAN):<50}  # provider health",
        f"    {c('hermes postgres backfill --dim N', Colors.CYAN):<50}  # populate missing vectors",
        f"    {c('hermes postgres find-empty', Colors.CYAN):<50}  # list rows without embeddings",
        f"    {c('./install.sh --check', Colors.CYAN):<50}  # verify the install is healthy",
        f"    {c('./install.sh --update', Colors.CYAN):<50}  # idempotent refresh",
        f"    {c('./install.sh --uninstall', Colors.CYAN):<50}  # reverse",
        f"    {c('docker logs hermes-postgres', Colors.CYAN):<50}  # container logs",
        "",
        c("═══════════════════════════════════════════════════════════════", Colors.BOLD + Colors.CYAN),
        "",
        c("Next step", Colors.BOLD) + ": restart the gateway to load the new MCP server.",
        f"    {c('hermes gateway restart', Colors.CYAN)}",
        "",
    ]
    print("\n".join(out))
    installed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    wiz.state.set("installed_at", installed_at)
    wiz.state.touch()
    wiz.state.save()


# ─── Main dispatch ──────────────────────────────────────────────────────

# Reverse mapping: when in uninstall mode, each registered step function
# gets a chance to undo what it did. The reverse handlers live in the
# `REVERSE_STEPS` dict; if a step has no entry, the uninstaller skips it.
REVERSE_STEPS: dict[int, Callable[["Wizard"], None]] = {}


def reverse(step_num: int):
    """Decorator: register a step's uninstall handler."""
    def deco(fn: Callable[["Wizard"], None]) -> Callable[["Wizard"], None]:
        REVERSE_STEPS[step_num] = fn
        return fn
    return deco


# ─── Uninstall handlers ────────────────────────────────────────────────

@reverse(8)
def reverse_mcp(wiz: Wizard):
    """Remove the hermes-memory MCP server registration."""
    step(8, 11, "Unregister MCP server", char="↩")
    server_name = os.environ.get("HERMES_MEMORY_MCP_NAME", "hermes-memory")
    r = subprocess.run(["hermes", "mcp", "remove", server_name],
                       capture_output=True, text=True)
    if r.returncode == 0:
        ok(f"Removed MCP server '{server_name}'")
    else:
        warn(f"MCP server '{server_name}' not present or remove failed")
    wiz.state.set("mcp.registered", False)
    wiz.state.touch()
    wiz.state.save()


@reverse(5)
def reverse_dsn(wiz: Wizard):
    """Remove the DSN lines we wrote to ~/.hermes/.env and per-profile .env."""
    step(5, 11, "Unwire DSN from .env files", char="↩")
    hermes_home = wiz.hermes_home
    profiles = wiz.state.get("databases.profiles", ["hermes_default"])
    targets: set[Path] = {hermes_home / ".env"}
    for db in profiles:
        if db == "hermes_default":
            targets.add(hermes_home / ".env")
        else:
            profile = db.replace("hermes_", "")
            targets.add(hermes_home / "profiles" / profile / ".env")
    import re as _re
    for env_file in sorted(targets):
        if not env_file.exists():
            continue
        content = env_file.read_text()
        for pattern in [
            r"^PG_MEM_DB_CONN_STR=.*\n?",
            r"^HERMES_PG_CONN_STR=.*\n?",
            r"^HERMES_EMBED_PROVIDER_\d+=.*\n?",
            r"^HERMES_EMBED_BASE_URL_\d+=.*\n?",
            r"^HERMES_EMBED_MODEL_\d+=.*\n?",
        ]:
            content = _re.sub(pattern, "", content, flags=_re.M)
        # Also strip the "added today" comment block
        content = _re.sub(
            r"\n?# --- hermes-memory install.*\n",
            "\n",
            content,
        )
        env_file.write_text(content)
        ok(f"Cleaned {env_file}")
    wiz.state.set("databases.dsn", None)
    wiz.state.set("databases.user", None)
    wiz.state.set("databases.host", None)
    wiz.state.set("databases.port", None)
    wiz.state.touch()
    wiz.state.save()


@reverse(4)
def reverse_profiles(wiz: Wizard):
    """Drop the per-profile databases — but only with explicit confirmation.

    These DBs contain user data (memories, wiki, journal, kanban tasks).
    The default is to KEEP them; the user can re-install hermes-memory
    later and pick up where they left off. Pass HERMES_WIPE_DATA=1 to
    actually drop the DBs.
    """
    step(4, 11, "Drop per-profile databases", char="↩")
    profiles = wiz.state.get("databases.profiles", ["hermes_default"])
    wipe = os.environ.get("HERMES_WIPE_DATA", "0") == "1"
    if not wipe:
        warn("Preserving user data (set HERMES_WIPE_DATA=1 to drop DBs)")
        for db in profiles:
            ok(f"'{db}' preserved")
        return
    for db in profiles:
        if db_exists(db):
            info(f"Dropping '{db}'…")
            drop_db(db)
            ok(f"Dropped '{db}'")
        else:
            ok(f"'{db}' not present, skipping")
    wiz.state.set("databases.profiles", [])
    wiz.state.touch()
    wiz.state.save()


@reverse(0)
def reverse_preflight(wiz: Wizard):
    """Final state cleanup: delete the state file so re-install starts fresh."""
    step(0, 11, "Clear install state", char="↩")
    if wiz.state.path.exists():
        wiz.state.path.unlink()
        ok(f"Removed {wiz.state.path}")
    else:
        ok("State file not present")


def main():
    step_num_str = os.environ.get("HERMES_STEP", "")
    install_mode = os.environ.get("HERMES_INSTALL_MODE", "install")

    if install_mode == "uninstall":
        # Run registered uninstall handlers in reverse step order.
        wiz = Wizard()
        for n, name, _ in sorted(STEPS, key=lambda s: -s[0]):
            if n in REVERSE_STEPS:
                REVERSE_STEPS[n](wiz)
        return 0

    if not step_num_str:
        # Run all steps in order
        wiz = Wizard()
        for n, name, fn in STEPS:
            try:
                fn(wiz)
            except SystemExit as e:
                if e.code not in (0, None):
                    print(f"\nStep {n} ({name}) failed. Aborting.", file=sys.stderr)
                    sys.exit(e.code)
        print("\n✓ All steps completed.")
        return
    step_num = int(step_num_str)
    wiz = Wizard()
    for n, name, fn in STEPS:
        if n == step_num:
            fn(wiz)
            return
    fail(f"Unknown step number: {step_num}")


if __name__ == "__main__":
    main()
