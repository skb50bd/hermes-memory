"""Install steps — the actual work each wizard step performs.

Each step is independent: it gets a `state_dir` and runs subprocesses
(file edits, docker, psql) via the helpers in `_helpers.py`. Tests
monkeypatch the helper methods to avoid touching real docker / psql.

Public surface — 8 step classes, one per StepName:
  PreflightStep       — checks python version, docker, port
  PostgresStep        — starts the hermes-postgres container
  ExtensionsStep      — creates the 5 PG extensions
  TemplateStep        — creates hermes_template (5 schemas)
  ProfileDbStep       — clones hermes_template → hermes_<profile>
  DsnStep             — writes HERMES_PG_CONN_STR to ~/.hermes/.env
  EmbedderStep        — verifies the embedder is reachable
  RegisterPluginStep  — wires config.yaml + plugins.enabled + drops
                        the v2 plugin files into
                        ~/.hermes/plugins/hermes-postgres-memory/
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, ClassVar

# Re-exported at module scope so test monkeypatching on
# ``hermes_memory.install.steps.psycopg.connect`` can resolve the
# attribute. MigrateStep's run() also imports it locally; both paths
# refer to the same module object.
import psycopg  # noqa: F401  (re-export for tests)
import yaml

from hermes_memory.install.paths import (
    HERMES_CONFIG_PATH,
    HERMES_ENV_PATH,
    HERMES_HOME,
    HERMES_MEMORY_SHIM_DIR,
    HERMES_PG_CONN_STR_DEFAULT,
    HERMES_PLUGINS_DIR,
    HERMES_STATE_PATH,
    PLUGIN_NAME,
)
from hermes_memory.install.state import StepName, StepResult

# Path to the v2 package's bundled assets (plugin.yaml, entry.py) that
# get dropped into ~/.hermes/plugins/<PLUGIN_NAME>/ at install time.
# Tests override this; production resolves to the installed package
# data via importlib.resources.
PACKAGE_DATA_ROOT = Path(str(resources.files("hermes_memory")))

# ---------------------------------------------------------------------------
# Back-compat re-exports — `from hermes_memory.install.steps import HERMES_HOME`
# continues to work.
# ---------------------------------------------------------------------------
__all__ = [
    "HERMES_HOME",
    "HERMES_ENV_PATH",
    "HERMES_CONFIG_PATH",
    "HERMES_STATE_PATH",
    "HERMES_PLUGINS_DIR",
    "HERMES_MEMORY_SHIM_DIR",
    "HERMES_PG_CONN_STR_DEFAULT",
    "PLUGIN_NAME",
    "PreflightStep",
    "DsnStep",
    "RegisterPluginStep",
]


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
@dataclass
class _BaseStep:
    state_dir: Path
    # Subclasses MUST set this to the StepName they correspond to.
    # ClassVar (not a dataclass field) so subclasses can override cleanly.
    step_name: ClassVar[StepName] = StepName.PREFLIGHT

    def run(self) -> StepResult:
        raise NotImplementedError

    def _result(self, success: bool, message: str) -> StepResult:
        return StepResult(
            step=self.step_name,
            status="ran" if success else "failed",
            message=message,
        )


# ---------------------------------------------------------------------------
# 1. Preflight
# ---------------------------------------------------------------------------
class PreflightStep(_BaseStep):
    """Checks python version, docker presence, port availability."""

    REQUIRED_PYTHON = (3, 11)
    step_name = StepName.PREFLIGHT

    def _check_python(self) -> bool:
        import sys

        return sys.version_info >= self.REQUIRED_PYTHON

    def _check_docker(self) -> bool:
        return shutil.which("docker") is not None

    def _check_port(self, port: int) -> bool:
        import socket

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                return False
            return True

    def _is_postgres_listening(self, port: int) -> bool:
        """Open a TCP connection to localhost:port and verify the
        server speaks the Postgres protocol (issues an error response
        to a startup message with an empty body — that's a PG server,
        not a random TCP service).
        """
        import socket

        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2) as s:
                # Postgres v3 startup packet: length(int32) + protocol(196608) + user/db
                # Sending an empty/minimal startup gets us back an ErrorResponse
                # which contains "SFATAL" or "C" codes — that's PG.
                # Simpler heuristic: send the magic 8-byte request, see if
                # we get a 1-byte response of any kind.
                s.sendall(b"\x00\x00\x00\x08\x04\xd2\x16\x2f")  # SSL request
                s.settimeout(2)
                data = s.recv(1)
                # PG responds with 'S' (allow SSL) or 'N' (no SSL), or
                # 'E' (error). Anything else = not PG.
                return bool(data) and data in (b"S", b"N", b"E")
        except (TimeoutError, OSError):
            return False

    def run(self) -> StepResult:
        issues: list[str] = []
        if not self._check_python():
            issues.append(f"python >= {self.REQUIRED_PYTHON} required")
        if not self._check_docker():
            issues.append("docker not found on PATH")
        if not self._check_port(10432):
            if self._is_postgres_listening(10432):
                # Port is in use because Postgres is already there — that's
                # the user's desired state. Not an issue; we just note it.
                return self._result(
                    True,
                    "preflight ok (postgres already running on :10432 — "
                    "will reuse existing container)",
                )
            issues.append(
                "port 10432 already in use by something that is NOT "
                "postgres — free the port or change HERMES_PG_PORT"
            )
        if issues:
            return self._result(False, "; ".join(issues))
        return self._result(True, "preflight ok")


# ---------------------------------------------------------------------------
# 5. DSN
# ---------------------------------------------------------------------------
class DsnStep(_BaseStep):
    """Writes HERMES_PG_CONN_STR to ~/.hermes/.env. Idempotent."""

    step_name = StepName.DSN

    def _format_env_line(self, dsn: str) -> str:
        return f'HERMES_PG_CONN_STR="{dsn}"\n'

    def run(self) -> StepResult:
        dsn = os.environ.get("HERMES_PG_CONN_STR", HERMES_PG_CONN_STR_DEFAULT)
        HERMES_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing = HERMES_ENV_PATH.read_text() if HERMES_ENV_PATH.exists() else ""
        if "HERMES_PG_CONN_STR=" in existing:
            return self._result(True, "DSN already in .env")
        with HERMES_ENV_PATH.open("a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(self._format_env_line(dsn))
        return self._result(True, f"wrote HERMES_PG_CONN_STR to {HERMES_ENV_PATH}")


# ---------------------------------------------------------------------------
# 6. Migrate — apply v2 SQL migrations to the profile DB.
# ---------------------------------------------------------------------------
def _migrations_dir() -> Path:
    """Path to the v2 SQL migration files. Ships inside the v2 package."""
    from importlib import resources

    return Path(str(resources.files("hermes_memory"))) / "migrations"


class MigrateStep(_BaseStep):
    """Apply v2 SQL migrations to the configured DSN.

    Fixes Bug 1 of the v2 live smoke test: the install used to clone
    hermes_template -> hermes_<profile> and stop, leaving the profile
    DB without the v2 schema (e.g. agent_memory.memory_chunks). Every
    long memory_remember failed with UndefinedTable.

    Also runs sync_sequences() afterwards to fix Bug 2: the
    bigserial sequences can lag behind the table's MAX(id) when
    prior installs backfilled rows with explicit IDs.

    Idempotent: re-runs are no-ops for already-applied versions.
    """

    step_name = StepName.MIGRATE

    def _resolve_dsn(self) -> str:
        """DSN to migrate. Prefer the DsnStep output, then env, then default."""
        dsn = os.environ.get("HERMES_PG_CONN_STR", "").strip()
        if not dsn:
            dsn = HERMES_PG_CONN_STR_DEFAULT
        return dsn

    def run(self) -> StepResult:
        # Imported here to keep module import-time lean, but also bound
        # as a module-level attribute below so tests can monkeypatch
        # ``hermes_memory.install.steps.psycopg.connect`` (the patch
        # path requires the module attribute to exist).

        from hermes_memory.migrate import apply_migrations, sync_sequences

        dsn = self._resolve_dsn()
        mig_dir = _migrations_dir()
        if not mig_dir.is_dir():
            return self._result(
                False,
                f"migrations dir not found: {mig_dir} (v2 install is missing SQL files)",
            )
        # Open the conn via psycopg using kwargs (libpq in the agent
        # venv can drop the dbname from a space-separated DSN, so
        # we never go through a DSN string here).
        # The DSN we have is in the form postgresql://user:pass@host:port/db
        # — parse it to kwargs so we sidestep that bug.
        kwargs = _parse_dsn_to_kwargs(dsn)
        try:
            with psycopg.connect(**kwargs) as conn:
                newly_applied = apply_migrations(conn, mig_dir)
                synced = sync_sequences(conn)
        except Exception as e:
            return self._result(
                False,
                f"migration apply failed: {type(e).__name__}: {e}",
            )
        n_new = len(newly_applied)
        n_synced = len(synced)
        if n_new == 0 and n_synced == 0:
            return self._result(True, "schema up to date, sequences in sync")
        return self._result(
            True,
            f"applied {n_new} new migration(s); synced {n_synced} sequence(s) "
            f"(latest: {newly_applied[-1] if newly_applied else 'none'})",
        )


def _parse_dsn_to_kwargs(dsn: str) -> dict[str, Any]:
    """Parse a postgresql:// DSN into psycopg connect() kwargs.

    Avoids the libpq quirk where dbname in a space-separated DSN
    can be silently dropped in some psycopg builds (observed in
    the agent venv, 2026-06-06)."""
    from urllib.parse import unquote, urlparse

    p = urlparse(dsn)
    if p.scheme not in ("postgresql", "postgres"):
        raise ValueError(f"unsupported DSN scheme: {p.scheme}")
    out: dict[str, Any] = {}
    if p.hostname:
        out["host"] = p.hostname
    if p.port:
        out["port"] = p.port
    if p.username:
        out["user"] = unquote(p.username)
    if p.password is not None:
        out["password"] = unquote(p.password)
    if p.path and p.path != "/":
        out["dbname"] = p.path.lstrip("/")
    return out


# ---------------------------------------------------------------------------
# 8. Register plugin
# ---------------------------------------------------------------------------
class RegisterPluginStep(_BaseStep):
    """Wires config.yaml: memory.provider=postgres, plugins.enabled += plugin,
    and removes the old mcp_servers.hermes-memory block."""

    step_name = StepName.REGISTER_PLUGIN

    def _load_config(self) -> dict[str, Any]:
        if not HERMES_CONFIG_PATH.exists():
            return {}
        with HERMES_CONFIG_PATH.open() as f:
            return yaml.safe_load(f) or {}

    def _save_config(self, cfg: dict[str, Any]) -> None:
        HERMES_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with HERMES_CONFIG_PATH.open("w") as f:
            yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)

    def run(self) -> StepResult:
        cfg = self._load_config()

        # 1) memory.provider = postgres
        mem = cfg.setdefault("memory", {})
        if mem.get("provider") != "postgres":
            mem["provider"] = "postgres"

        # 2) plugins.enabled += PLUGIN_NAME (idempotent)
        plugins = cfg.setdefault("plugins", {})
        enabled = plugins.setdefault("enabled", [])
        if PLUGIN_NAME not in enabled:
            enabled.append(PLUGIN_NAME)

        # 3) mcp_servers.hermes-memory block — remove (the v1 plugin is gone)
        mcp = cfg.get("mcp_servers", {})
        if "hermes-memory" in mcp:
            del mcp["hermes-memory"]

        self._save_config(cfg)

        # 4) Drop the v2 plugin files into ~/.hermes/plugins/<PLUGIN_NAME>/
        # This is what makes the plugin actually loadable. config.yaml alone
        # is not enough — the hermes-agent loader looks for plugin.yaml +
        # entry.py on disk. PLAN-V2 §1.2 calls this the "non-invasive
        # plug-in" promise.
        self._drop_plugin_assets()

        # 5) Drop the MemoryProvider shim at ~/.hermes/plugins/postgres/
        # so that `hermes memory status` reports the v2 plugin as installed.
        # Without this, the discovery scanner at
        # plugins/memory/__init__.py::_iter_provider_dirs() doesn't see
        # the v2 plugin as a memory provider (it scans $HERMES_HOME/plugins/
        # directly and applies the _is_memory_provider_dir heuristic to
        # each child, NOT $HERMES_HOME/plugins/memory/<name>/ as the
        # misleading comment in the discovery source suggests).
        self._drop_memory_shim()

        return self._result(
            True,
            f"registered {PLUGIN_NAME} in config.yaml; dropped plugin assets at {HERMES_PLUGINS_DIR}; dropped memory shim at {HERMES_MEMORY_SHIM_DIR}",
        )

    def _drop_plugin_assets(self) -> None:
        """Copy plugin.yaml + entry.py from the v2 package into the
        user's plugins directory. Idempotent — overwrites in place.

        Why a thin entry.py shim instead of an editable install + symlink?
        - editable installs / symlinks touch ~/.hermes/hermes-agent/'s
          working tree, which `hermes update` then flags as dirty
        - a shim file in the user-plugins dir is invisible to `hermes update`
          and survives a clean re-install
        """
        HERMES_PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
        for asset in ("plugin.yaml", "entry.py"):
            src = PACKAGE_DATA_ROOT / asset
            dst = HERMES_PLUGINS_DIR / asset
            if not src.is_file():
                # Don't silently skip — surface the bug loudly.
                raise FileNotFoundError(
                    f"v2 install asset missing: {src} "
                    f"(expected to be shipped inside the hermes_memory package)"
                )
            shutil.copy2(src, dst)

    def _drop_memory_shim(self) -> None:
        """Copy the MemoryProvider shim from the v2 package to
        $HERMES_HOME/plugins/postgres/. Idempotent.

        Why this is a separate method from _drop_plugin_assets:
        - The shim lives at $HERMES_HOME/plugins/postgres/ (not
          $HERMES_HOME/plugins/hermes-postgres-memory/) so that the
          discovery scanner at plugins/memory/__init__.py
          treats it as a sibling memory-provider directory
        - Discovery key fact: the scanner iterates $HERMES_HOME/plugins/
          directly and applies _is_memory_provider_dir to each child;
          the directory name is what gets matched against
          config.yaml's `memory.provider` (which is "postgres")
        - The shim is a no-op register(ctx) function plus a minimal
          _PostgresMemoryProvider class — see shim/postgres/__init__.py
        - The shim satisfies the discovery heuristic (contains the
          literal substring 'MemoryProvider' in the first 8KB) and the
          loading contract (defines a register(collector) function that
          calls collector.register_memory_provider(instance))
        """
        HERMES_MEMORY_SHIM_DIR.mkdir(parents=True, exist_ok=True)
        shim_src = PACKAGE_DATA_ROOT / "shim" / "postgres"
        for asset in ("__init__.py", "plugin.yaml"):
            src = shim_src / asset
            dst = HERMES_MEMORY_SHIM_DIR / asset
            if not src.is_file():
                # Don't silently skip — surface the bug loudly.
                raise FileNotFoundError(
                    f"v2 memory shim asset missing: {src} "
                    f"(expected to be shipped inside the hermes_memory package "
                    f"at shim/postgres/{asset})"
                )
            shutil.copy2(src, dst)
