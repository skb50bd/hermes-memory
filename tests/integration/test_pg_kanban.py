"""TDD: PgKanbanRepo — tenants, tasks, claim (SKIP LOCKED), lifecycle, history, search."""

from __future__ import annotations

import threading

import pytest


@pytest.fixture
def kanban_repo(pg_conn):
    from hermes_memory.pg_repos import PgKanbanRepo

    return PgKanbanRepo(pg_conn)


@pytest.fixture
def tenant(kanban_repo) -> str:
    kanban_repo.tenant_create("default", "Default Board")
    return "default"


def test_tenant_create_and_list(kanban_repo) -> None:
    kanban_repo.tenant_create("sv", "SportsVerse")
    kanban_repo.tenant_create("docs", "Docs")
    slugs = [t.slug for t in kanban_repo.list_tenants()]
    assert "sv" in slugs
    assert "docs" in slugs


def test_create_and_get_task(kanban_repo, tenant) -> None:
    tid = kanban_repo.create(tenant, "Write README", body="explain quickstart")
    assert tid.startswith("t_")
    t = kanban_repo.get(tid)
    assert t is not None
    assert t.title == "Write README"
    assert t.tenant_slug == tenant
    assert t.status == "ready"


def test_list_filters(kanban_repo, tenant) -> None:
    kanban_repo.create(tenant, "a", priority=5)
    kanban_repo.create(tenant, "b", priority=1)
    kanban_repo.create(tenant, "c", priority=5, assignee="alice")
    tasks = kanban_repo.list(tenant, limit=10)
    assert len(tasks) == 3
    # Filter by assignee
    alice = kanban_repo.list(tenant, assignee="alice", limit=10)
    assert len(alice) == 1
    assert alice[0].title == "c"


def test_claim_picks_highest_priority_first(kanban_repo, tenant) -> None:
    kanban_repo.create(tenant, "low", priority=1)
    kanban_repo.create(tenant, "high", priority=10)
    kanban_repo.create(tenant, "mid", priority=5)
    t = kanban_repo.claim("worker-1")
    assert t is not None
    assert t.title == "high"


def test_claim_is_exclusive_under_concurrency(kanban_repo, tenant) -> None:
    """Two workers racing the same queue get DIFFERENT tasks."""
    kanban_repo.create(tenant, "task1", priority=1)
    kanban_repo.create(tenant, "task2", priority=1)
    kanban_repo.create(tenant, "task3", priority=1)
    results: list = []
    lock = threading.Lock()

    def claim_one(name):
        task = kanban_repo.claim(name)
        with lock:
            results.append(task)

    threads = [threading.Thread(target=claim_one, args=(f"w{i}",)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    claimed_ids = {r.id for r in results if r}
    assert len(claimed_ids) == 3  # all 3 distinct


def test_claim_returns_none_when_empty(kanban_repo, tenant) -> None:
    assert kanban_repo.claim("worker-1") is None


def test_heartbeat_after_claim(kanban_repo, tenant) -> None:
    kanban_repo.create(tenant, "work", priority=1)
    t = kanban_repo.claim("worker-1")
    assert t is not None
    assert kanban_repo.heartbeat(t.id) is True


def test_complete_task(kanban_repo, tenant) -> None:
    kanban_repo.create(tenant, "work", priority=1)
    t = kanban_repo.claim("worker-1")
    assert t is not None
    assert kanban_repo.complete(t.id, "done") is True
    task = kanban_repo.get(t.id)
    assert task is not None
    assert task.status == "done"


def test_fail_task(kanban_repo, tenant) -> None:
    kanban_repo.create(tenant, "work", priority=1)
    t = kanban_repo.claim("worker-1")
    assert t is not None
    assert kanban_repo.fail(t.id, "broken pipe") is True
    task = kanban_repo.get(t.id)
    assert task is not None
    assert task.status == "failed"


def test_comment_and_history(kanban_repo, tenant) -> None:
    tid = kanban_repo.create(tenant, "work", priority=1)
    cid = kanban_repo.comment(tid, "first comment", author="alice")
    assert cid > 0
    events = kanban_repo.history(tid, limit=10)
    kinds = [e.kind for e in events]
    assert "created" in kinds
    assert "commented" in kinds


def test_link_and_children(kanban_repo, tenant) -> None:
    parent = kanban_repo.create(tenant, "parent", priority=1)
    child = kanban_repo.create(tenant, "child", priority=1)
    assert kanban_repo.link(parent, child) is True
    children = kanban_repo.children(parent)
    assert any(c.id == child for c in children)


def test_link_self_is_noop(kanban_repo, tenant) -> None:
    tid = kanban_repo.create(tenant, "x", priority=1)
    assert kanban_repo.link(tid, tid) is False


def test_search_finds_by_keyword(kanban_repo, tenant) -> None:
    kanban_repo.create(tenant, "auth task", body="oauth integration")
    kanban_repo.create(tenant, "routing task", body="tenant routing")
    hits = kanban_repo.search("oauth", limit=5)
    assert any("auth" in h.title for h in hits)


def test_subscribe_and_unsubscribe(kanban_repo, tenant) -> None:
    tid = kanban_repo.create(tenant, "work", priority=1)
    assert kanban_repo.subscribe(tid, "discord", "12345") is True
    assert kanban_repo.unsubscribe(tid, "discord", "12345") is True
