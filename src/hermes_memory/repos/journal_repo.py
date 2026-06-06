"""Journal repository — hermes_journal.sessions + messages.

Public surface (from JournalTools.cs):
  - log_session(profile, *, metadata) -> int
  - log_message(session_id, role, content, *, tool_calls) -> int
  - search(query, *, top_k, session_id, role) -> list[Message]
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

Role = Literal["user", "assistant", "tool", "system"]


@dataclass
class Message:
    id: int
    session_id: int
    role: Role
    content: str
    tool_calls: dict[str, Any] | None


class JournalRepo:
    def log_session(self, profile: str, *, metadata: dict[str, Any] | None = None) -> int:
        if not profile:
            raise ValueError("profile must be non-empty")
        return self._insert_session(profile, metadata or {})

    def log_message(
        self,
        session_id: int,
        role: Role,
        content: str,
        *,
        tool_calls: dict[str, Any] | None = None,
    ) -> int:
        if role not in ("user", "assistant", "tool", "system"):
            raise ValueError(f"invalid role: {role}")
        if not content and not tool_calls:
            raise ValueError("message must have content or tool_calls")
        return self._insert_message(session_id, role, content, tool_calls)

    def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        session_id: int | None = None,
        role: Role | None = None,
    ) -> list[Message]:
        if not query.strip():
            return []
        return self._search(query, top_k=top_k, session_id=session_id, role=role)

    def _insert_session(self, profile, metadata) -> int:
        raise NotImplementedError

    def _insert_message(self, session_id, role, content, tool_calls) -> int:
        raise NotImplementedError

    def _search(self, query, *, top_k, session_id, role) -> list[Message]:
        raise NotImplementedError
