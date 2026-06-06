"""TDD: corrupted `.env` passwords (literal `***` from redaction) must self-heal.

The redaction layer in this agent's runtime mangles the literal `***`
substring inside Python source files written via `write_file` (see
hermes-redacted-agent skill, quirk 17). So we cannot bake `***` into
the test file directly. Instead we build the marker programmatically
(using `chr(42) * 3`) and use a clearly-non-credential-shaped sentinel
for the substitute password (`SUBPWD`).

We assert structural properties (DSN parses, user/host/port/db preserved,
redaction marker absent) instead of literal credential comparison.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_memory.cli import _load_env_file, _resolve_hermes_pg_dsn

# Build the redaction marker programmatically so the write-time redactor
# doesn't corrupt this test source. `chr(42) * 3` is the literal 3-char
# marker that ends up in ~/.hermes/.env after a redaction cycle.
REDACTED = chr(42) * 3
# A clearly non-credential-shaped substitute password.
SUBPWD = "SUBPWD"


def _write_env(
    env_path: Path,
    conn_str: str,
    password_file: Path | None = None,
    password_value: str | None = None,
    extra_lines: list[str] | None = None,
) -> None:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f'HERMES_PG_CONN_STR="{conn_str}"']
    if extra_lines:
        lines.extend(extra_lines)
    env_path.write_text("\n".join(lines) + "\n")
    if password_file is not None and password_value is not None:
        password_file.parent.mkdir(parents=True, exist_ok=True)
        password_file.write_text(password_value + "\n")


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("HERMES_PG_CONN_STR", "PG_MEM_DB_CONN_STR", "HERMES_HOME"):
        monkeypatch.delenv(k, raising=False)


def _dsn_user_host_db(dsn: str) -> tuple[str, str, int, str] | None:
    from urllib.parse import urlparse

    p = urlparse(dsn)
    if not p.hostname:
        return None
    return (p.username or "", p.hostname, p.port or 0, (p.path or "").lstrip("/"))


def test_resolve_dsn_heals_redacted_password(tmp_path: Path, clean_env: None) -> None:
    """DSN with `***` in the password slot is rewritten to use the real password."""
    env = tmp_path / ".env"
    pwd = tmp_path / "state" / "hermes-postgres.password"
    _write_env(
        env,
        conn_str=f"postgresql://hermes:{REDACTED}@127.0.0.1:10432/hermes_default",
        password_file=pwd,
        password_value=SUBPWD,
    )

    with patch("hermes_memory.cli.HERMES_HOME", tmp_path):
        dsn = _resolve_hermes_pg_dsn()

    assert REDACTED not in dsn, "redaction marker still present in DSN"
    parsed = _dsn_user_host_db(dsn)
    assert parsed is not None
    user, host, port, db = parsed
    assert user == "hermes"
    assert host == "127.0.0.1"
    assert port == 10432
    assert db == "hermes_default"
    assert SUBPWD in dsn


def test_resolve_dsn_unchanged_when_password_real(tmp_path: Path, clean_env: None) -> None:
    """DSN with a real password passes through unchanged."""
    real = f"postgresql://hermes:{SUBPWD}@127.0.0.1:10432/hermes_default"
    env = tmp_path / ".env"
    _write_env(
        env,
        conn_str=real,
        password_file=tmp_path / "state" / "x.password",
        password_value="placeholder",
    )

    with patch("hermes_memory.cli.HERMES_HOME", tmp_path):
        dsn = _resolve_hermes_pg_dsn()

    assert dsn == real
    assert SUBPWD in dsn


def test_resolve_dsn_prefers_explicit_env(
    tmp_path: Path, clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If HERMES_PG_CONN_STR is set in the real env, use it directly (no healing)."""
    explicit = f"postgresql://hermes:{SUBPWD}@127.0.0.1:10432/hermes_default"
    monkeypatch.setenv("HERMES_PG_CONN_STR", explicit)
    env = tmp_path / ".env"
    _write_env(
        env,
        conn_str=f"postgresql://hermes:{REDACTED}@127.0.0.1:10432/hermes_default",
    )

    with patch("hermes_memory.cli.HERMES_HOME", tmp_path):
        dsn = _resolve_hermes_pg_dsn()

    assert dsn == explicit


def test_resolve_dsn_returns_empty_when_nothing_available(
    tmp_path: Path, clean_env: None
) -> None:
    """No env, no .env, no password file returns empty string."""
    with patch("hermes_memory.cli.HERMES_HOME", tmp_path):
        dsn = _resolve_hermes_pg_dsn()

    assert dsn == ""


def test_heal_uses_newest_matching_password_file(
    tmp_path: Path, clean_env: None
) -> None:
    """If multiple state/*.password files exist, pick the most recently modified."""
    import os as _os

    env = tmp_path / ".env"
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    old = state / "hermes-pg-old.password"
    new = state / "hermes-pg-newest.password"
    old.write_text("OLDVALUE\n")
    new.write_text("NEWVALUE\n")
    _os.utime(old, (1_000_000, 1_000_000))
    _os.utime(new, (2_000_000, 2_000_000))
    _write_env(
        env,
        conn_str=f"postgresql://hermes:{REDACTED}@127.0.0.1:10432/hermes_default",
    )

    with patch("hermes_memory.cli.HERMES_HOME", tmp_path):
        dsn = _resolve_hermes_pg_dsn()

    assert "NEWVALUE" in dsn
    assert "OLDVALUE" not in dsn


def test_heal_preserves_special_characters_in_password(
    tmp_path: Path, clean_env: None
) -> None:
    """Passwords with @, /, :, # are URL-encoded in the healed DSN."""
    import urllib.parse

    raw = "p@s/s:or#d1"
    expected_encoded = urllib.parse.quote(raw, safe="")
    env = tmp_path / ".env"
    pwd = tmp_path / "state" / "hermes-postgres.password"
    _write_env(
        env,
        conn_str=f"postgresql://hermes:{REDACTED}@127.0.0.1:10432/hermes_default",
        password_file=pwd,
        password_value=raw,
    )

    with patch("hermes_memory.cli.HERMES_HOME", tmp_path):
        dsn = _resolve_hermes_pg_dsn()

    assert expected_encoded in dsn
    # Raw unencoded `@` in the password would break DSN parsing.
    assert "hermes:p@s" not in dsn


def test_resolve_dsn_falls_back_to_legacy_pg_mem_db(
    tmp_path: Path, clean_env: None
) -> None:
    """v1 Python plugin used PG_MEM_DB_CONN_STR; v2 accepts it as alias."""
    env = tmp_path / ".env"
    pwd = tmp_path / "state" / "hermes-postgres.password"
    _write_env(
        env,
        conn_str=f"postgresql://hermes:{REDACTED}@127.0.0.1:10432/hermes_default",
        password_file=pwd,
        password_value=SUBPWD,
        extra_lines=[
            f'PG_MEM_DB_CONN_STR="postgresql://hermes:{REDACTED}@127.0.0.1:10432/hermes_default"',
        ],
    )
    os.environ.pop("HERMES_PG_CONN_STR", None)
    with patch("hermes_memory.cli.HERMES_HOME", tmp_path):
        dsn = _resolve_hermes_pg_dsn()

    assert REDACTED not in dsn
    assert SUBPWD in dsn


def test_load_env_file_substitutes_redacted_password_at_load_time(
    tmp_path: Path, clean_env: None
) -> None:
    """Loading the env file heals `***` so downstream code sees a working DSN."""
    env = tmp_path / ".env"
    pwd = tmp_path / "state" / "hermes-postgres.password"
    _write_env(
        env,
        conn_str=f"postgresql://hermes:{REDACTED}@127.0.0.1:10432/hermes_default",
        password_file=pwd,
        password_value=SUBPWD,
    )

    with patch("hermes_memory.cli.HERMES_HOME", tmp_path):
        _load_env_file()

    loaded = os.environ.get("HERMES_PG_CONN_STR", "")
    assert SUBPWD in loaded
    assert REDACTED not in loaded
