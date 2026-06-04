"""Concurrent claim tests for kanban SKIP LOCKED.

Verifies that multiple workers cannot double-claim the same task.
"""

from __future__ import annotations

import concurrent.futures
import os
import uuid

import psycopg2
import pytest

# Ensure the plugin is importable
import sys
sys.path.insert(0, "/home/pixu/.hermes/hermes-agent")

from plugins.kanban.postgres import (
    create_task, claim_next, complete_task, get_task,
    list_tasks, _get_pool,
)


@pytest.fixture(scope="module")
def db_conn():
    dsn = os.environ.get("PG_MEM_DB_CONN_STR", "")
    if not dsn:
        pytest.skip("PG_MEM_DB_CONN_STR not set")
    conn = psycopg2.connect(dsn)
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def clean_tasks(db_conn):
    """Remove all test tasks before each test."""
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM hermes_kanban.tasks WHERE title LIKE 'test-%'")
        # Also reset any running tasks from previous failed tests
        cur.execute("UPDATE hermes_kanban.tasks SET status = 'ready', worker_pid = NULL, current_run_id = NULL WHERE status = 'running'")
    db_conn.commit()
    yield


class TestKanbanClaim:
    def test_single_claim(self, db_conn):
        task = create_task(title="test-single", tenant="default")
        claimed = claim_next(worker_pid=1234)
        assert claimed is not None
        assert claimed["id"] == task["id"]
        assert claimed["status"] == "running"
        # Second claim should return None
        claimed2 = claim_next(worker_pid=5678)
        assert claimed2 is None

    def test_concurrent_claims_no_duplicates(self, db_conn):
        # Create 5 tasks
        tasks = [create_task(title=f"test-concurrent-{i}", tenant="default") for i in range(5)]
        task_ids = {t["id"] for t in tasks}

        def worker(worker_id: int):
            claimed = []
            for _ in range(10):  # Try to claim up to 10 times
                result = claim_next(worker_pid=worker_id)
                if result is None:
                    break
                claimed.append(result["id"])
            return claimed

        # Run 10 workers concurrently
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(worker, i) for i in range(10)]
            results = [f.result(timeout=30) for f in futures]

        # Collect all claimed IDs
        all_claimed = []
        for r in results:
            all_claimed.extend(r)

        # No duplicates
        assert len(all_claimed) == len(set(all_claimed)), f"Duplicate claims detected! {all_claimed}"
        # All 5 tasks claimed
        assert set(all_claimed) == task_ids, f"Not all tasks claimed: {set(all_claimed)} vs {task_ids}"

    def test_claim_heartbeat_complete_cycle(self, db_conn):
        from plugins.kanban.postgres import heartbeat_claim

        task = create_task(title="test-cycle", tenant="default")
        claimed = claim_next(worker_pid=9999)
        assert claimed["id"] == task["id"]

        # Heartbeat
        ok = heartbeat_claim(task["id"], worker_pid=9999, extend_seconds=600)
        assert ok

        # Complete
        completed = complete_task(task["id"], "done", worker_pid=9999)
        assert completed is not None
        assert completed["status"] == "done"

        # Re-claim should fail (task is done)
        re_claimed = claim_next(worker_pid=1111)
        assert re_claimed is None or re_claimed["id"] != task["id"]

    def test_fail_and_requeue(self, db_conn):
        from plugins.kanban.postgres import fail_task

        task = create_task(title="test-fail", tenant="default")
        claimed = claim_next(worker_pid=2222)
        assert claimed["id"] == task["id"]

        # Fail with requeue
        failed = fail_task(task["id"], "error", worker_pid=2222, requeue=True)
        assert failed is not None
        assert failed["status"] == "ready"
        assert failed["consecutive_failures"] == 1

        # Can claim again
        reclaimed = claim_next(worker_pid=3333)
        assert reclaimed is not None
        assert reclaimed["id"] == task["id"]
