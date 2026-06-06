"""Install wizard — state and step orchestration.

The 8-step wizard records progress in a JSON state file so re-runs
are idempotent. The state file lives at
~/.hermes/state/hermes-memory.json by default.

This module is the orchestrator. Real step implementations (docker
calls, psql, etc.) live in `install/steps.py`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from hermes_memory.install.paths import (  # noqa: F401
    HERMES_CONFIG_PATH,
    HERMES_ENV_PATH,
    HERMES_HOME,
    HERMES_PG_CONN_STR_DEFAULT,
    HERMES_PLUGINS_DIR,
    HERMES_STATE_PATH,
    PLUGIN_NAME,
)


class StateError(Exception):
    """Raised when state operations fail (e.g. invalid step name)."""


class StepName(str, Enum):
    """The 8 wizard steps in execution order."""
    PREFLIGHT = "preflight"
    POSTGRES = "postgres"
    EXTENSIONS = "extensions"
    TEMPLATE = "template"
    PROFILE_DB = "profile_db"
    DSN = "dsn"
    EMBEDDER = "embedder"
    REGISTER_PLUGIN = "register_plugin"


STEP_ORDER: tuple[StepName, ...] = (
    StepName.PREFLIGHT,
    StepName.POSTGRES,
    StepName.EXTENSIONS,
    StepName.TEMPLATE,
    StepName.PROFILE_DB,
    StepName.DSN,
    StepName.EMBEDDER,
    StepName.REGISTER_PLUGIN,
)


# Backwards-compat alias for the pre-DSN rebrand: PROFILE_DB used to
# be "profiles" plural; the SQL file name is unchanged.
LEGACY_NAME_MAP = {
    "profiles": "profile_db",
    "migrate": "extensions",  # legacy migrate step was bundled with extensions
    "summary": "register_plugin",  # final summary folded into register
}


class WizardState:
    """JSON-backed state file. Thread-safe per-instance (file lock not implemented)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text())
            except json.JSONDecodeError:
                # Corrupt state — start fresh but back up the old one
                backup = self.path.with_suffix(".json.bak")
                self.path.rename(backup)
                self._data = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2))

    def is_empty(self) -> bool:
        return not self._data

    def is_done(self, step: StepName) -> bool:
        return self._data.get("steps", {}).get(step.value, {}).get("done", False)

    def mark_done(self, step: StepName, *, detail: dict[str, Any] | None = None) -> None:
        if not isinstance(step, StepName):
            raise StateError(f"invalid step: {step!r}")
        steps = self._data.setdefault("steps", {})
        if step.value in steps and steps[step.value].get("done"):
            # Idempotent: don't overwrite an existing entry
            return
        steps[step.value] = {"done": True, "detail": detail or {}}
        self._save()

    def get_detail(self, step: StepName) -> dict[str, Any]:
        return self._data.get("steps", {}).get(step.value, {}).get("detail", {})

    def completed_steps(self) -> list[StepName]:
        out = []
        for s in STEP_ORDER:
            if self.is_done(s):
                out.append(s)
        return out

    def clear(self) -> None:
        self._data = {}
        if self.path.exists():
            self.path.unlink()


# ---------------------------------------------------------------------------
# Wizard — orchestrator
# ---------------------------------------------------------------------------
@dataclass
class StepResult:
    step: StepName
    status: str  # "ran" | "skipped" | "failed"
    message: str

    @property
    def success(self) -> bool:
        return self.status in ("ran", "skipped")


# StepRunner is the contract for what each step's `run` does.
# In production it wraps docker / psql / file edits.
StepRunner = Callable[[StepName], StepResult]


class Wizard:
    """The install wizard orchestrator.

    Usage:
        state = WizardState(Path("~/.hermes/state/hermes-memory.json").expanduser())
        wizard = Wizard(state=state, runner=my_runner)
        results = wizard.run_pending()
    """

    def __init__(
        self,
        *,
        state: WizardState,
        runner: StepRunner,
        assume_yes: bool = False,
    ) -> None:
        self.state = state
        self.runner = runner
        self.assume_yes = assume_yes

    def run_pending(self) -> list[StepResult]:
        results: list[StepResult] = []
        for step in STEP_ORDER:
            if self.state.is_done(step):
                results.append(StepResult(step, "skipped", "already done"))
                continue
            result = self.runner(step)
            if result.success:
                self.state.mark_done(step, detail={"message": result.message})
            results.append(result)
            if not result.success:
                # Stop on first failure (fail-fast)
                break
        return results
