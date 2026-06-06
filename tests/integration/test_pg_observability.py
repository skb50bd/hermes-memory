"""TDD: PgObservabilityRepo — logs, llm_calls, tool_calls."""

from __future__ import annotations

import pytest


@pytest.fixture
def obs_repo(pg_conn):
    from hermes_memory.pg_repos import PgObservabilityRepo
    return PgObservabilityRepo(pg_conn)


def test_log_inserts_row(obs_repo) -> None:
    # We can't easily query the row back (the base doesn't expose
    # _query_logs), but insert must not raise.
    obs_repo.log("INFO", "agent started", profile="default", fields={"v": 1})


def test_log_validates_level(obs_repo) -> None:
    with pytest.raises(ValueError):
        obs_repo.log("VERBOSE", "x", profile="default")  # type: ignore[arg-type]


def test_record_llm_call(obs_repo) -> None:
    obs_repo.record_llm_call(
        profile="default", model="gpt-4",
        prompt_tokens=100, completion_tokens=50, duration_ms=1200,
    )


def test_record_tool_call(obs_repo) -> None:
    obs_repo.record_tool_call(
        profile="default", tool="web_search",
        duration_ms=300, status="ok",
    )


def test_record_tool_call_with_error(obs_repo) -> None:
    obs_repo.record_tool_call(
        profile="default", tool="web_search",
        duration_ms=300, status="error", error="timeout",
    )


def test_flush_returns_count(obs_repo) -> None:
    # The base flush() always returns _flush() — for PG we just
    # commit any pending tx. Verify it doesn't blow up.
    n = obs_repo.flush()
    assert isinstance(n, int)


def test_close_is_idempotent(obs_repo) -> None:
    obs_repo.close()
    obs_repo.close()  # second call should be a no-op
