"""TDD: PgSessionsRepo — sessions, messages, compression locks."""

from __future__ import annotations

import threading

import pytest


@pytest.fixture
def sessions_repo(pg_conn):
    from hermes_memory.pg_repos import PgSessionsRepo
    return PgSessionsRepo(pg_conn)


def test_open_session_returns_id(sessions_repo) -> None:
    sid = sessions_repo.open_session("default")
    assert sid is not None


def test_append_and_get_messages(sessions_repo) -> None:
    sid = sessions_repo.open_session("default")
    sessions_repo.append_message(sid, "user", "hello")
    sessions_repo.append_message(sid, "assistant", "world")
    msgs = sessions_repo.get_messages(sid, limit=10)
    assert len(msgs) == 2
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert msgs[0].content == "hello"


def test_append_validates_role(sessions_repo) -> None:
    sid = sessions_repo.open_session("default")
    with pytest.raises(ValueError):
        sessions_repo.append_message(sid, "robot", "x")


def test_get_messages_respects_limit(sessions_repo) -> None:
    sid = sessions_repo.open_session("default")
    for i in range(5):
        sessions_repo.append_message(sid, "user", f"msg {i}")
    msgs = sessions_repo.get_messages(sid, limit=3)
    assert len(msgs) == 3


def test_close_session(sessions_repo) -> None:
    sid = sessions_repo.open_session("default")
    assert sessions_repo.close_session(sid) is True


def test_compression_lock_roundtrip(sessions_repo) -> None:
    sid = sessions_repo.open_session("default")
    assert sessions_repo.acquire_compression_lock(sid, "holder-a", ttl_seconds=60) is True
    # A different holder can't acquire while held
    assert sessions_repo.acquire_compression_lock(sid, "holder-b", ttl_seconds=60) is False
    # Original holder can release
    assert sessions_repo.release_compression_lock(sid, "holder-a") is True
    # Now another holder can acquire
    assert sessions_repo.acquire_compression_lock(sid, "holder-b", ttl_seconds=60) is True


def test_compression_lock_is_exclusive_under_concurrency(sessions_repo) -> None:
    """Two threads racing for the same session's lock — only one wins."""
    sid = sessions_repo.open_session("default")
    results: list = []
    lock = threading.Lock()

    def try_acquire(name):
        ok = sessions_repo.acquire_compression_lock(sid, name, ttl_seconds=60)
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=try_acquire, args=(f"h{i}",)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Exactly one True, four False
    assert sum(1 for r in results if r) == 1
    assert sum(1 for r in results if not r) == 4
