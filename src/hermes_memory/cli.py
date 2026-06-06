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
    HERMES_HOME,
    HERMES_STATE_PATH,
    STEP_ORDER,
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
    p_doctor = sub.add_parser(
        "doctor",
        help="Diagnose install state; --heal fixes redacted DSNs in place",
    )
    p_doctor.add_argument(
        "--heal",
        action="store_true",
        help="rewrite ~/.hermes/.env using the password file when the DSN is redacted",
    )
    p_doctor.add_argument(
        "--json", action="store_true", help="emit JSON instead of a human-readable report"
    )

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
        return _run_doctor(args)
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
def _resolve_step(step_arg) -> StepName:
    if isinstance(step_arg, int):
        if not 0 <= step_arg < len(STEP_ORDER):
            valid = ", ".join(f"{i}={s.value}" for i, s in enumerate(STEP_ORDER))
            import sys
            print(
                f"hermes-memory install: --step {step_arg} out of range; "
                f"valid: {valid}",
                file=sys.stderr,
            )
            sys.exit(2)
        return STEP_ORDER[step_arg]
    if isinstance(step_arg, str):
        for s in STEP_ORDER:
            if s.value == step_arg:
                return s
        # Legacy aliases
        from hermes_memory.install.state import LEGACY_NAME_MAP
        if step_arg in LEGACY_NAME_MAP:
            return StepName(LEGACY_NAME_MAP[step_arg])
        import sys
        valid = ", ".join(s.value for s in STEP_ORDER)
        print(
            f"hermes-memory install: unknown step {step_arg!r}; "
            f"valid: {valid}",
            file=sys.stderr,
        )
        sys.exit(2)
    raise TypeError(f"step must be int, str, or None; got {type(step_arg).__name__}")


# Build the redaction marker programmatically so the auto-redact layer doesn't
# mangle it in source files. The redactor (Hermes's `agent/redact.py`) replaces
# literal `***` substrings in tool output with `***`; using a runtime-built
# constant here keeps the production code immune to that transform.
_REDACTED_PASSWORD = chr(42) * 3  # the literal 3-char marker used in env files


def _find_password_file(hermes_home: Path | None = None) -> Path | None:
    """Locate a Postgres password file under ~/.hermes/state/.

    Picks the most recently modified matching file. Two naming conventions
    are supported (the v1 install produced `hermes-postgres.password`, the
    v2 install produces `hermes-pg-<profile>.password`):

      - `hermes-postgres.password`       (legacy)
      - `hermes-pg-<anything>.password`  (v2 per-profile)

    Returns None if none exist.
    """
    home = hermes_home or HERMES_HOME
    state = home / "state"
    if not state.is_dir():
        return None
    candidates: list[Path] = []
    legacy = state / "hermes-postgres.password"
    if legacy.exists():
        candidates.append(legacy)
    candidates.extend(state.glob("hermes-pg-*.password"))
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _heal_redacted_dsn(dsn: str, hermes_home: Path | None = None) -> str:
    """If a DSN has `***` in the password slot, substitute the real password.

    The redaction layer mangles passwords inside `~/.hermes/.env`. When that
    happens, a stored DSN like `postgresql://hermes:***@host/db` no longer
    authenticates. We recover by reading the real password from
    `~/.hermes/state/hermes-pg-*.password` and substituting it.

    URL-safe decoding: `urllib.parse.unquote` on the password slot, since
    some special characters are percent-encoded inside DSNs.
    """
    if _REDACTED_PASSWORD not in dsn:
        return dsn
    pwd_file = _find_password_file(hermes_home)
    if pwd_file is None or not pwd_file.exists():
        return dsn  # Can't heal; return as-is and let the probe fail honestly.
    try:
        real_password = pwd_file.read_text().strip()
    except Exception:
        return dsn
    if not real_password:
        return dsn
    # Replace the FIRST occurrence (password slot) and the path slot if both got masked.
    from urllib.parse import quote

    safe = quote(real_password, safe="")
    return dsn.replace(_REDACTED_PASSWORD, safe, 1).replace(
        _REDACTED_PASSWORD, safe, 1
    )


def _resolve_hermes_pg_dsn(hermes_home: Path | None = None) -> str:
    """Get a working HERMES_PG_CONN_STR, healing the `***` corruption if present.

    Resolution order:
      1. Explicit env var `HERMES_PG_CONN_STR` (with `***` healed).
      2. `PG_MEM_DB_CONN_STR` from env (legacy v1 Python plugin key).
      3. `HERMES_PG_CONN_STR` from `~/.hermes/.env` (with `***` healed).
    """
    _load_env_file(hermes_home=hermes_home)
    home = hermes_home or HERMES_HOME
    for key in ("HERMES_PG_CONN_STR", "PG_MEM_DB_CONN_STR"):
        value = os.environ.get(key, "")
        if value:
            return _heal_redacted_dsn(value, hermes_home=home)
    return ""


def _load_env_file(hermes_home: Path | None = None) -> None:
    """Read ~/.hermes/.env into os.environ if not already set.

    Idempotent. We don't override existing env vars — explicit env wins.

    Also handles the legacy `PG_MEM_DB_CONN_STR` key (from the v1
    Python plugin) as an alias for `HERMES_PG_CONN_STR`.
    """
    env_path = (hermes_home or HERMES_HOME) / ".env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)
    except Exception:
        pass
    # v1 → v2 alias: PG_MEM_DB_CONN_STR is the old Python plugin key.
    if (
        "HERMES_PG_CONN_STR" not in os.environ
        and "PG_MEM_DB_CONN_STR" in os.environ
    ):
        os.environ["HERMES_PG_CONN_STR"] = os.environ["PG_MEM_DB_CONN_STR"]
    # Heal redacted passwords in any DSN that just got loaded.
    for key in ("HERMES_PG_CONN_STR", "PG_MEM_DB_CONN_STR"):
        value = os.environ.get(key, "")
        if value and _REDACTED_PASSWORD in value:
            os.environ[key] = _heal_redacted_dsn(value, hermes_home=hermes_home)


def _pg_reachable(dsn: str | None = None) -> bool:
    """Is a Postgres server reachable on HERMES_PG_CONN_STR / localhost:10432?

    Used by the stub-step runners to detect "already configured" state
    before claiming success. Returns False on any error (no DSN, no
    psycopg, no server).

    Automatically heals a `***` redacted password in the DSN using the
    real password from `~/.hermes/state/hermes-pg-*.password`.
    """
    dsn = _resolve_hermes_pg_dsn() if dsn is None else _heal_redacted_dsn(dsn)
    if not dsn:
        # Fall back to the default port — same logic as PreflightStep.
        try:
            import socket
            with socket.create_connection(("127.0.0.1", 10432), timeout=1) as s:
                s.sendall(b"\x00\x00\x00\x08\x04\xd2\x16\x2f")
                s.settimeout(1)
                data = s.recv(1)
                return bool(data) and data in (b"S", b"N", b"E")
        except Exception:
            return False
    try:
        import psycopg
        with psycopg.connect(dsn, connect_timeout=2) as c:
            c.execute("SELECT 1").fetchone()
        return True
    except Exception:
        return False


def _hermes_template_exists(dsn: str | None = None) -> bool:
    """Does the `hermes_template` database exist on the target PG?"""
    _load_env_file()
    if dsn is None:
        dsn = os.environ.get("HERMES_PG_CONN_STR", "")
    if not dsn:
        return False
    try:
        import psycopg
        with psycopg.connect(dsn, connect_timeout=2) as c, c.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = 'hermes_template'")
            return cur.fetchone() is not None
    except Exception:
        return False


_REQUIRED_SCHEMAS = (
    "agent_memory", "hermes_wiki", "hermes_journal",
    "hermes_skills", "hermes_metrics", "hermes_kanban",
    "hermes_observability", "hermes_sessions",
)


def _hermes_template_has_all_schemas(dsn: str | None = None) -> bool:
    """Are all 8 hermes_* schemas present in `hermes_template`?"""
    _load_env_file()
    if dsn is None:
        dsn = os.environ.get("HERMES_PG_CONN_STR", "")
    if not dsn:
        return False
    # Switch to hermes_template by replacing the dbname.
    # Format: postgresql://u:p@host:port/dbname
    if "/" in dsn.rsplit("@", 1)[-1]:
        admin_dsn = dsn.rsplit("/", 1)[0] + "/hermes_template"
    else:
        return False
    try:
        import psycopg
        with psycopg.connect(admin_dsn, connect_timeout=2) as c, c.cursor() as cur:
            cur.execute(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name = ANY(%s)",
                (list(_REQUIRED_SCHEMAS),),
            )
            present = {r[0] for r in cur.fetchall()}
        return present == set(_REQUIRED_SCHEMAS)
    except Exception:
        return False


def _embedder_reachable() -> bool:
    """Is the bge-m3 embedder reachable at the configured URL?

    Defaults to the local Ollama address (10.49.0.52:11434 in the user's
    setup, but we also accept localhost). Probes with the OpenAI-compatible
    /embeddings endpoint.
    """
    url = os.environ.get(
        "HERMES_EMBED_URL", "http://10.49.0.52:11434/v1/embeddings"
    )
    try:
        import httpx
        r = httpx.post(
            url,
            json={"input": "ping", "model": "bge-m3"},
            timeout=3.0,
        )
        return r.status_code == 200
    except Exception:
        return False


_BOOTSTRAP_HINT = (
    "Run scripts/hermes-bootstrap.sh from the hermes-memory repo "
    "(it lives inside the Docker image; mount it via "
    "`docker compose -f compose/compose.yaml up -d` first). "
    "Track: https://github.com/skb50bd/hermes-memory/issues"
)


def _run_bash_delegated_step(step: StepName) -> StepResult:
    """Detect-already-configured runner for the 5 bash-delegated steps.

    The full implementation of POSTGRES, EXTENSIONS, TEMPLATE, PROFILE_DB,
    EMBEDDER lives in scripts/hermes-bootstrap.sh (a bash script that
    runs inside the Docker image). This Python wrapper checks whether
    the work each step would do is already complete, and reports
    truthfully:

      - If the prerequisite state is present → status="skipped" with
        a "already done" message. The wizard marks the step done and
        moves on.

      - If the work is needed but we don't have a Python impl here →
        status="failed" with a clear pointer to scripts/hermes-bootstrap.sh.

    This is the fix for bug 3: previously these steps returned
    status="ran" with a "not wired" message, which lied to the wizard
    and made `hermes-memory status` report the install as done.
    """
    if step == StepName.POSTGRES:
        if _pg_reachable():
            return StepResult(
                step, "skipped",
                "postgres already reachable — nothing to do",
            )
        return StepResult(step, "failed", f"postgres not reachable. {_BOOTSTRAP_HINT}")
    if step == StepName.EXTENSIONS:
        if not _pg_reachable():
            return StepResult(step, "failed", f"postgres not reachable. {_BOOTSTRAP_HINT}")
        if _hermes_template_exists() and _hermes_template_has_all_schemas():
            return StepResult(
                step, "skipped",
                "5 PG extensions + 8 schemas already applied to hermes_template",
            )
        return StepResult(step, "failed", f"schemas not all applied. {_BOOTSTRAP_HINT}")
    if step == StepName.TEMPLATE:
        if _hermes_template_exists():
            return StepResult(
                step, "skipped",
                "hermes_template database already exists",
            )
        return StepResult(
            step, "failed",
            f"hermes_template not found. {_BOOTSTRAP_HINT}",
        )
    if step == StepName.PROFILE_DB:
        _load_env_file()
        # Per-profile DBs are named hermes_<profile>; the user's profile
        # is in HERMES_PROFILE env or "default" if unset.
        profile = os.environ.get("HERMES_PROFILE", "default")
        target_db = f"hermes_{profile}"
        # If we can connect to the per-profile DB AND it has the 8
        # schemas, it's already set up.
        dsn = os.environ.get("HERMES_PG_CONN_STR", "")
        if dsn:
            try:
                profile_dsn = dsn.rsplit("/", 1)[0] + f"/{target_db}"
                import psycopg
                with psycopg.connect(profile_dsn, connect_timeout=2) as c, c.cursor() as cur:
                    cur.execute(
                        "SELECT schema_name FROM information_schema.schemata "
                        "WHERE schema_name = ANY(%s)",
                        (list(_REQUIRED_SCHEMAS),),
                    )
                    present = {r[0] for r in cur.fetchall()}
                if present == set(_REQUIRED_SCHEMAS):
                    return StepResult(
                        step, "skipped",
                        f"{target_db} already cloned from hermes_template",
                    )
            except Exception:
                pass
        return StepResult(
            step, "failed",
            f"{target_db} not set up. {_BOOTSTRAP_HINT}",
        )
    if step == StepName.EMBEDDER:
        if _embedder_reachable():
            return StepResult(
                step, "skipped",
                "bge-m3 embedder reachable — nothing to do",
            )
        return StepResult(
            step, "failed",
            "embedder not reachable. Pull bge-m3: "
            "`docker run -d -p 11434:11434 ollama/ollama && "
            "ollama pull bge-m3`",
        )
    return StepResult(step, "failed", f"unknown step: {step}")


def _run_install(args: argparse.Namespace) -> int:
    state = WizardState(HERMES_STATE_PATH)
    steps_impl = {
        StepName.PREFLIGHT: lambda: PreflightStep(state_dir=HERMES_STATE_PATH.parent).run(),
        StepName.DSN: lambda: DsnStep(state_dir=HERMES_STATE_PATH.parent).run(),
        StepName.REGISTER_PLUGIN: lambda: RegisterPluginStep(state_dir=HERMES_STATE_PATH.parent).run(),
    }

    def runner(step: StepName) -> StepResult:
        if step in steps_impl:
            return steps_impl[step]()
        # The 5 bash-delegated steps: detect "already configured" first.
        # If the prerequisite state is present, mark as skipped (truthful);
        # if the work is needed but we don't have a Python impl, mark
        # as failed with a clear pointer to scripts/hermes-bootstrap.sh.
        return _run_bash_delegated_step(step)

    # --step N (or --step NAME) means "run only this step".
    # Without --step, the full wizard runs in order.
    if args.step is not None:
        target = _resolve_step(args.step)
        result = runner(target)
        marker = "✓" if result.success else "✗"
        print(f"  {marker} {result.step.value}: {result.message}")
        return 0 if result.success else 1

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


def _run_doctor(args: argparse.Namespace) -> int:
    """Run install diagnostics. With --heal, also fix known issues in place.

    Returns 0 if the install is healthy (no FAIL-severity issues), 1
    if any FAIL-severity issue remains, 2 if a FAIL that the doctor
    could not auto-heal.
    """
    import json as _json

    from hermes_memory.install.doctor import run_doctor as _doctor

    report = _doctor(heal=bool(getattr(args, "heal", False)))
    if getattr(args, "json", False):
        print(_json.dumps(report.to_dict(), indent=2))
    else:
        if not report.issues:
            print(f"✓ {report.summary_line()}")
            return 0
        for issue in report.issues:
            marker = {
                "OK": "✓",
                "WARN": "!",
                "FAIL": "✗",
            }[issue.severity.value]
            print(f"  {marker} [{issue.severity.value}] {issue.code}: {issue.message}")
            if issue.fix_hint:
                print(f"      → {issue.fix_hint}")
        print()
        print(report.summary_line())
    sev = report.severity
    return 0 if sev == "OK" else (1 if sev == "WARN" else 2)


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
