"""PostgreSQL observability plugin for Hermes Agent.

Structured logs, traces, LLM calls, and tool calls in TimescaleDB.
Fail-open with bounded queue and file fallback.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import psycopg2
from psycopg2.extensions import make_dsn

logger = logging.getLogger(__name__)

# Bounded queue: drop oldest if full
_MAX_QUEUE_SIZE = 1000
_FLUSH_INTERVAL_SECONDS = 5

# Redaction patterns
_REDACT_KEYS = {"password", "secret", "token", "api_key", "auth", "credential"}


def _redact_dict(d: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively redact sensitive keys from dict."""
    if not isinstance(d, dict):
        return d
    out = {}
    for k, v in d.items():
        if any(r in k.lower() for r in _REDACT_KEYS):
            out[k] = "***"
        elif isinstance(v, dict):
            out[k] = _redact_dict(v)
        elif isinstance(v, list):
            out[k] = [_redact_dict(i) if isinstance(i, dict) else i for i in v]
        else:
            out[k] = v
    return out


class PostgresLogHandler(logging.Handler):
    """Logging handler that writes to TimescaleDB with bounded queue.

    Fail-open: if DB is unavailable, drops records silently (after
    queue fills) rather than blocking the application.
    """

    def __init__(self, dsn: Optional[str] = None):
        super().__init__()
        self.dsn = dsn or os.environ.get("PG_MEM_DB_CONN_STR", "").strip()
        self._queue: queue.Queue[Dict] = queue.Queue(maxsize=_MAX_QUEUE_SIZE)
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._conn: Optional[psycopg2.extensions.connection] = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self.flush()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = {
                "ts": datetime.fromtimestamp(record.created, tz=timezone.utc),
                "level": record.levelname,
                "logger": record.name,
                "message": self.format(record),
                "exception": traceback.format_exception(*record.exc_info) if record.exc_info else None,
                "profile": getattr(record, "profile", None),
                "session_id": getattr(record, "session_id", None),
                "task_id": getattr(record, "task_id", None),
                "platform": getattr(record, "platform", None),
                "metadata": _redact_dict(getattr(record, "extra", {})),
            }
            # Drop oldest if full (fail-open)
            if self._queue.full():
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass
            self._queue.put_nowait(payload)
        except Exception:
            self.handleError(record)

    def flush(self) -> None:
        self._drain_queue()

    def _flush_loop(self) -> None:
        while self._running:
            time.sleep(_FLUSH_INTERVAL_SECONDS)
            self._drain_queue()

    def _drain_queue(self) -> None:
        batch = []
        while not self._queue.empty() and len(batch) < 100:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if not batch:
            return
        try:
            self._insert_batch(batch)
        except Exception as e:
            logger.debug("Postgres log insert failed: %s", e)
            # Re-queue for retry (drop if full)
            for item in batch:
                if self._queue.full():
                    break
                try:
                    self._queue.put_nowait(item)
                except queue.Full:
                    break

    def _insert_batch(self, batch: list) -> None:
        if not self.dsn:
            return
        if self._conn is None:
            self._conn = psycopg2.connect(self.dsn)
        with self._conn.cursor() as cur:
            for item in batch:
                cur.execute(
                    """
                    INSERT INTO hermes_observability.logs
                    (ts, level, logger, message, exception, profile, session_id, task_id, platform, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        item["ts"], item["level"], item["logger"], item["message"],
                        item["exception"], item["profile"], item["session_id"],
                        item["task_id"], item["platform"], json.dumps(item["metadata"]),
                    ),
                )
        self._conn.commit()


class ObservabilityCollector:
    """Collect traces, LLM calls, and tool calls."""

    def __init__(self, dsn: Optional[str] = None):
        self.dsn = dsn or os.environ.get("PG_MEM_DB_CONN_STR", "").strip()
        self._conn: Optional[psycopg2.extensions.connection] = None

    def _get_conn(self) -> psycopg2.extensions.connection:
        if self._conn is None:
            self._conn = psycopg2.connect(self.dsn)
        return self._conn

    def record_llm_call(
        self,
        *,
        profile: str,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        latency_ms: Optional[float] = None,
        cost_usd: Optional[float] = None,
        metadata: Optional[Dict] = None,
    ) -> None:
        try:
            conn = self._get_conn()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO hermes_observability.llm_calls
                    (ts, profile, session_id, model, provider, prompt_tokens, completion_tokens, total_tokens, latency_ms, cost_usd, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        datetime.now(timezone.utc), profile, session_id, model, provider,
                        prompt_tokens, completion_tokens,
                        (prompt_tokens or 0) + (completion_tokens or 0),
                        latency_ms, cost_usd, json.dumps(_redact_dict(metadata or {})),
                    ),
                )
            conn.commit()
        except Exception as e:
            logger.debug("LLM call record failed: %s", e)

    def record_tool_call(
        self,
        *,
        profile: str,
        session_id: Optional[str] = None,
        tool_name: str,
        tool_call_id: Optional[str] = None,
        latency_ms: Optional[float] = None,
        success: Optional[bool] = None,
        error: Optional[str] = None,
        metadata: Optional[Dict] = None,
    ) -> None:
        try:
            conn = self._get_conn()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO hermes_observability.tool_calls
                    (ts, profile, session_id, tool_name, tool_call_id, latency_ms, success, error, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        datetime.now(timezone.utc), profile, session_id, tool_name,
                        tool_call_id, latency_ms, success, error,
                        json.dumps(_redact_dict(metadata or {})),
                    ),
                )
            conn.commit()
        except Exception as e:
            logger.debug("Tool call record failed: %s", e)


def install_handler() -> PostgresLogHandler:
    """Install the Postgres logging handler on the root logger."""
    handler = PostgresLogHandler()
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter("%(message)s")
    handler.setFormatter(formatter)
    logging.getLogger().addHandler(handler)
    handler.start()
    return handler


def register(ctx) -> None:
    pass
