"""TDD: `hermes-memory doctor` — self-diagnose and self-heal install state.

The doctor is a user-facing command that surfaces install problems
(especially the `***` redacted-password issue discovered during the
2026-06-06 live migration) and fixes them in place.

It returns a structured report the user can read, and a summary line
that ends in OK / WARN / FAIL — scriptable from CI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_memory.install.doctor import DoctorReport, Severity, run_doctor

# -- helpers --

def _write(env: Path, line: str) -> None:
    env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text(line + "\n")


def _pwd_file(home: Path, name: str, value: str) -> Path:
    p = home / "state" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(value + "\n")
    return p


# -- tests --


def test_doctor_reports_clean_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Healthy install: no issues, status OK.

    A "clean" DSN has a real password (not the redaction marker), which
    is the post-heal state. We can't write a literal real-looking
    password in the test source (the redaction layer would mangle it),
    so we write a marker string and then patch `_heal_redacted_dsn` /
    `_find_password_file` to recognise it as a "real" password.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    real_password = "REALSUBPWD"  # not the redaction marker
    _write(
        tmp_path / ".env",
        'HERMES_PG_CONN_STR="postgresql://hermes:***@127.0.0.1:10432/hermes_default"'.replace(
            chr(42) * 3, real_password
        ),
    )
    _pwd_file(tmp_path, "hermes-postgres.password", real_password)

    report = run_doctor(home=tmp_path, pg_check=False, embedder_check=False)
    assert isinstance(report, DoctorReport)
    assert report.severity == Severity.OK, f"unexpected issues: {report.issues}"
    assert all(i.severity != Severity.FAIL for i in report.issues)


def test_doctor_detects_redacted_dsn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the DSN has `***` in the password slot, doctor flags it (WARN or FAIL)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write(
        tmp_path / ".env",
        'HERMES_PG_CONN_STR="postgresql://hermes:***@127.0.0.1:10432/hermes_default"',
    )
    # No password file → FAIL
    report = run_doctor(home=tmp_path, pg_check=False, embedder_check=False)
    assert any(
        i.code in ("DSN_REDACTED_NO_PWD_FILE", "DSN_REDACTED_HEALABLE")
        for i in report.issues
    ), f"expected redacted-DSN issue, got: {report.issues}"


def test_doctor_self_heals_redacted_dsn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`run_doctor(..., heal=True)` rewrites the DSN on disk using the password file."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write(
        tmp_path / ".env",
        'HERMES_PG_CONN_STR="postgresql://hermes:***@127.0.0.1:10432/hermes_default"',
    )
    _pwd_file(tmp_path, "hermes-postgres.password", "HEALEDPWD")

    report = run_doctor(home=tmp_path, heal=True, pg_check=False, embedder_check=False)
    # The on-disk .env must now have HEALEDPWD in it.
    env_text = (tmp_path / ".env").read_text()
    assert "HEALEDPWD" in env_text
    # The heal issue should be marked OK in the report.
    healed = [i for i in report.issues if i.code == "DSN_HEALED"]
    assert healed, f"expected DSN_HEALED issue, got: {report.issues}"


def test_doctor_reports_missing_password_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DSN with `***` and no password file: FAIL severity."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write(
        tmp_path / ".env",
        'HERMES_PG_CONN_STR="postgresql://hermes:***@127.0.0.1:10432/hermes_default"',
    )

    report = run_doctor(home=tmp_path)
    fail_issues = [i for i in report.issues if i.severity == Severity.FAIL]
    assert fail_issues, f"expected FAIL severity issue, got: {report.issues}"


def test_doctor_detects_missing_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ~/.hermes/.env at all: WARN severity (install never ran)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    report = run_doctor(home=tmp_path)
    warn_issues = [i for i in report.issues if i.severity == Severity.WARN]
    assert any("env" in i.message.lower() or "install" in i.message.lower() for i in warn_issues)


def test_doctor_json_output_is_serializable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DoctorReport can be JSON-serialized for machine consumption."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write(
        tmp_path / ".env",
        'HERMES_PG_CONN_STR="postgresql://hermes:***@127.0.0.1:10432/hermes_default"',
    )
    _pwd_file(tmp_path, "hermes-postgres.password", "PW123")

    report = run_doctor(home=tmp_path)
    payload = report.to_dict()
    blob = json.dumps(payload)
    roundtrip = json.loads(blob)
    assert roundtrip["severity"] in ("OK", "WARN", "FAIL")
    assert isinstance(roundtrip["issues"], list)


def test_doctor_summary_line_ends_with_severity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The terminal summary ends with OK / WARN / FAIL — scriptable."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write(
        tmp_path / ".env",
        'HERMES_PG_CONN_STR="postgresql://hermes:***@127.0.0.1:10432/hermes_default"',
    )
    _pwd_file(tmp_path, "hermes-postgres.password", "PW123")

    report = run_doctor(home=tmp_path)
    summary = report.summary_line()
    assert summary.strip().endswith(("OK", "WARN", "FAIL"))
