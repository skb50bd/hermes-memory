"""TDD: tools layer."""

from __future__ import annotations

import json

from hermes_memory.repos.kanban_repo import KanbanRepo, Task, Tenant
from hermes_memory.repos.memory_repo import (
    MEMORY_MAX_CHARS,
    Memory,
    MemoryNotFoundError,
    MemoryRepo,
)
from hermes_memory.repos.wiki_repo import Document, WikiRepo
from hermes_memory.tools import (
    make_all_tools,
    make_kanban_tools,
    make_memory_tools,
    make_wiki_tools,
)


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------
class FakeRepo(MemoryRepo):
    def __init__(self):
        self.memories: dict[int, Memory] = {}
        self.next_id = 1

    def _insert_memory(self, content, *, tags, category, source, embedding_dim):
        for m in self.memories.values():
            if m.content == content and m.source == source and not m.deleted:
                return 0
        mid = self.next_id
        self.next_id += 1
        self.memories[mid] = Memory(
            id=mid, content=content, tags=tuple(tags), category=category,
            source=source, embedding_dim=embedding_dim, deleted=False,
        )
        return mid

    def _embed_query(self, query: str) -> list[float]:
        return [0.0] * 1024

    def _search(self, query_embedding, query_text, *, top_k, hybrid_text_weight):
        return list(self.memories.values())[:top_k]

    def _forget(self, memory_id):
        m = self.memories.get(memory_id)
        if m is None:
            raise MemoryNotFoundError(str(memory_id))
        if m.deleted:
            return False
        m.mark_deleted()
        return True

    def _status(self):
        return {"live_memories": sum(1 for m in self.memories.values() if not m.deleted)}


def test_memory_remember_stores():
    r = FakeRepo()
    tools = make_memory_tools(r)
    out = tools["memory_remember"]("hello")
    assert "Stored memory 1" in out


def test_memory_remember_dedup():
    r = FakeRepo()
    tools = make_memory_tools(r)
    tools["memory_remember"]("hello")
    out = tools["memory_remember"]("hello")
    assert "deduped" in out


def test_memory_remember_too_large_returns_routing_error():
    r = FakeRepo()
    tools = make_memory_tools(r)
    out = tools["memory_remember"]("x" * (MEMORY_MAX_CHARS + 1))
    data = json.loads(out)
    assert "error" in data
    assert "routing" in data.get("error", "").lower() or "routing_rule" in str(data)


def test_memory_search_returns_json():
    r = FakeRepo()
    tools = make_memory_tools(r)
    tools["memory_remember"]("a")
    tools["memory_remember"]("b")
    out = tools["memory_search"]("a")
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    assert len(parsed) == 2


def test_memory_forget_ok():
    r = FakeRepo()
    tools = make_memory_tools(r)
    tools["memory_remember"]("x")
    out = tools["memory_forget"](1)
    assert "Forgot memory 1" in out


def test_memory_forget_missing():
    r = FakeRepo()
    tools = make_memory_tools(r)
    out = tools["memory_forget"](999)
    data = json.loads(out)
    assert "error" in data


def test_memory_status():
    r = FakeRepo()
    tools = make_memory_tools(r)
    tools["memory_remember"]("x")
    out = tools["memory_status"]()
    data = json.loads(out)
    assert data["live_memories"] == 1


# ---------------------------------------------------------------------------
# Wiki
# ---------------------------------------------------------------------------
class FakeWiki(WikiRepo):
    def __init__(self):
        self.docs: dict[str, Document] = {}
        self.next_id = 1

    def _insert_document(self, slug, title, body_md, *, category, tags, metadata):
        if slug in self.docs:
            raise ValueError(f"slug already exists: {slug}")
        did = self.next_id
        self.next_id += 1
        self.docs[slug] = Document(did, slug, title, body_md, category, metadata, tuple(tags))
        return did

    def _fetch_document(self, slug):
        return self.docs.get(slug)

    def _insert_link(self, source_slug, target_slug, context):
        return source_slug in self.docs and target_slug in self.docs

    def _fetch_backlinks(self, target_slug):
        return []

    def _fetch_related(self, slug, max_hops):
        return []

    def _search(self, query, *, top_k):
        return [d for d in self.docs.values() if query.lower() in d.body_md.lower()][:top_k]


def test_wiki_create_and_read():
    w = FakeWiki()
    tools = make_wiki_tools(w)
    out = tools["wiki_create"]("foo", "Foo", "body")
    assert "foo" in out
    out = tools["wiki_read"]("foo")
    data = json.loads(out)
    assert data["slug"] == "foo"


def test_wiki_create_duplicate():
    w = FakeWiki()
    tools = make_wiki_tools(w)
    tools["wiki_create"]("foo", "Foo", "x")
    out = tools["wiki_create"]("foo", "Foo2", "y")
    data = json.loads(out)
    assert "error" in data


def test_wiki_read_missing():
    w = FakeWiki()
    tools = make_wiki_tools(w)
    out = tools["wiki_read"]("nope")
    data = json.loads(out)
    assert "not_found" in data.get("error", "")


def test_wiki_search():
    w = FakeWiki()
    tools = make_wiki_tools(w)
    tools["wiki_create"]("a", "A", "postgres tips")
    tools["wiki_create"]("b", "B", "wiki stuff")
    out = tools["wiki_search"]("postgres")
    data = json.loads(out)
    assert len(data) == 1


# ---------------------------------------------------------------------------
# Kanban
# ---------------------------------------------------------------------------
class FakeKanban(KanbanRepo):
    def __init__(self):
        self.tenants: dict[str, int] = {}
        self.tasks: dict[str, Task] = {}
        self.next_tid = 1

    def _insert_tenant(self, slug, name, description, icon, color):
        tid = self.next_tid
        self.next_tid += 1
        self.tenants[slug] = tid
        return tid

    def _fetch_tenants(self):
        return [Tenant(t, s, s, "", "", "") for s, t in self.tenants.items()]

    def _insert_task(self, task_id, tenant_slug, title, body, status, priority,
                     assignee, parent_id, tags, skills_json):
        self.tasks[task_id] = Task(
            task_id, tenant_slug, title, body, status, priority, assignee,
            parent_id, tuple(tags), skills_json
        )

    def _fetch_tasks(self, tenant_slug, *, status, assignee, limit):
        return [t for t in self.tasks.values() if t.tenant_slug == tenant_slug]

    def _fetch_task(self, task_id):
        return self.tasks.get(task_id)

    def _claim_next(self, assignee, max_runtime_seconds):
        for t in self.tasks.values():
            if t.status == "ready":
                t.status = "running"
                t.assignee = assignee
                return t
        return None

    def _heartbeat(self, task_id):
        return task_id in self.tasks

    def _complete_task(self, task_id, summary, result):
        if task_id in self.tasks:
            self.tasks[task_id].status = "done"
            return True
        return False

    def _fail_task(self, task_id, error, status):
        if task_id in self.tasks:
            self.tasks[task_id].status = status
            return True
        return False

    def _insert_comment(self, task_id, body, author):
        return 1

    def _fetch_history(self, task_id, limit):
        return []

    def _insert_link(self, parent_id, child_id):
        return True

    def _fetch_children(self, parent_id):
        return []

    def _fetch_parents(self, child_id):
        return []

    def _insert_subscription(self, task_id, platform, chat_id, thread_id, user_id):
        return True

    def _delete_subscription(self, task_id, platform, chat_id, thread_id):
        return True

    def _search(self, query, *, tenant_slug, limit):
        return []


def test_kanban_create_and_get():
    k = FakeKanban()
    tools = make_kanban_tools(k)
    tools["kanban_tenant_create"]("sv", "SV")
    out = tools["kanban_create"]("sv", "Fix bug")
    assert "t_" in out
    tid = out.split(" ")[2]
    out = tools["kanban_get"](tid)
    data = json.loads(out)
    assert data["title"] == "Fix bug"


def test_kanban_claim():
    k = FakeKanban()
    tools = make_kanban_tools(k)
    tools["kanban_tenant_create"]("a", "A")
    out = tools["kanban_create"]("a", "x")
    tid = out.split(" ")[2]
    claimed = tools["kanban_claim"]("alice")
    data = json.loads(claimed)
    assert data["id"] == tid
    assert data["assignee"] == "alice"


def test_kanban_tenants():
    k = FakeKanban()
    tools = make_kanban_tools(k)
    tools["kanban_tenant_create"]("a", "A")
    out = tools["kanban_tenants"]()
    data = json.loads(out)
    assert len(data) == 1


# ---------------------------------------------------------------------------
# make_all_tools
# ---------------------------------------------------------------------------
def test_make_all_tools_dispatches():
    repos = {
        "memory": FakeRepo(),
        "wiki": FakeWiki(),
        "kanban": FakeKanban(),
    }
    tools = make_all_tools(repos)
    assert "memory_remember" in tools
    assert "wiki_create" in tools
    assert "kanban_create" in tools
    assert "journal_log_session" not in tools


def test_make_all_tools_with_empty_repos():
    tools = make_all_tools({})
    assert tools == {}
