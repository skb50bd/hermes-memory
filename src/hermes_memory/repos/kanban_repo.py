"""Kanban repository — hermes_kanban tenants/tasks/runs/events/comments/etc.

Public surface (from KanbanTools.cs — 17 tools):
  tenants:
    - tenant_create(slug, name, *, description, icon, color) -> int
    - list_tenants() -> list[Tenant]
  tasks:
    - create(tenant_slug, title, *, body, priority, assignee, parent_id, tags, skills_json) -> str  # t_<id>
    - list(tenant_slug, *, status, assignee, limit) -> list[Task]
    - get(task_id) -> Task | None
  lifecycle:
    - claim(assignee, *, max_runtime_seconds) -> Task | None
    - heartbeat(task_id) -> bool
    - complete(task_id, summary, *, result) -> bool
    - fail(task_id, error, *, status) -> bool
  comments + history:
    - comment(task_id, body, *, author) -> int
    - history(task_id, *, limit) -> list[Event]
  structure:
    - link(parent_id, child_id) -> bool
    - children(parent_id) -> list[Task]
    - parents(child_id) -> list[Task]
  notifications:
    - subscribe(task_id, platform, chat_id, *, thread_id, user_id) -> bool
    - unsubscribe(task_id, platform, chat_id, *, thread_id) -> bool
  search:
    - search(query, *, tenant_slug, limit) -> list[Task]
"""

from __future__ import annotations

import secrets
import string
from dataclasses import dataclass
from typing import Any, Literal, Optional


TaskStatus = Literal[
    "ready", "running", "blocked", "done",
    "crashed", "timed_out", "failed", "archived", "cancelled",
]


@dataclass
class Tenant:
    id: int
    slug: str
    name: str
    description: str
    icon: str
    color: str


@dataclass
class Task:
    id: str  # "t_<26-char base32>"
    tenant_slug: str
    title: str
    body: str
    status: TaskStatus
    priority: int
    assignee: Optional[str]
    parent_id: Optional[str]
    tags: tuple[str, ...]
    skills_json: Optional[str]
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


@dataclass
class Event:
    id: int
    task_id: str
    kind: str
    actor: str
    payload: dict[str, Any]
    created_at: str


class KanbanRepo:
    BASE32 = string.ascii_lowercase + "234567"

    def _new_task_id(self) -> str:
        suffix = "".join(secrets.choice(self.BASE32) for _ in range(26))
        return f"t_{suffix}"

    def tenant_create(
        self,
        slug: str,
        name: str,
        *,
        description: str = "",
        icon: str = "",
        color: str = "",
    ) -> int:
        if not slug or not slug.replace("-", "").replace("_", "").isalnum():
            raise ValueError("slug must be URL-safe (alphanumeric, -, _)")
        if not name:
            raise ValueError("name must be non-empty")
        return self._insert_tenant(slug, name, description, icon, color)

    def list_tenants(self) -> list[Tenant]:
        return self._fetch_tenants()

    def create(
        self,
        tenant_slug: str,
        title: str,
        *,
        body: str = "",
        priority: int = 0,
        assignee: str | None = None,
        parent_id: str | None = None,
        tags: list[str] | None = None,
        skills_json: str | None = None,
    ) -> str:
        if not title:
            raise ValueError("title must be non-empty")
        if priority < 0:
            raise ValueError("priority must be >= 0")
        task_id = self._new_task_id()
        self._insert_task(
            task_id, tenant_slug, title, body, "ready",
            priority, assignee, parent_id, list(tags or []), skills_json,
        )
        return task_id

    def list(
        self,
        tenant_slug: str,
        *,
        status: TaskStatus | None = None,
        assignee: str | None = None,
        limit: int = 100,
    ) -> list[Task]:
        if limit <= 0 or limit > 1000:
            raise ValueError("limit must be 1..1000")
        return self._fetch_tasks(
            tenant_slug, status=status, assignee=assignee, limit=limit
        )

    def get(self, task_id: str) -> Optional[Task]:
        return self._fetch_task(task_id)

    def claim(
        self, assignee: str, *, max_runtime_seconds: int = 0
    ) -> Optional[Task]:
        if not assignee:
            raise ValueError("assignee must be non-empty")
        return self._claim_next(assignee, max_runtime_seconds)

    def heartbeat(self, task_id: str) -> bool:
        if not task_id.startswith("t_"):
            raise ValueError("task_id must look like 't_<suffix>'")
        return self._heartbeat(task_id)

    def complete(
        self, task_id: str, summary: str, *, result: str | None = None
    ) -> bool:
        if not summary:
            raise ValueError("summary must be non-empty")
        return self._complete_task(task_id, summary, result)

    def fail(
        self, task_id: str, error: str, *, status: str = "failed"
    ) -> bool:
        if status not in ("failed", "blocked", "cancelled"):
            raise ValueError(f"invalid fail status: {status}")
        return self._fail_task(task_id, error, status)

    def comment(self, task_id: str, body: str, *, author: str | None = None) -> int:
        if not body:
            raise ValueError("body must be non-empty")
        return self._insert_comment(task_id, body, author)

    def history(self, task_id: str, *, limit: int = 100) -> list[Event]:
        return self._fetch_history(task_id, limit)

    def link(self, parent_id: str, child_id: str) -> bool:
        if parent_id == child_id:
            return False
        return self._insert_link(parent_id, child_id)

    def children(self, parent_id: str) -> list[Task]:
        return self._fetch_children(parent_id)

    def parents(self, child_id: str) -> list[Task]:
        return self._fetch_parents(child_id)

    def subscribe(
        self,
        task_id: str,
        platform: str,
        chat_id: str,
        *,
        thread_id: str | None = None,
        user_id: str | None = None,
    ) -> bool:
        return self._insert_subscription(
            task_id, platform, chat_id, thread_id, user_id
        )

    def unsubscribe(
        self, task_id: str, platform: str, chat_id: str, *, thread_id: str | None = None
    ) -> bool:
        return self._delete_subscription(task_id, platform, chat_id, thread_id)

    def search(
        self,
        query: str,
        *,
        tenant_slug: str | None = None,
        limit: int = 20,
    ) -> list[Task]:
        if not query.strip():
            return []
        return self._search(query, tenant_slug=tenant_slug, limit=limit)

    # -- hooks
    def _insert_tenant(self, slug, name, description, icon, color) -> int:
        raise NotImplementedError

    def _fetch_tenants(self) -> list[Tenant]:
        raise NotImplementedError

    def _insert_task(
        self, task_id, tenant_slug, title, body, status, priority,
        assignee, parent_id, tags, skills_json,
    ) -> None:
        raise NotImplementedError

    def _fetch_tasks(self, tenant_slug, *, status, assignee, limit) -> list[Task]:
        raise NotImplementedError

    def _fetch_task(self, task_id) -> Task | None:
        raise NotImplementedError

    def _claim_next(self, assignee, max_runtime_seconds) -> Task | None:
        raise NotImplementedError

    def _heartbeat(self, task_id) -> bool:
        raise NotImplementedError

    def _complete_task(self, task_id, summary, result) -> bool:
        raise NotImplementedError

    def _fail_task(self, task_id, error, status) -> bool:
        raise NotImplementedError

    def _insert_comment(self, task_id, body, author) -> int:
        raise NotImplementedError

    def _fetch_history(self, task_id, limit) -> list[Event]:
        raise NotImplementedError

    def _insert_link(self, parent_id, child_id) -> bool:
        raise NotImplementedError

    def _fetch_children(self, parent_id) -> list[Task]:
        raise NotImplementedError

    def _fetch_parents(self, child_id) -> list[Task]:
        raise NotImplementedError

    def _insert_subscription(self, task_id, platform, chat_id, thread_id, user_id) -> bool:
        raise NotImplementedError

    def _delete_subscription(self, task_id, platform, chat_id, thread_id) -> bool:
        raise NotImplementedError

    def _search(self, query, *, tenant_slug, limit) -> list[Task]:
        raise NotImplementedError
