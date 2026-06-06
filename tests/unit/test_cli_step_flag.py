"""TDD: cli --step N flag should run only that step (issue: bug #1).

The CLI parses `--step N` but currently ignores it — the full wizard
runs regardless. These tests pin the desired behavior:

  - `hermes-memory install --step 5` runs only step 5
  - `hermes-memory install --step dsn` accepts a name too
  - `hermes-memory install --step 0` runs only preflight
  - `hermes-memory install` (no flag) runs the full wizard
  - Invalid step index → clear error, exit 2
"""

from __future__ import annotations

import argparse

import pytest

from hermes_memory.cli import _run_install


def _make_args(step=None, yes=True):
    return argparse.Namespace(step=step, yes=yes)


def test_step_flag_runs_only_that_step(tmp_path, monkeypatch, capsys):
    """`--step 5` should run only step 5 (DSN), not the whole wizard."""
    # Isolate the state file so we don't pollute ~/.hermes
    fake_state = tmp_path / "state.json"
    monkeypatch.setattr("hermes_memory.cli.HERMES_STATE_PATH", fake_state)

    rc = _run_install(_make_args(step=5))
    out = capsys.readouterr().out
    assert rc == 0
    # Only DSN should have been reported; no preflight, no register.
    assert "dsn" in out
    assert "preflight" not in out
    assert "register_plugin" not in out
    assert "embedder" not in out


def test_step_flag_accepts_name(tmp_path, monkeypatch, capsys):
    """`--step dsn` should also work (name-based)."""
    fake_state = tmp_path / "state.json"
    monkeypatch.setattr("hermes_memory.cli.HERMES_STATE_PATH", fake_state)
    rc = _run_install(_make_args(step="dsn"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "dsn" in out
    assert "preflight" not in out


def test_no_step_flag_runs_full_wizard(tmp_path, monkeypatch, capsys):
    """Without --step, the full 8-step wizard runs in order."""
    fake_state = tmp_path / "state.json"
    monkeypatch.setattr("hermes_memory.cli.HERMES_STATE_PATH", fake_state)
    _run_install(_make_args(step=None))
    # Returns non-zero because preflight fails on port-in-use — that's
    # expected. The point is the wizard runs and preflight is tried.
    # We just confirm the output mentions preflight.
    out = capsys.readouterr().out
    assert "preflight" in out


def test_step_out_of_range_exits_2(tmp_path, monkeypatch, capsys):
    """An out-of-range step index produces a clear error and exit 2."""
    fake_state = tmp_path / "state.json"
    monkeypatch.setattr("hermes_memory.cli.HERMES_STATE_PATH", fake_state)
    with pytest.raises(SystemExit) as exc_info:
        _run_install(_make_args(step=99))
    assert exc_info.value.code == 2


def test_step_by_index_maps_to_correct_name(tmp_path, monkeypatch, capsys):
    """`--step 5` should map to StepName.DSN (5th in STEP_ORDER)."""
    fake_state = tmp_path / "state.json"
    monkeypatch.setattr("hermes_memory.cli.HERMES_STATE_PATH", fake_state)
    _run_install(_make_args(step=5))
    out = capsys.readouterr().out
    # Output should show "dsn:" not anything else.
    assert "dsn:" in out
