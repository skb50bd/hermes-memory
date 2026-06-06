"""TDD: every StepName in STEP_ORDER must be wired in _run_install.

The install CLI dispatches to either a Python step class (preflight,
dsn, migrate, register_plugin) or the bash-delegated detect-only
wrapper (postgres, extensions, template, profile_db, embedder). The
MIGRATE step is a Python step (MigrateStep in install/steps.py) and
must be present in the steps_impl dict — otherwise the wizard runs
all 8 other steps successfully, then hits MIGRATE, finds no Python
impl and no bash wrapper, and reports
``unknown step: StepName.MIGRATE`` with status="failed".

This bug was caught in production when re-running ``hermes-memory
install --yes`` against the fluffy profile (2026-06-06) — the v2
config had been written (memory.provider=postgres, plugins.enabled
has the v2 plugin, mcp_servers.hermes-memory block removed) but
``schema_migrations`` was missing from the per-profile DB. The
RegisterPluginStep had marked itself done, so the wizard thought
the install was complete, but migrations had never run.

Test: iterate STEP_ORDER and assert that for every step the runner
inside ``_run_install`` returns a non-``"failed"`` StepResult.

We do NOT need PG to test this — we mock every step class.
"""

from __future__ import annotations

from argparse import Namespace

from hermes_memory.cli import _run_install
from hermes_memory.install.state import STEP_ORDER, StepName
from hermes_memory.install.wizard import StepResult


def _build_args(yes: bool = True) -> Namespace:
    """Build a minimal argparse.Namespace that satisfies _run_install."""
    return Namespace(step=None, yes=yes)


def test_run_install_handles_every_step_in_step_order(monkeypatch, tmp_path):
    """For every StepName in STEP_ORDER, the runner must return a
    successful StepResult — never "unknown step" / "failed"."""

    # Force a fresh state file so the wizard runs every step.
    monkeypatch.setattr("hermes_memory.cli.HERMES_STATE_PATH", tmp_path / "state.json")

    # Stub every step class so we never touch docker / PG / filesystem.
    class _Stub:
        def __init__(self, *a, **kw):
            pass

        def run(self):
            return StepResult(StepName.PREFLIGHT, "ran", "stubbed")

    # Map the 4 Python-implemented steps to the stub.
    for cls_name in (
        "PreflightStep",
        "DsnStep",
        "MigrateStep",
        "RegisterPluginStep",
    ):
        monkeypatch.setattr(f"hermes_memory.cli.{cls_name}", _Stub)

    # The 5 bash-delegated steps (postgres, extensions, template,
    # profile_db, embedder) get handled by _run_bash_delegated_step.
    # Stub it to return success when work is already done.
    def _stub_bash(step):
        return StepResult(step, "ran", "stubbed")

    monkeypatch.setattr("hermes_memory.cli._run_bash_delegated_step", _stub_bash)

    # Run the full wizard.
    rc = _run_install(_build_args(yes=True))

    # If MIGRATE (or any other step) is unwired, _run_install prints
    # "✗ migrate: unknown step: StepName.MIGRATE" and returns 1.
    assert rc == 0, (
        f"_run_install returned {rc}; one of the {len(STEP_ORDER)} steps "
        f"in STEP_ORDER is not wired. STEP_ORDER = "
        f"{[s.value for s in STEP_ORDER]}"
    )


def test_run_install_wires_migrate_step_explicitly(monkeypatch, tmp_path):
    """The MIGRATE step is the one that was missed in v2.0.0 — verify
    it gets a real Python step class, not the bash-delegated
    "unknown step" path."""

    monkeypatch.setattr("hermes_memory.cli.HERMES_STATE_PATH", tmp_path / "state.json")

    called_with: list[str] = []

    class _Recorder:
        def __init__(self, *a, **kw):
            pass

        def run(self):
            called_with.append("python")
            return StepResult(StepName.MIGRATE, "ran", "ok")

    monkeypatch.setattr("hermes_memory.cli.MigrateStep", _Recorder)
    # Stub the other Python steps + the bash path so the wizard runs cleanly.
    for cls_name in ("PreflightStep", "DsnStep", "RegisterPluginStep"):
        monkeypatch.setattr(
            f"hermes_memory.cli.{cls_name}",
            type(
                cls_name,
                (),
                {
                    "__init__": lambda self, *a, **kw: None,
                    "run": lambda self: StepResult(StepName.PREFLIGHT, "ran", "ok"),
                },
            ),
        )
    monkeypatch.setattr(
        "hermes_memory.cli._run_bash_delegated_step",
        lambda step: StepResult(step, "ran", "ok"),
    )

    rc = _run_install(_build_args(yes=True))
    assert rc == 0
    assert "python" in called_with, (
        "MigrateStep was not called — the MIGRATE step is not wired "
        "in _run_install's steps_impl dict"
    )
