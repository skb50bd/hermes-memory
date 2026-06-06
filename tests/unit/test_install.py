"""TDD: install/ — 8-step idempotent wizard.

The wizard is the install orchestration. Each Step has:
  - name: short identifier (e.g. "00_preflight")
  - is_done(state) -> bool
  - run(ctx) -> StepResult
  - rollback(ctx) -> None   # for uninstall

The orchestrator (install/wizard.py) walks the steps in order,
skipping ones that are already done (for idempotency), prompting
for confirmations, and recording state in ~/.hermes/state/hermes-memory.json.

Public surface tested here:
  - Step enum (8 names)
  - WizardState (the JSON state file shape)
  - run_step() dispatch
  - check_done() / mark_done()
  - each step's is_done() and run() (smoke tests only — real work
    uses subprocess / docker / psql; we mock them in tests)
"""

from __future__ import annotations

import pytest

from hermes_memory.install.state import STEP_ORDER, StateError, StepName, WizardState
from hermes_memory.install.wizard import (
    StepResult,
    Wizard,
)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def test_state_init(tmp_path):
    s = WizardState(tmp_path / "state.json")
    assert s.is_empty()
    assert s.path == tmp_path / "state.json"


def test_state_mark_step_done(tmp_path):
    s = WizardState(tmp_path / "state.json")
    s.mark_done(StepName.PREFLIGHT)
    assert s.is_done(StepName.PREFLIGHT)
    assert not s.is_done(StepName.POSTGRES)
    # Reload
    s2 = WizardState(tmp_path / "state.json")
    assert s2.is_done(StepName.PREFLIGHT)


def test_state_mark_done_idempotent(tmp_path):
    s = WizardState(tmp_path / "state.json")
    s.mark_done(StepName.PREFLIGHT, detail={"docker": "/usr/bin/docker"})
    s.mark_done(StepName.PREFLIGHT, detail={"docker": "/different"})
    # First write wins (idempotency)
    assert s.get_detail(StepName.PREFLIGHT) == {"docker": "/usr/bin/docker"}


def test_state_invalid_step_raises(tmp_path):
    s = WizardState(tmp_path / "state.json")
    with pytest.raises(StateError):
        s.mark_done("not_a_step")  # type: ignore[arg-type]


def test_state_completed_steps_in_order(tmp_path):
    s = WizardState(tmp_path / "state.json")
    s.mark_done(StepName.PREFLIGHT)
    s.mark_done(StepName.POSTGRES)
    s.mark_done(StepName.EXTENSIONS)
    done = s.completed_steps()
    assert done == [StepName.PREFLIGHT, StepName.POSTGRES, StepName.EXTENSIONS]


# ---------------------------------------------------------------------------
# Step enum
# ---------------------------------------------------------------------------
def test_step_names_count():
    """Exactly 8 steps in the wizard (per the plan)."""
    assert len(STEP_ORDER) == 8


def test_step_order_is_8_named():
    expected_names = {
        "preflight", "postgres", "extensions", "template",
        "profile_db", "dsn", "embedder", "register_plugin",
    }
    actual = {s.value for s in STEP_ORDER}
    assert actual == expected_names


# ---------------------------------------------------------------------------
# Wizard — idempotency
# ---------------------------------------------------------------------------
def test_wizard_skips_done_steps(tmp_path):
    """The wizard is idempotent: re-running with state present skips
    completed steps without re-executing them."""
    s = WizardState(tmp_path / "state.json")
    s.mark_done(StepName.PREFLIGHT)
    s.mark_done(StepName.POSTGRES)
    # The runner is only called for steps that aren't already done.
    called_with: list[StepName] = []

    def runner(step):
        called_with.append(step)
        return StepResult(step, "ran", "ok")

    w = Wizard(state=s, runner=runner)
    results = w.run_pending()
    skipped = [r for r in results if r.status == "skipped"]
    run = [r for r in results if r.status == "ran"]
    assert len(skipped) == 2
    assert len(run) == 6  # the other 6
    # The 2 done steps were never re-run
    assert StepName.PREFLIGHT not in called_with
    assert StepName.POSTGRES not in called_with
    # The 6 not-done steps were each run
    assert len(called_with) == 6


def test_wizard_records_steps_after_run(tmp_path):
    s = WizardState(tmp_path / "state.json")
    ran = []
    def runner(step):
        ran.append(step)
        return StepResult(step, "ran", "ok")

    w = Wizard(state=s, runner=runner)
    w.run_pending()
    for step in STEP_ORDER:
        assert s.is_done(step), f"{step.value} not marked done after run"


# ---------------------------------------------------------------------------
# Preflight (Step 0) — checks the environment
# ---------------------------------------------------------------------------
def test_preflight_passes_when_essentials_present(tmp_path, monkeypatch):
    """Preflight checks python version, docker presence, port availability.
    Test the *contract*: given a fake env, it returns a result that
    can be inspected, and records state on success."""
    from hermes_memory.install.steps import PreflightStep

    step = PreflightStep(state_dir=tmp_path)
    monkeypatch.setattr(step, "_check_python", lambda: True)
    monkeypatch.setattr(step, "_check_docker", lambda: True)
    monkeypatch.setattr(step, "_check_port", lambda p: True)
    result = step.run()
    assert result.success is True


def test_preflight_fails_on_missing_docker(tmp_path, monkeypatch):
    from hermes_memory.install.steps import PreflightStep

    step = PreflightStep(state_dir=tmp_path)
    monkeypatch.setattr(step, "_check_python", lambda: True)
    monkeypatch.setattr(step, "_check_docker", lambda: False)
    # The new preflight probes the port for PG if 10432 is busy.
    # Set port-free so we get past that check and reach the docker failure.
    monkeypatch.setattr(step, "_check_port", lambda p: True)
    result = step.run()
    assert result.success is False
    assert "docker" in result.message.lower()


def test_preflight_port_in_use_with_pg_is_warning_not_failure(tmp_path, monkeypatch):
    """If the port is already serving Postgres, preflight should not
    fail — that's the user's desired state. It should pass with a
    note that the existing PG will be used.

    This is the bug 2 fix: previously `port 10432 already in use`
    was a hard fail, blocking real installs where the container
    is already running."""
    from hermes_memory.install.steps import PreflightStep

    step = PreflightStep(state_dir=tmp_path)
    monkeypatch.setattr(step, "_check_python", lambda: True)
    monkeypatch.setattr(step, "_check_docker", lambda: True)
    monkeypatch.setattr(step, "_check_port", lambda p: False)  # port IS in use
    monkeypatch.setattr(step, "_is_postgres_listening", lambda p: True)
    result = step.run()
    assert result.success is True
    assert "postgres" in result.message.lower()
    assert "already" in result.message.lower() or "existing" in result.message.lower()


def test_preflight_port_in_use_without_pg_is_failure(tmp_path, monkeypatch):
    """If the port is in use but it's not Postgres (e.g. some other
    service), preflight should still fail — something is squatting
    on our port and we can't proceed."""
    from hermes_memory.install.steps import PreflightStep

    step = PreflightStep(state_dir=tmp_path)
    monkeypatch.setattr(step, "_check_python", lambda: True)
    monkeypatch.setattr(step, "_check_docker", lambda: True)
    monkeypatch.setattr(step, "_check_port", lambda p: False)  # port in use
    monkeypatch.setattr(step, "_is_postgres_listening", lambda p: False)  # but not PG
    result = step.run()
    assert result.success is False


def test_is_postgres_listening_returns_bool():
    """The probe must return a bool (not raise) for any host:port."""
    from hermes_memory.install.steps import PreflightStep

    step = PreflightStep(state_dir="/tmp")
    # Definitely-not-postgres port: closed → False
    result = step._is_postgres_listening(54399)
    assert isinstance(result, bool)
    # An open HTTP port that's not PG: False
    # (skip; we don't want a hardcoded port that might bind something)


# ---------------------------------------------------------------------------
# DSN step (Step 5) — writes the connection string to ~/.hermes/.env
# ---------------------------------------------------------------------------
def test_dsn_step_writes_env(tmp_path, monkeypatch):
    """The DSN step appends HERMES_PG_CONN_STR to ~/.hermes/.env."""
    from hermes_memory.install.steps import DsnStep

    env_path = tmp_path / ".env"
    monkeypatch.setattr("hermes_memory.install.steps.HERMES_ENV_PATH", env_path)
    monkeypatch.setattr("hermes_memory.install.steps.HERMES_PG_CONN_STR_DEFAULT",
                        "postgresql://hermes:test@127.0.0.1:10432/hermes_default")
    step = DsnStep(state_dir=tmp_path)
    result = step.run()
    assert result.success is True
    assert "HERMES_PG_CONN_STR" in env_path.read_text()


def test_dsn_step_idempotent(tmp_path, monkeypatch):
    """Re-running doesn't duplicate the HERMES_PG_CONN_STR line."""
    from hermes_memory.install.steps import DsnStep

    env_path = tmp_path / ".env"
    monkeypatch.setattr("hermes_memory.install.steps.HERMES_ENV_PATH", env_path)
    monkeypatch.setattr("hermes_memory.install.steps.HERMES_PG_CONN_STR_DEFAULT",
                        "postgresql://hermes:test@127.0.0.1:10432/hermes_default")
    step = DsnStep(state_dir=tmp_path)
    step.run()
    step.run()
    content = env_path.read_text()
    assert content.count("HERMES_PG_CONN_STR=") == 1


# ---------------------------------------------------------------------------
# Register plugin step — writes to ~/.hermes/config.yaml
# ---------------------------------------------------------------------------
def test_register_plugin_sets_memory_provider(tmp_path, monkeypatch):
    """The register step adds 'hermes-postgres-memory' to
    plugins.enabled, sets memory.provider: postgres, and removes the
    old mcp_servers.hermes-memory block from config.yaml."""
    from hermes_memory.install.steps import RegisterPluginStep

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "memory:\n  provider: ''\n"
        "plugins:\n  enabled: []\n"
        "mcp_servers:\n  hermes-memory:\n    command: /old/csharp/binary\n"
    )
    monkeypatch.setattr("hermes_memory.install.steps.HERMES_CONFIG_PATH", config_path)
    step = RegisterPluginStep(state_dir=tmp_path)
    result = step.run()
    assert result.success is True
    text = config_path.read_text()
    assert "hermes-postgres-memory" in text
    assert "postgres" in text
    # Old MCP block is gone
    assert "/old/csharp/binary" not in text
