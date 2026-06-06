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
from typing import Any

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
    "HERMES_HOME", "HERMES_ENV_PATH", "HERMES_CONFIG_PATH", "HERMES_STATE_PATH",
    "HERMES_PLUGINS_DIR", "HERMES_PG_CONN_STR_DEFAULT", "PLUGIN_NAME",
    "PreflightStep", "DsnStep", "RegisterPluginStep",
]


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------
@dataclass
class _BaseStep:
    state_dir: Path

    def run(self) -> StepResult:
        raise NotImplementedError

    def _result(self, success: bool, message: str) -> StepResult:
        # NB: the step's name is filled in by the runner, not here.
        return StepResult(step=StepName.PREFLIGHT, status="ran" if success else "failed", message=message)


# ---------------------------------------------------------------------------
# 1. Preflight
# ---------------------------------------------------------------------------
class PreflightStep(_BaseStep):
    """Checks python version, docker presence, port availability."""

    REQUIRED_PYTHON = (3, 11)

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

    def run(self) -> StepResult:
        issues: list[str] = []
        if not self._check_python():
            issues.append(f"python >= {self.REQUIRED_PYTHON} required")
        if not self._check_docker():
            issues.append("docker not found on PATH")
        if not self._check_port(10432):
            issues.append("port 10432 already in use")
        if issues:
            return self._result(False, "; ".join(issues))
        return self._result(True, "preflight ok")


# ---------------------------------------------------------------------------
# 5. DSN
# ---------------------------------------------------------------------------
class DsnStep(_BaseStep):
    """Writes HERMES_PG_CONN_STR to ~/.hermes/.env. Idempotent."""

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
