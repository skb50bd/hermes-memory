"""TDD: 5 install steps (postgres, extensions, template, profile_db, embedder)
that delegate to bash should NOT pretend to succeed when the work is
not done. They should:

  - Return status="skipped" with a clear "already configured" message
    when the prerequisite state is detected (e.g. PG is running, the
    hermes_template DB exists, the 5 schemas are applied, the bge-m3
    embedder is reachable).
  - Return status="failed" with a clear "this build doesn't include
    the X step yet, run scripts/hermes-bootstrap.sh first" message
    when the prerequisite state is NOT detected.

Bug 3: currently these steps return status="ran" with a "not wired"
message, which lies to the wizard's idempotency tracking and makes
`hermes-memory status` report them as done.
"""

from __future__ import annotations

from unittest.mock import patch


def test_postgres_step_detects_running_pg(tmp_path, monkeypatch):
    """If PG is already running, postgres step should NOT say
    'not wired' — it should report already-configured."""
    import argparse

    from hermes_memory.cli import _run_install

    fake_state = tmp_path / "state.json"
    monkeypatch.setattr("hermes_memory.cli.HERMES_STATE_PATH", fake_state)
    with patch("hermes_memory.cli._pg_reachable", return_value=True):
        rc = _run_install(argparse.Namespace(step=1, yes=True))
    # rc=0 because skipped counts as success
    assert rc == 0


def test_postgres_step_without_pg_running_says_failed(capsys, tmp_path, monkeypatch):
    """If PG is NOT running, the postgres step should fail with a
    clear 'run docker compose up -d' message — not silently 'ran'."""
    import argparse

    from hermes_memory.cli import _run_install

    fake_state = tmp_path / "state.json"
    monkeypatch.setattr("hermes_memory.cli.HERMES_STATE_PATH", fake_state)
    with patch("hermes_memory.cli._pg_reachable", return_value=False):
        rc = _run_install(argparse.Namespace(step=1, yes=True))
    out = capsys.readouterr().out
    assert rc == 1
    assert "not wired" not in out.lower()  # bug 3 fix
    assert "bootstrap" in out.lower() or "docker" in out.lower() or "manual" in out.lower()


def test_postgres_step_with_pg_running_says_already_configured(capsys, tmp_path, monkeypatch):
    """The cleanest test: invoke `install --step 1` (POSTGRES) and
    verify the output does NOT contain the old "not wired" lie.
    If PG is reachable, the message should be "already running" or
    similar.
    """
    import argparse

    from hermes_memory.cli import _run_install

    fake_state = tmp_path / "state.json"
    monkeypatch.setattr("hermes_memory.cli.HERMES_STATE_PATH", fake_state)
    with patch("hermes_memory.cli._pg_reachable", return_value=True):
        rc = _run_install(argparse.Namespace(step=1, yes=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "not wired" not in out.lower(), (
        f"Step claimed 'not wired' even though PG is reachable: {out!r}"
    )
    assert "postgres" in out.lower()


def test_extensions_step_with_schemas_applied_says_skipped(capsys, tmp_path, monkeypatch):
    """If hermes_template already has the 5 schemas, extensions step
    should report 'already applied' not 'not wired'."""
    import argparse

    from hermes_memory.cli import _run_install

    fake_state = tmp_path / "state.json"
    monkeypatch.setattr("hermes_memory.cli.HERMES_STATE_PATH", fake_state)
    with (
        patch("hermes_memory.cli._pg_reachable", return_value=True),
        patch("hermes_memory.cli._hermes_template_exists", return_value=True),
        patch("hermes_memory.cli._hermes_template_has_all_schemas", return_value=True),
    ):
        rc = _run_install(argparse.Namespace(step=2, yes=True))
    out = capsys.readouterr().out
    assert rc == 0
    assert "not wired" not in out.lower()
    assert "already" in out.lower() or "applied" in out.lower()


def test_template_step_when_db_missing_says_run_bootstrap(capsys, tmp_path, monkeypatch):
    """If hermes_template doesn't exist, the template step should
    fail with a clear 'run scripts/hermes-bootstrap.sh' message,
    not silently claim success."""
    import argparse

    from hermes_memory.cli import _run_install

    fake_state = tmp_path / "state.json"
    monkeypatch.setattr("hermes_memory.cli.HERMES_STATE_PATH", fake_state)
    with (
        patch("hermes_memory.cli._pg_reachable", return_value=True),
        patch("hermes_memory.cli._hermes_template_exists", return_value=False),
    ):
        rc = _run_install(argparse.Namespace(step=3, yes=True))
    out = capsys.readouterr().out
    assert rc == 1, f"expected failure, got rc={rc} out={out!r}"
    assert "bootstrap" in out.lower() or "manual" in out.lower()
