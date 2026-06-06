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
  RegisterPluginStep  — wires config.yaml + plugins.enabled
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import yaml

from hermes_memory.install.paths import (
    HERMES_CONFIG_PATH,
    HERMES_ENV_PATH,
    HERMES_HOME,
    HERMES_PG_CONN_STR_DEFAULT,
    HERMES_PLUGINS_DIR,
    HERMES_STATE_PATH,
    PLUGIN_NAME,
)
from hermes_memory.install.state import StepName, StepResult

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
        return self._result(
            True,
            f"registered {PLUGIN_NAME} in config.yaml; removed old mcp_servers block",
        )
