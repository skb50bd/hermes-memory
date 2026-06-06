"""Sessions repository — hermes_sessions.sessions/messages/compression_locks.

This is the per-profile session storage. Note: hermes-agent already
has its own SQLite session store (`hermes_state.py`); this repo
exists to back the agent_memory parity surface (issue parity with
#8 — the postgres session is the durable one for those who prefer
not to use the local SQLite store).

Public surface:
  - open_session(profile, *, metadata) -> int
  - append_message(session_id, role, content, *, tool_calls) -> int
  - get_messages(session_id, *, limit, since) -> list[Message]
  - acquire_compression_lock(session_id, holder, ttl_seconds) -> bool
  - release_compression_lock(session_id, holder) -> bool
  - close_session(session_id) -> bool
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional


@dataclass
class SessionMessage:
    id: int
    session_id: int
    role: str
    content: str
    tool_calls: Optional[dict[str, Any]]
    created_at: datetime


class SessionsRepo:
    def open_session(
        self, profile: str, *, metadata: dict[str, Any] | None = None
    ) -> int:
        if not profile:
            raise ValueError("profile must be non-empty")
        return self._insert_session(profile, metadata or {})

    def append_message(
        self,
        session_id: int,
        role: str,
        content: str,
        *,
        tool_calls: dict[str, Any] | None = None,
    ) -> int:
        if role not in ("user", "assistant", "tool", "system"):
            raise ValueError(f"invalid role: {role}")
        return self._insert_message(session_id, role, content, tool_calls)

    def get_messages(
        self,
        session_id: int,
        *,
        limit: int = 100,
        since: datetime | None = None,
    ) -> list[SessionMessage]:
        if limit <= 0 or limit > 5000:
            raise ValueError("limit must be 1..5000")
        return self._fetch_messages(session_id, limit, since)

    def acquire_compression_lock(
        self, session_id: int, holder: str, *, ttl_seconds: int = 300
    ) -> bool:
        if not holder:
            raise ValueError("holder must be non-empty")
        return self._acquire_lock(session_id, holder, ttl_seconds)

    def release_compression_lock(self, session_id: int, holder: str) -> bool:
        return self._release_lock(session_id, holder)

    def close_session(self, session_id: int) -> bool:
        return self._close_session(session_id)

    # hooks
    def _insert_session(self, profile, metadata) -> int:
        raise NotImplementedError

    def _insert_message(self, session_id, role, content, tool_calls) -> int:
        raise NotImplementedError

    def _fetch_messages(self, session_id, limit, since) -> list[SessionMessage]:
        raise NotImplementedError

    def _acquire_lock(self, session_id, holder, ttl_seconds) -> bool:
        raise NotImplementedError

    def _release_lock(self, session_id, holder) -> bool:
        raise NotImplementedError

    def _close_session(self, session_id) -> bool:
        raise NotImplementedError
