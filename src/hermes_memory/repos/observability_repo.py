"""Observability repository — hermes_observability logs/traces/spans/llm_calls/tool_calls.

Fail-open with bounded queue and file fallback. The interface is
intentionally small — the original C# tool surface is captured in
the observability plugin's QueueHandler (see plugins/observability/postgres/__init__.py).

Public surface:
  - log(level, message, *, profile, fields) -> int | None
  - record_llm_call(profile, model, prompt_tokens, completion_tokens, duration_ms, *, status) -> int
  - record_tool_call(profile, tool, duration_ms, *, status, error) -> int
  - flush() -> int   # returns count drained
  - close() -> None
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


@dataclass
class LogEvent:
    level: LogLevel
    message: str
    profile: str
    fields: dict[str, Any]
    created_at: str


@dataclass
class LLMCall:
    profile: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    duration_ms: int
    status: str  # "ok" | "error"


@dataclass
class ToolCall:
    profile: str
    tool: str
    duration_ms: int
    status: str
    error: str | None


class ObservabilityRepo:
    """Base class. Subclasses implement the storage hooks.

    All public methods are non-blocking — they're safe to call from
    the hot path. Subclasses MUST implement bounded queue + file
    fallback (see PostgresLogHandler in the legacy plugin for the
    reference implementation).
    """

    def log(
        self,
        level: LogLevel,
        message: str,
        *,
        profile: str = "default",
        fields: dict[str, Any] | None = None,
    ) -> int | None:
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ValueError(f"invalid level: {level}")
        return self._insert_log(
            LogEvent(level, message, profile, fields or {}, "now")
        )

    def record_llm_call(
        self,
        profile: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        duration_ms: int,
        *,
        status: str = "ok",
    ) -> int:
        if status not in ("ok", "error"):
            raise ValueError(f"invalid status: {status}")
        return self._insert_llm_call(
            LLMCall(profile, model, prompt_tokens, completion_tokens, duration_ms, status)
        )

    def record_tool_call(
        self,
        profile: str,
        tool: str,
        duration_ms: int,
        *,
        status: str = "ok",
        error: str | None = None,
    ) -> int:
        if status not in ("ok", "error"):
            raise ValueError(f"invalid status: {status}")
        return self._insert_tool_call(
            ToolCall(profile, tool, duration_ms, status, error)
        )

    def flush(self) -> int:
        return self._flush()

    def close(self) -> None:
        self._close()

    # hooks
    def _insert_log(self, event: LogEvent) -> int | None:
        raise NotImplementedError

    def _insert_llm_call(self, call: LLMCall) -> int:
        raise NotImplementedError

    def _insert_tool_call(self, call: ToolCall) -> int:
        raise NotImplementedError

    def _flush(self) -> int:
        raise NotImplementedError

    def _close(self) -> None:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Redaction helper used by subclasses (and by the stdlib logging handler
# that wraps this repo).
# ---------------------------------------------------------------------------
_REDACT_KEYS = {"password", "secret", "token", "api_key", "auth", "credential"}


def redact_dict(d: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact sensitive keys (password, secret, token, ...)."""
    if not isinstance(d, dict):
        return d
    out: dict[str, Any] = {}
    for k, v in d.items():
        if any(r in k.lower() for r in _REDACT_KEYS):
            out[k] = "***"
        elif isinstance(v, dict):
            out[k] = redact_dict(v)
        elif isinstance(v, list):
            out[k] = [redact_dict(i) if isinstance(i, dict) else i for i in v]
        else:
            out[k] = v
    return out
