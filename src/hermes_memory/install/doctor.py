"""`hermes-memory doctor` — self-diagnose and self-heal install state.

Surfaces install problems and (optionally) fixes them in place. Designed
to be run by users after an install and by CI as a smoke check.

The single most common issue this catches: the auto-redaction layer in
the Hermes agent runtime rewrites `~/.hermes/.env` on read-write cycles
(see hermes-redacted-agent skill, quirk 11a), leaving the DSN's password
slot as the literal 3-char marker. `doctor --heal` rewrites the DSN
using the real password from `~/.hermes/state/hermes-pg-*.password`.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

REDACTED = chr(42) * 3  # the literal 3-char marker; built programmatically


class Severity(str, Enum):
    OK = "OK"
    WARN = "WARN"
    FAIL = "FAIL"

    @property
    def rank(self) -> int:
        return {"OK": 0, "WARN": 1, "FAIL": 2}[self.value]


@dataclass
class Issue:
    """A single problem detected by the doctor."""

    code: str
    severity: Severity
    message: str
    fix_hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "fix_hint": self.fix_hint,
        }


@dataclass
class DoctorReport:
    """Aggregated doctor findings."""

    home: str
    issues: list[Issue] = field(default_factory=list)

    @property
    def severity(self) -> Severity:
        if not self.issues:
            return Severity.OK
        return max((i.severity for i in self.issues), key=lambda s: s.rank)

    def to_dict(self) -> dict[str, Any]:
        return {
            "home": self.home,
            "severity": self.severity.value,
            "issues": [i.to_dict() for i in self.issues],
        }

    def summary_line(self) -> str:
        """Single line for shell scripts: `hermes-memory doctor: OK`."""
        counts = {"OK": 0, "WARN": 0, "FAIL": 0}
        for i in self.issues:
            counts[i.severity.value] += 1
        return (
            f"hermes-memory doctor [{self.home}]: "
            f"{counts['FAIL']} fail, {counts['WARN']} warn, {counts['OK']} ok "
            f"— {self.severity.value}"
        )


def _pg_port_listening(host: str = "127.0.0.1", port: int = 10432, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _pg_reachable(dsn: str, timeout: float = 1.5) -> bool:
    if not dsn:
        return False
    if REDACTED in dsn:
        return False  # redacted DSN cannot authenticate
    try:
        import psycopg

        with psycopg.connect(dsn, connect_timeout=int(timeout)) as c:
            c.execute("SELECT 1").fetchone()
        return True
    except Exception:
        return False


def _read_env_file(home: Path) -> str | None:
    env = home / ".env"
    if not env.exists():
        return None
    try:
        return env.read_text()
    except Exception:
        return None


def _heal_env_file(home: Path, password_value: str) -> bool:
    """Rewrite the .env file with the real password substituted into the DSN.

    Returns True if any line was changed. Conservative: only edits lines
    matching `HERMES_PG_CONN_STR=...` or `PG_MEM_DB_CONN_STR=...` that
    contain the redaction marker in the password slot.
    """
    env = home / ".env"
    if not env.exists():
        return False
    from urllib.parse import quote

    safe = quote(password_value, safe="")
    changed = False
    new_lines: list[str] = []
    for line in env.read_text().splitlines():
        new_line = line
        for key in ("HERMES_PG_CONN_STR", "PG_MEM_DB_CONN_STR"):
            if line.strip().startswith(f"{key}=") and REDACTED in new_line:
                new_line = new_line.replace(REDACTED, safe, 1).replace(REDACTED, safe, 1)
        if new_line != line:
            changed = True
        new_lines.append(new_line)
    if changed:
        env.write_text("\n".join(new_lines) + "\n")
    return changed


def _find_password_file(home: Path) -> Path | None:
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


def _check_env_present(home: Path, report: DoctorReport) -> None:
    if (home / ".env").exists():
        return
    report.issues.append(
        Issue(
            code="ENV_MISSING",
            severity=Severity.WARN,
            message="~/.hermes/.env does not exist; install has not been run",
            fix_hint="run `hermes-memory install` to create it",
        )
    )


def _check_dsn_format(home: Path, report: DoctorReport) -> None:
    env_text = _read_env_file(home)
    if env_text is None:
        return
    for line in env_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key not in ("HERMES_PG_CONN_STR", "PG_MEM_DB_CONN_STR"):
            continue
        value = line.split("=", 1)[1].strip().strip('"').strip("'")
        if REDACTED in value:
            pwd_file = _find_password_file(home)
            if pwd_file is None:
                report.issues.append(
                    Issue(
                        code="DSN_REDACTED_NO_PWD_FILE",
                        severity=Severity.FAIL,
                        message=(
                            f"{key} has the redaction marker in the password slot and "
                            "no password file under ~/.hermes/state/ to recover from"
                        ),
                        fix_hint=("rotate the PG password, then re-run `hermes-memory install`"),
                    )
                )
            else:
                report.issues.append(
                    Issue(
                        code="DSN_REDACTED_HEALABLE",
                        severity=Severity.WARN,
                        message=(
                            f"{key} has the redaction marker in the password slot; "
                            f"can be healed using {pwd_file.name}"
                        ),
                        fix_hint="run `hermes-memory doctor --heal` to rewrite .env",
                    )
                )


def _check_pg_reachable(home: Path, report: DoctorReport) -> None:
    # Cheap check first: is the port open?
    if not _pg_port_listening():
        report.issues.append(
            Issue(
                code="PG_PORT_CLOSED",
                severity=Severity.WARN,
                message="PostgreSQL port 10432 is not open on 127.0.0.1",
                fix_hint=(
                    "start the postgres container: "
                    "`docker compose -f ~/infra/compose/postgres.yml up -d`"
                ),
            )
        )
        return
    # Try to connect with the healed DSN
    from hermes_memory.cli import _resolve_hermes_pg_dsn

    dsn = _resolve_hermes_pg_dsn(hermes_home=home)
    if dsn and not _pg_reachable(dsn):
        report.issues.append(
            Issue(
                code="PG_AUTH_FAILS",
                severity=Severity.FAIL,
                message="PostgreSQL is up but the DSN cannot authenticate",
                fix_hint=(
                    "run `hermes-memory doctor --heal`; if that fails, "
                    "rotate the password and re-run `hermes-memory install`"
                ),
            )
        )


def _check_embedder(home: Path, report: DoctorReport) -> None:
    """Is the configured embedder reachable? Reads the embedder URL from .env."""
    env_text = _read_env_file(home)
    if env_text is None:
        return
    url = ""
    for line in env_text.splitlines():
        if line.strip().startswith("EMBEDDER_URL="):
            url = line.split("=", 1)[1].strip().strip('"').strip("'")
            break
    if not url:
        return
    try:
        import urllib.request

        with urllib.request.urlopen(url.rstrip("/") + "/api/tags", timeout=1) as r:
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}")
    except Exception as e:
        report.issues.append(
            Issue(
                code="EMBEDDER_UNREACHABLE",
                severity=Severity.WARN,
                message=f"Embedder at {url} is not reachable: {type(e).__name__}",
                fix_hint=(
                    "verify the embedder container is up; check EMBEDDER_URL in ~/.hermes/.env"
                ),
            )
        )


def run_doctor(
    home: Path | None = None,
    *,
    heal: bool = False,
    pg_check: bool = True,
    embedder_check: bool = True,
) -> DoctorReport:
    """Run all doctor checks. Optionally heal known issues in place.

    Args:
        home: Override `~/.hermes` (default). Used by tests.
        heal: If True, attempt to fix detected issues (e.g. rewrite the
            .env DSN using the password file). Off by default; users
            must opt in.
        pg_check: If True (default), check PG reachability.
        embedder_check: If True (default), check embedder reachability.
    """
    if home is None:
        home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    report = DoctorReport(home=str(home))

    _check_env_present(home, report)
    _check_dsn_format(home, report)

    if heal:
        for issue in list(report.issues):
            if issue.code == "DSN_REDACTED_HEALABLE":
                pwd_file = _find_password_file(home)
                if pwd_file is not None and _heal_env_file(home, pwd_file.read_text().strip()):
                    issue.severity = Severity.OK
                    issue.message = "rewrote DSN in ~/.hermes/.env using the password file"
                    issue.code = "DSN_HEALED"
                    issue.fix_hint = ""

    if pg_check:
        _check_pg_reachable(home, report)
    if embedder_check:
        _check_embedder(home, report)

    return report
