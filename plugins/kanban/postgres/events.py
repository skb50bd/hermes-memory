"""Kanban event consumer using PostgreSQL LISTEN/NOTIFY.

Replaces SQLite file-tailing for the dashboard WebSocket.
Connects to Postgres, LISTENs on 'hermes_kanban_event', and yields
parsed events for the dashboard to broadcast.
"""

from __future__ import annotations

import json
import logging
import select
import threading
from typing import Callable, Dict, Optional

import psycopg2
from psycopg2.extensions import POLL_OK, POLL_READ, POLL_WRITE

from plugins.kanban.postgres import get_pg_kanban_db_conn_str

logger = logging.getLogger(__name__)


class KanbanEventListener:
    """LISTEN/NOTIFY consumer for hermes_kanban_event channel."""

    def __init__(self):
        self._conn: Optional[psycopg2.extensions.connection] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._handlers: list[Callable[[Dict], None]] = []

    def add_handler(self, handler: Callable[[Dict], None]) -> None:
        self._handlers.append(handler)

    def start(self) -> None:
        if self._running:
            return
        dsn = get_pg_kanban_db_conn_str()
        self._conn = psycopg2.connect(dsn)
        self._conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
        with self._conn.cursor() as cur:
            cur.execute("LISTEN hermes_kanban_event")
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("Kanban event listener started")

    def stop(self) -> None:
        self._running = False
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def _loop(self) -> None:
        while self._running and self._conn:
            try:
                # Wait for notification with timeout
                if select.select([self._conn], [], [], 1.0) == ([], [], []):
                    continue
                self._conn.poll()
                while self._conn.notifies:
                    notify = self._conn.notifies.pop(0)
                    try:
                        payload = json.loads(notify.payload) if notify.payload else {}
                        payload["channel"] = notify.channel
                        for handler in self._handlers:
                            try:
                                handler(payload)
                            except Exception as e:
                                logger.warning("Kanban event handler error: %s", e)
                    except json.JSONDecodeError:
                        logger.warning("Invalid kanban event payload: %r", notify.payload)
            except Exception as e:
                if self._running:
                    logger.warning("Kanban event loop error: %s", e)
                break


def create_websocket_handler(websocket_send: Callable) -> Callable[[Dict], None]:
    """Create a handler that forwards kanban events to a WebSocket."""
    def handler(event: Dict) -> None:
        try:
            websocket_send(json.dumps({
                "type": "kanban_event",
                "task_id": event.get("task_id"),
                "kind": event.get("kind"),
                "actor": event.get("actor"),
                "payload": event.get("payload"),
            }))
        except Exception as e:
            logger.warning("WebSocket send failed: %s", e)
    return handler
