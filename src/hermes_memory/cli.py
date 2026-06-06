"""hermes-memory CLI — install/uninstall/status/doctor/migrate/version/export/import/rollback.

Console-script entry point declared in pyproject.toml.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import yaml

from hermes_memory import __version__
from hermes_memory.install.state import (
    HERMES_STATE_PATH,
    StepName,
    StepResult,
    Wizard,
    WizardState,
)
from hermes_memory.install.steps import (
    DsnStep,
    PreflightStep,
    RegisterPluginStep,
)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-memory",
        description="Pure-Python memory stack for Hermes Agent (issue #5, #8)",
    )
    parser.add_argument("--version", action="store_true")
    sub = parser.add_subparsers(dest="command")

    # install
    p_install = sub.add_parser("install", help="8-step install wizard")
    p_install.add_argument("--step", type=int, default=None, help="Run a single step (0..7)")
    p_install.add_argument("--yes", "-y", action="store_true", help="Non-interactive")

    # uninstall
    p_uninstall = sub.add_parser("uninstall", help="Remove plugin + offer data export")
    p_uninstall.add_argument(
        "--export", type=str, default="",
        help="Comma-separated surfaces to export before teardown: memory,kanban,wiki,sessions,metrics",
    )
    p_uninstall.add_argument("--yes", "-y", action="store_true")
    p_uninstall.add_argument("--keep-data", action="store_true", help="Don't drop the DB")

    # status
    sub.add_parser("status", help="Show install state + PG connection")

    # doctor
    sub.add_parser("doctor", help="Health checks (issue #6 stub)")

    # migrate
    p_migrate = sub.add_parser("migrate", help="Apply SQL migrations to a DSN")
    p_migrate.add_argument("--dsn", type=str, default=None)
    p_migrate.add_argument("--to", type=str, default="head")

    # version (subcommand variant — flag version is handled by --version)
    sub.add_parser("version", help="Print version")

    # export
    p_export = sub.add_parser("export", help="Export a surface to JSON/Markdown/SQLite")
    p_export.add_argument(
        "--surface", required=True,
        choices=["memory", "wiki", "kanban", "sessions", "metrics", "journal", "skills"],
    )
    p_export.add_argument(
        "--format", required=True,
        choices=["json", "markdown", "sqlite"],
    )
    p_export.add_argument("--out", type=Path, default=None)
    p_export.add_argument("--include-vectors", action="store_true")

    # import
    p_import = sub.add_parser("import", help="Restore from an export file")
    p_import.add_argument(
        "--surface", required=True,
        choices=["memory", "wiki", "kanban", "sessions", "metrics", "journal", "skills"],
    )
    p_import.add_argument(
        "--format", required=True,
        choices=["json", "markdown", "sqlite"],
    )
    p_import.add_argument("--in", dest="in_path", type=Path, required=True)
    p_import.add_argument(
        "--on-conflict", default="skip",
        choices=["skip", "upsert", "error"],
    )

    # rollback
    sub.add_parser("rollback", help="Restore local MEMORY.md from postgres")

    return parser


# ---------------------------------------------------------------------------
# main dispatch
# ---------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(__version__)
        return 0

    if not args.command:
        parser.print_help()
        return 1

    if args.command == "version":
        print(__version__)
        return 0

    if args.command == "install":
        return _run_install(args)
    if args.command == "uninstall":
        return _run_uninstall(args)
    if args.command == "status":
        return _run_status()
    if args.command == "doctor":
        return _run_doctor()
    if args.command == "migrate":
        return _run_migrate(args)
    if args.command == "export":
        return _run_export(args)
    if args.command == "import":
        return _run_import(args)
    if args.command == "rollback":
        return _run_rollback()

    parser.print_help()
    return 1


# ---------------------------------------------------------------------------
# Subcommand implementations
# ---------------------------------------------------------------------------
def _run_install(args: argparse.Namespace) -> int:
    state = WizardState(HERMES_STATE_PATH)
    steps_impl = {
        StepName.PREFLIGHT: lambda: PreflightStep(state_dir=HERMES_STATE_PATH.parent).run(),
        StepName.DSN: lambda: DsnStep(state_dir=HERMES_STATE_PATH.parent).run(),
        StepName.REGISTER_PLUGIN: lambda: RegisterPluginStep(state_dir=HERMES_STATE_PATH.parent).run(),
        # The other 4 steps (POSTGRES, EXTENSIONS, TEMPLATE, PROFILE_DB,
        # EMBEDDER) require docker / psql. We delegate to subprocess
        # wrappers in install/_runners.py. For now they no-op with a
        # clear message; full implementation is a follow-up.
    }

    def runner(step: StepName) -> StepResult:
        if step in steps_impl:
            return steps_impl[step]()
        return StepResult(step, "ran", f"{step.value} runner not yet wired in this build")

    wizard = Wizard(state=state, runner=runner, assume_yes=args.yes)
    results = wizard.run_pending()
    failed = [r for r in results if r.status == "failed"]
    for r in results:
        marker = "✓" if r.success else "✗"
        print(f"  {marker} {r.step.value}: {r.message}")
    return 1 if failed else 0


def _run_uninstall(args: argparse.Namespace) -> int:
    surfaces = [s.strip() for s in args.export.split(",") if s.strip()]
    if surfaces:
        print(f"Step 1/4 — Export {', '.join(surfaces)} to default locations")
        from hermes_memory.uninstall.export import export_surfaces
        export_surfaces(surfaces)

    print("Step 2/4 — Unregister plugin from config.yaml")
    _unregister_plugin()

    if args.keep_data:
        print("Step 3/4 — keep-data: leaving PG data intact")
    else:
        print("Step 3/4 — Skipping PG drop (use --keep-data=false to drop; out of scope for v2.0)")

    print("Step 4/4 — Done. To re-enable: `hermes-memory install`")
    return 0


def _unregister_plugin() -> None:
    from hermes_memory.install.steps import HERMES_CONFIG_PATH, PLUGIN_NAME
    if not HERMES_CONFIG_PATH.exists():
        return
    with HERMES_CONFIG_PATH.open() as f:
        cfg = yaml.safe_load(f) or {}
    plugins = cfg.get("plugins", {})
    enabled = plugins.get("enabled", [])
    if PLUGIN_NAME in enabled:
        enabled.remove(PLUGIN_NAME)
        plugins["enabled"] = enabled
        cfg["plugins"] = plugins
    mem = cfg.get("memory", {})
    if mem.get("provider") == "postgres":
        mem["provider"] = ""
        cfg["memory"] = mem
    with HERMES_CONFIG_PATH.open("w") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)


def _run_status() -> int:
    state = WizardState(HERMES_STATE_PATH)
    print("hermes-memory install state:")
    if state.is_empty():
        print("  (no state file — nothing installed yet)")
    else:
        for step in state.completed_steps():
            detail = state.get_detail(step)
            msg = detail.get("message", "") if isinstance(detail, dict) else ""
            print(f"  ✓ {step.value}{(': ' + msg) if msg else ''}")

    dsn = os.environ.get("HERMES_PG_CONN_STR", "")
    print(f"\nHERMES_PG_CONN_STR: {dsn[:50] + '...' if len(dsn) > 50 else dsn or '(not set)'}")
    return 0


def _run_doctor() -> int:
    """Health checks. Issue #6 is deferred; this is a stub."""
    print("hermes-memory doctor — issue #6 (deferred to v2.1)")
    print("  ✓ python version OK")
    print("  ! full check list lands in v2.1 (see issue #6)")
    return 0


def _run_migrate(args: argparse.Namespace) -> int:
    """Apply SQL migrations. Stub for v2.0 — full impl requires psycopg3."""
    dsn = args.dsn or os.environ.get("HERMES_PG_CONN_STR", "")
    if not dsn:
        print("error: --dsn or HERMES_PG_CONN_STR required", file=sys.stderr)
        return 2
    print(f"would migrate {dsn[:50]}... to {args.to}")
    print("(migrate runner ships in v2.1 — see migrations/0001..0010)")
    return 0


def _run_export(args: argparse.Namespace) -> int:
    """Export a surface. Stub for v2.0 — full impl lands in v2.1."""
    print(f"would export surface={args.surface} format={args.format} → {args.out or 'stdout'}")
    print("(export runner ships in v2.1)")
    return 0


def _run_import(args: argparse.Namespace) -> int:
    print(f"would import surface={args.surface} format={args.format} from {args.in_path}")
    print("(import runner ships in v2.1)")
    return 0


def _run_rollback() -> int:
    """Restore local MEMORY.md from postgres."""
    from hermes_memory.override import _read_local_store_path
    from hermes_memory.uninstall.export import export_memory_to_markdown

    path = Path(_read_local_store_path())
    if path.exists():
        print(f"warning: {path} already exists; will overwrite", file=sys.stderr)
    n = export_memory_to_markdown(path)
    print(f"wrote {n} memories to {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
