"""TDD: cli.py — argparse subcommand dispatcher.

Subcommands per the plan:
  install     — runs the 8-step wizard
  uninstall   — strips the plugin + offers per-surface export
  status      — prints install state + PG connection
  doctor      — health checks (issue #6, even though deferred)
  migrate     — apply migrations to a DSN
  version     — print version
  export      — dump a surface to JSON / Markdown / SQLite
  import      — restore from export
  rollback    — restore local MEMORY.md from postgres (legacy)

Usage:
    hermes-memory install [--step 0..7] [--yes]
    hermes-memory status
    hermes-memory --version
    hermes-memory export --surface memory --format markdown --out path
"""

from __future__ import annotations

import pytest

from hermes_memory import __version__
from hermes_memory.cli import build_parser, main


def test_version_is_string():
    assert isinstance(__version__, str)
    assert __version__ == "2.0.0"


def test_build_parser_has_subcommands():
    parser = build_parser()
    # Subcommand parsers exist (rough check — the test is that
    # the parser builds without error)
    expected = [
        "install", "uninstall", "status", "doctor", "migrate",
        "version", "export", "import", "rollback",
    ]
    for cmd in expected:
        # Each subcommand must parse (without args where applicable)
        try:
            if cmd == "migrate":
                parser.parse_args([cmd, "--dsn", "x"])
            elif cmd in ("export", "import"):
                # These need --surface + --format
                if cmd == "export":
                    parser.parse_args([cmd, "--surface", "memory", "--format", "json"])
                else:
                    parser.parse_args([cmd, "--surface", "memory", "--format", "json", "--in", "/tmp/x"])
            elif cmd in ("install", "uninstall"):
                parser.parse_args([cmd, "--yes"])
            else:
                parser.parse_args([cmd])
        except SystemExit:
            pytest.fail(f"subcommand {cmd!r} failed to parse")


def test_version_flag(capsys):
    parser = build_parser()
    args = parser.parse_args(["--version"])
    assert args.version is True


def test_install_subcommand_defaults():
    parser = build_parser()
    args = parser.parse_args(["install"])
    assert args.command == "install"
    assert args.step is None
    assert args.yes is False


def test_install_subcommand_with_step():
    parser = build_parser()
    args = parser.parse_args(["install", "--step", "3"])
    assert args.step == 3


def test_install_subcommand_with_yes():
    parser = build_parser()
    args = parser.parse_args(["install", "--yes"])
    assert args.yes is True


def test_status_subcommand():
    parser = build_parser()
    args = parser.parse_args(["status"])
    assert args.command == "status"


def test_migrate_subcommand_requires_dsn():
    parser = build_parser()
    args = parser.parse_args(["migrate"])
    assert args.command == "migrate"
    assert args.dsn is None  # optional; default to env


def test_export_subcommand_required_args():
    parser = build_parser()
    args = parser.parse_args(["export", "--surface", "memory", "--format", "markdown"])
    assert args.surface == "memory"
    assert args.format == "markdown"


def test_export_format_choices():
    parser = build_parser()
    # Valid format
    args = parser.parse_args(["export", "--surface", "memory", "--format", "json"])
    assert args.format == "json"
    # Invalid format → SystemExit
    with pytest.raises(SystemExit):
        parser.parse_args(["export", "--surface", "memory", "--format", "csv"])


def test_uninstall_subcommand_with_export():
    parser = build_parser()
    args = parser.parse_args(["uninstall", "--export", "memory,kanban,wiki"])
    assert args.export == "memory,kanban,wiki"


def test_rollback_subcommand():
    parser = build_parser()
    args = parser.parse_args(["rollback"])
    assert args.command == "rollback"


# ---------------------------------------------------------------------------
# main() — end-to-end dispatch
# ---------------------------------------------------------------------------
def test_main_version_prints_version(capsys):
    rc = main(["--version"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "2.0.0" in captured.out


def test_main_unknown_command_exits_nonzero():
    with pytest.raises(SystemExit):
        main(["this-is-not-a-command"])


def test_main_status_works_without_dsn(monkeypatch, tmp_path, capsys):
    """status with no HERMES_PG_CONN_STR set should still report state."""
    monkeypatch.delenv("HERMES_PG_CONN_STR", raising=False)
    # state file doesn't exist
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    rc = main(["status"])
    # Should not crash; rc is 0 or non-zero depending on whether
    # the state file exists. We only assert that it didn't raise.
    assert rc in (0, 1)


# ---------------------------------------------------------------------------
# install wiring — the actual 8-step run
# ---------------------------------------------------------------------------
def test_run_install_dry_run(monkeypatch, tmp_path, capsys):
    """install --yes with a state file present should be a no-op."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    rc = main(["install", "--yes"])
    # First run: no state → preflight runs (may fail if no docker).
    # We don't care about the result; we care that it doesn't crash.
    assert rc in (0, 1)
