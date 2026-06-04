"""PostgreSQL session store adapter for Hermes Agent.

Mirrors or replaces SQLite SessionDB. Supports dual-write mode
(SQLite primary + Postgres mirror) and Postgres-primary mode.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

import psycopg2
import psycopg2.pool
from psycopg2.extensions import make_dsn

logger = logging.getLogger(__name__)

_POOL = None
_POOL_LOCK = threading.Lock()


def _get_pool():
    global _POOL
    if _POOL is not None:
        return _POOL
    with _POOL_LOCK:
        if _POOL is None:
            dsn = os.environ.get("PG_MEM_DB_CONN_STR", "").strip()
            if not dsn:
                raise RuntimeError("PG_MEM_DB_CONN_STR not set")
            _POOL = psycopg2.pool.ThreadedConnectionPool(0, 2, dsn)
        return _POOL


@contextmanager
def _cursor(*, commit: bool = False) -> Iterator[Any]:
    pool = _get_pool()
    conn = pool.getconn()
    cur = None
    try:
        conn.autocommit = not commit
        cur = conn.cursor()
        yield cur
        if commit:
            conn.commit()
    except Exception:
        if commit:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        pool.putconn(conn, close=False)


class PostgresSessionStore:
    """Postgres-backed session store matching Hermes SessionDB API."""

    def create_session(
        self,
        session_id: str,
        source: str,
        **kwargs,
    ) -> str:
        with _cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO hermes_sessions.sessions
                (id, profile, source, parent_session_id, title, model, system_prompt, cwd,
                 platform, chat_id, thread_id, user_id, user_name, gateway_session_key, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    ended_at = NULL, end_reason = NULL, archived = false
                RETURNING id
                """,
                (
                    session_id,
                    kwargs.get("profile", "default"),
                    source,
                    kwargs.get("parent_session_id"),
                    kwargs.get("title"),
                    kwargs.get("model"),
                    kwargs.get("system_prompt"),
                    kwargs.get("cwd"),
                    kwargs.get("platform"),
                    kwargs.get("chat_id"),
                    kwargs.get("thread_id"),
                    kwargs.get("user_id"),
                    kwargs.get("user_name"),
                    kwargs.get("gateway_session_key"),
                    json.dumps(kwargs.get("metadata", {})),
                ),
            )
            return cur.fetchone()[0]

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        **kwargs,
    ) -> int:
        with _cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO hermes_sessions.messages
                (session_id, role, content, tool_calls, tool_call_id, model, token_count, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    session_id, role, content,
                    json.dumps(kwargs.get("tool_calls")) if kwargs.get("tool_calls") else None,
                    kwargs.get("tool_call_id"),
                    kwargs.get("model"),
                    kwargs.get("token_count"),
                    json.dumps(kwargs.get("metadata", {})),
                ),
            )
            msg_id = cur.fetchone()[0]
            # Update session message count
            cur.execute(
                "UPDATE hermes_sessions.sessions SET message_count = message_count + 1 WHERE id = %s",
                (session_id,),
            )
            return msg_id

    def get_session_messages(
        self,
        session_id: str,
        limit: int = 1000,
    ) -> List[Dict]:
        with _cursor() as cur:
            cur.execute(
                """
                SELECT id, timestamp, role, content, tool_calls, model, token_count
                FROM hermes_sessions.messages
                WHERE session_id = %s
                ORDER BY timestamp
                LIMIT %s
                """,
                (session_id, limit),
            )
            rows = cur.fetchall()
        return [
            {
                "id": r[0],
                "timestamp": r[1].isoformat() if r[1] else None,
                "role": r[2],
                "content": r[3],
                "tool_calls": r[4],
                "model": r[5],
                "token_count": r[6],
            }
            for r in rows
        ]

    def search_sessions(self, query: str, limit: int = 10) -> List[Dict]:
        with _cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT s.id, s.title, s.started_at
                FROM hermes_sessions.sessions s
                JOIN hermes_sessions.messages m ON m.session_id = s.id
                WHERE m.content_tsv @@ plainto_tsquery('english', %s)
                ORDER BY s.started_at DESC
                LIMIT %s
                """,
                (query, limit),
            )
            rows = cur.fetchall()
        return [
            {"id": r[0], "title": r[1], "started_at": r[2].isoformat() if r[2] else None}
            for r in rows
        ]

    def update_token_counts(self, session_id: str, token_count: int) -> bool:
        with _cursor(commit=True) as cur:
            cur.execute(
                "UPDATE hermes_sessions.sessions SET token_count = %s WHERE id = %s",
                (token_count, session_id),
            )
            return cur.rowcount > 0

    def acquire_compression_lock(self, session_id: str, holder: str, ttl_seconds: int = 60) -> bool:
        now = datetime.now(timezone.utc)
        expires = now + __import__("datetime").timedelta(seconds=ttl_seconds)
        with _cursor(commit=True) as cur:
            cur.execute(
                """
                INSERT INTO hermes_sessions.compression_locks
                (session_id, holder, acquired_at, expires_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (session_id) DO UPDATE SET
                    holder = EXCLUDED.holder,
                    acquired_at = EXCLUDED.acquired_at,
                    expires_at = EXCLUDED.expires_at
                WHERE hermes_sessions.compression_locks.expires_at < now()
                RETURNING session_id
                """,
                (session_id, holder, now, expires),
            )
            return cur.fetchone() is not None

    def release_compression_lock(self, session_id: str, holder: str) -> bool:
        with _cursor(commit=True) as cur:
            cur.execute(
                """
                DELETE FROM hermes_sessions.compression_locks
                WHERE session_id = %s AND holder = %s
                RETURNING session_id
                """,
                (session_id, holder),
            )
            return cur.fetchone() is not None


def register(ctx) -> None:
    """Register session store adapter."""
    pass
