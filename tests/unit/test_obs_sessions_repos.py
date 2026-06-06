"""TDD: observability_repo + sessions_repo."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from hermes_memory.repos.observability_repo import (
    LLMCall,
    LogEvent,
    ObservabilityRepo,
    ToolCall,
    redact_dict,
)
from hermes_memory.repos.sessions_repo import SessionMessage, SessionsRepo


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------
class FakeObsRepo(ObservabilityRepo):
    def __init__(self):
        self.logs: list[LogEvent] = []
        self.llm_calls: list[LLMCall] = []
        self.tool_calls: list[ToolCall] = []

    def _insert_log(self, event):
        self.logs.append(event)
        return len(self.logs)

    def _insert_llm_call(self, call):
        self.llm_calls.append(call)
        return len(self.llm_calls)

    def _insert_tool_call(self, call):
        self.tool_calls.append(call)
        return len(self.tool_calls)

    def _flush(self):
        return 0

    def _close(self):
        pass


def test_obs_log_records_event():
    o = FakeObsRepo()
    o.log("INFO", "started", profile="default", fields={"x": 1})
    assert len(o.logs) == 1
    assert o.logs[0].level == "INFO"


def test_obs_invalid_level_raises():
    o = FakeObsRepo()
    with pytest.raises(ValueError, match="level"):
        o.log("VERBOSE", "x")  # type: ignore[arg-type]


def test_obs_record_llm_call():
    o = FakeObsRepo()
    o.record_llm_call("default", "gpt-4", 100, 200, 1500)
    assert o.llm_calls[0].prompt_tokens == 100


def test_obs_record_tool_call_with_error():
    o = FakeObsRepo()
    o.record_tool_call("default", "search", 250, status="error", error="timeout")
    assert o.tool_calls[0].error == "timeout"


def test_obs_invalid_status_raises():
    o = FakeObsRepo()
    with pytest.raises(ValueError, match="status"):
        o.record_llm_call("d", "m", 1, 1, 1, status="weird")  # type: ignore[arg-type]


def test_redact_dict_strips_secrets():
    d = {"user": "shakib", "password": "abc", "nested": {"api_key": "xyz"}}
    out = redact_dict(d)
    assert out["user"] == "shakib"
    assert out["password"] == "***"
    assert out["nested"]["api_key"] == "***"


def test_redact_dict_handles_lists():
    d = {"items": [{"token": "abc"}, {"safe": 1}]}
    out = redact_dict(d)
    assert out["items"][0]["token"] == "***"
    assert out["items"][1]["safe"] == 1


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
class FakeSessionsRepo(SessionsRepo):
    def __init__(self):
        self._sessions: dict[int, str] = {}
        self._messages: list[SessionMessage] = []
        self._locks: dict[int, str] = {}
        self._next_sid = 1
        self._next_mid = 1

    def _insert_session(self, profile, metadata):
        sid = self._next_sid
        self._next_sid += 1
        self._sessions[sid] = profile
        return sid

    def _insert_message(self, session_id, role, content, tool_calls):
        mid = self._next_mid
        self._next_mid += 1
        self._messages.append(
            SessionMessage(mid, session_id, role, content, tool_calls, datetime.utcnow())
        )
        return mid

    def _fetch_messages(self, session_id, limit, since):
        out = [m for m in self._messages if m.session_id == session_id]
        if since:
            out = [m for m in out if m.created_at >= since]
        return out[:limit]

    def _acquire_lock(self, session_id, holder, ttl_seconds):
        if session_id in self._locks:
            return False
        self._locks[session_id] = holder
        return True

    def _release_lock(self, session_id, holder):
        if self._locks.get(session_id) == holder:
            del self._locks[session_id]
            return True
        return False

    def _close_session(self, session_id):
        return session_id in self._sessions


def test_sessions_open_and_append():
    s = FakeSessionsRepo()
    sid = s.open_session("default")
    assert sid > 0
    mid = s.append_message(sid, "user", "hello")
    assert mid > 0


def test_sessions_get_messages():
    s = FakeSessionsRepo()
    sid = s.open_session("default")
    s.append_message(sid, "user", "a")
    s.append_message(sid, "assistant", "b")
    msgs = s.get_messages(sid)
    assert len(msgs) == 2


def test_sessions_get_messages_since():
    s = FakeSessionsRepo()
    sid = s.open_session("default")
    s.append_message(sid, "user", "a")
    since = datetime.utcnow() + timedelta(milliseconds=10)
    # Force a clear wall-clock gap so the second message's ts > since.
    import time

    time.sleep(0.05)
    s.append_message(sid, "user", "b")
    msgs = s.get_messages(sid, since=since)
    assert len(msgs) == 1
    assert msgs[0].content == "b"


def test_sessions_invalid_role_raises():
    s = FakeSessionsRepo()
    sid = s.open_session("default")
    with pytest.raises(ValueError, match="role"):
        s.append_message(sid, "hacker", "x")  # type: ignore[arg-type]


def test_sessions_compression_lock_round_trip():
    s = FakeSessionsRepo()
    sid = s.open_session("default")
    assert s.acquire_compression_lock(sid, "alice") is True
    # Second attempt by a different holder fails
    assert s.acquire_compression_lock(sid, "bob") is False
    # Holder releases
    assert s.release_compression_lock(sid, "alice") is True
    # Now bob can take it
    assert s.acquire_compression_lock(sid, "bob") is True


def test_sessions_release_with_wrong_holder_fails():
    s = FakeSessionsRepo()
    sid = s.open_session("default")
    s.acquire_compression_lock(sid, "alice")
    assert s.release_compression_lock(sid, "bob") is False


def test_sessions_invalid_limit_raises():
    s = FakeSessionsRepo()
    sid = s.open_session("default")
    with pytest.raises(ValueError, match="limit"):
        s.get_messages(sid, limit=0)


def test_sessions_close():
    s = FakeSessionsRepo()
    sid = s.open_session("default")
    assert s.close_session(sid) is True
