"""TDD: kanban_repo.py — tenants, tasks, claims, lifecycle, history."""

from __future__ import annotations

import pytest

from hermes_memory.repos.kanban_repo import Event, KanbanRepo, Task, Tenant


class FakeKanbanRepo(KanbanRepo):
    def __init__(self):
        self._tenants: dict[str, tuple[int, str, str, str, str]] = {}
        self._tasks: dict[str, Task] = {}
        self._events: list[Event] = []
        self._next_tid = 1
        self._next_eid = 1

    # tenants
    def _insert_tenant(self, slug, name, description, icon, color):
        tid = self._next_tid
        self._next_tid += 1
        self._tenants[slug] = (tid, name, description, icon, color)
        return tid

    def _fetch_tenants(self):
        out = []
        for slug, (tid, name, desc, icon, color) in self._tenants.items():
            out.append(Tenant(tid, slug, name, desc, icon, color))
        return out

    # tasks
    def _insert_task(
        self,
        task_id,
        tenant_slug,
        title,
        body,
        status,
        priority,
        assignee,
        parent_id,
        tags,
        skills_json,
    ):
        self._tasks[task_id] = Task(
            id=task_id,
            tenant_slug=tenant_slug,
            title=title,
            body=body,
            status=status,
            priority=priority,
            assignee=assignee,
            parent_id=parent_id,
            tags=tuple(tags),
            skills_json=skills_json,
        )
        self._events.append(
            Event(self._next_eid, task_id, "created", assignee or "system", {}, "now")
        )
        self._next_eid += 1

    def _fetch_tasks(self, tenant_slug, *, status, assignee, limit):
        out = []
        for t in self._tasks.values():
            if t.tenant_slug != tenant_slug:
                continue
            if status and t.status != status:
                continue
            if assignee and t.assignee != assignee:
                continue
            out.append(t)
        return out[:limit]

    def _fetch_task(self, task_id):
        return self._tasks.get(task_id)

    def _claim_next(self, assignee, max_runtime_seconds):
        # Naive: first ready task
        for t in self._tasks.values():
            if t.status == "ready":
                t.status = "running"
                t.assignee = assignee
                self._events.append(Event(self._next_eid, t.id, "claimed", assignee, {}, "now"))
                self._next_eid += 1
                return t
        return None

    def _heartbeat(self, task_id):
        return task_id in self._tasks

    def _complete_task(self, task_id, summary, result):
        t = self._tasks.get(task_id)
        if not t or t.status not in ("ready", "running", "blocked"):
            return False
        t.status = "done"
        self._events.append(
            Event(
                self._next_eid,
                task_id,
                "completed",
                t.assignee or "system",
                {"summary": summary, "result": result},
                "now",
            )
        )
        self._next_eid += 1
        return True

    def _fail_task(self, task_id, error, status):
        t = self._tasks.get(task_id)
        if not t:
            return False
        t.status = status
        self._events.append(
            Event(
                self._next_eid, task_id, "failed", t.assignee or "system", {"error": error}, "now"
            )
        )
        self._next_eid += 1
        return True

    def _insert_comment(self, task_id, body, author):
        self._events.append(
            Event(self._next_eid, task_id, "comment", author or "system", {"body": body}, "now")
        )
        self._next_eid += 1
        return self._next_eid - 1

    def _fetch_history(self, task_id, limit):
        return [e for e in self._events if e.task_id == task_id][:limit]

    def _insert_link(self, parent_id, child_id):
        if parent_id not in self._tasks or child_id not in self._tasks:
            return False
        self._tasks[child_id].parent_id = parent_id
        return True

    def _fetch_children(self, parent_id):
        return [t for t in self._tasks.values() if t.parent_id == parent_id]

    def _fetch_parents(self, child_id):
        t = self._tasks.get(child_id)
        if not t or not t.parent_id:
            return []
        return [self._tasks[t.parent_id]] if t.parent_id in self._tasks else []

    def _insert_subscription(self, *args, **kwargs):
        return True

    def _delete_subscription(self, *args, **kwargs):
        return True

    def _search(self, query, *, tenant_slug, limit):
        out = []
        for t in self._tasks.values():
            if tenant_slug and t.tenant_slug != tenant_slug:
                continue
            if query.lower() in t.title.lower() or query.lower() in t.body.lower():
                out.append(t)
        return out[:limit]


@pytest.fixture
def repo():
    return FakeKanbanRepo()


# Tenants
def test_tenant_create_and_list(repo):
    tid = repo.tenant_create("sv", "SportsVerse")
    assert tid > 0
    tenants = repo.list_tenants()
    assert len(tenants) == 1
    assert tenants[0].slug == "sv"


def test_tenant_invalid_slug_raises(repo):
    with pytest.raises(ValueError, match="slug"):
        repo.tenant_create("with space!", "x")


# Task lifecycle
def test_create_and_get_task(repo):
    repo.tenant_create("sv", "SportsVerse")
    tid = repo.create("sv", "Fix bug", body="in chunker")
    assert tid.startswith("t_")
    t = repo.get(tid)
    assert t.title == "Fix bug"
    assert t.status == "ready"


def test_create_invalid_title_raises(repo):
    repo.tenant_create("sv", "SV")
    with pytest.raises(ValueError, match="title"):
        repo.create("sv", "")


def test_list_tasks_by_tenant(repo):
    repo.tenant_create("a", "A")
    repo.tenant_create("b", "B")
    repo.create("a", "x")
    repo.create("b", "y")
    repo.create("a", "z")
    a_tasks = repo.list("a")
    assert len(a_tasks) == 2


def test_list_tasks_filter_by_status(repo):
    repo.tenant_create("a", "A")
    repo.create("a", "x")
    repo.claim("alice")
    repo.create("a", "y")
    ready = repo.list("a", status="ready")
    assert len(ready) == 1
    assert ready[0].title == "y"


def test_claim_returns_first_ready(repo):
    repo.tenant_create("a", "A")
    repo.create("a", "first")
    repo.create("a", "second")
    claimed = repo.claim("alice")
    assert claimed is not None
    assert claimed.assignee == "alice"
    assert claimed.status == "running"


def test_claim_empty_when_no_ready(repo):
    repo.tenant_create("a", "A")
    assert repo.claim("alice") is None


def test_heartbeat_returns_true_for_known(repo):
    repo.tenant_create("a", "A")
    tid = repo.create("a", "x")
    assert repo.heartbeat(tid) is True


def test_complete_records_event(repo):
    repo.tenant_create("a", "A")
    tid = repo.create("a", "x")
    repo.claim("alice")
    assert repo.complete(tid, "done") is True
    hist = repo.history(tid)
    kinds = [e.kind for e in hist]
    assert "completed" in kinds


def test_fail_records_event(repo):
    repo.tenant_create("a", "A")
    tid = repo.create("a", "x")
    repo.claim("alice")
    assert repo.fail(tid, "boom") is True
    assert repo.get(tid).status == "failed"


def test_fail_invalid_status_raises(repo):
    with pytest.raises(ValueError, match="status"):
        repo.fail("t_x", "e", status="nope")  # type: ignore[arg-type]


def test_comment_appends_event(repo):
    repo.tenant_create("a", "A")
    tid = repo.create("a", "x")
    cid = repo.comment(tid, "looking into it", author="bob")
    assert cid > 0
    hist = repo.history(tid)
    assert any(e.kind == "comment" and e.payload["body"] == "looking into it" for e in hist)


# Structure
def test_link_parent_child(repo):
    repo.tenant_create("a", "A")
    parent = repo.create("a", "parent")
    child = repo.create("a", "child")
    assert repo.link(parent, child) is True
    children = repo.children(parent)
    assert len(children) == 1
    assert children[0].id == child


def test_link_self_returns_false(repo):
    repo.tenant_create("a", "A")
    t = repo.create("a", "x")
    assert repo.link(t, t) is False


def test_parents_walks_up(repo):
    repo.tenant_create("a", "A")
    grandparent = repo.create("a", "gp")
    parent = repo.create("a", "p")
    child = repo.create("a", "c")
    repo.link(grandparent, parent)
    repo.link(parent, child)
    assert repo.parents(child)[0].id == parent
    assert repo.parents(parent)[0].id == grandparent


# Search
def test_search_finds_matches(repo):
    repo.tenant_create("a", "A")
    repo.create("a", "postgres routing bug")
    repo.create("a", "wiki rendering")
    hits = repo.search("postgres")
    assert len(hits) == 1
    assert "postgres" in hits[0].title


def test_search_empty_query_returns_empty(repo):
    assert repo.search("") == []
