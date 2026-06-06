"""Tool wrappers — the agent-facing layer over the repos.

These are thin shims that:
  - Validate input types
  - Convert repo exceptions to user-readable tool errors
  - Format responses as JSON strings (for hermes-agent's tool system)
  - Hold a reference to the active repo instance

The plugin loader calls `register(ctx)` to register these as tools
via `ctx.register_tool(name, fn)`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from hermes_memory.repos.journal_repo import JournalRepo
from hermes_memory.repos.kanban_repo import KanbanRepo
from hermes_memory.repos.memory_repo import (
    MemoryNotFoundError,
    MemoryRepo,
    RoutingRuleViolationError,
)
from hermes_memory.repos.metrics_repo import MetricsRepo
from hermes_memory.repos.observability_repo import ObservabilityRepo
from hermes_memory.repos.sessions_repo import SessionsRepo
from hermes_memory.repos.skills_repo import SkillsRepo
from hermes_memory.repos.wiki_repo import WikiRepo


def _json(obj: Any) -> str:
    """JSON-serialize with default coercion for dataclasses."""
    def default(o):
        if hasattr(o, "__dict__"):
            return o.__dict__
        return str(o)
    return json.dumps(obj, default=default, indent=2)


def _tool_error(message: str, **extra: Any) -> str:
    """Format a tool error response. Matches the hermes-agent convention.

    `message` is the human-readable error string. Extra kwargs become
    top-level fields in the JSON response (e.g. `error_kind="not_found"`).
    """
    payload = {"error": message, **extra}
    return json.dumps(payload, indent=2)


# ---------------------------------------------------------------------------
# Memory tools
# ---------------------------------------------------------------------------
def make_memory_tools(repo: MemoryRepo) -> dict[str, Callable]:
    def memory_remember(
        content: str,
        tags: list[str] | None = None,
        category: str | None = None,
        source: str | None = None,
    ) -> str:
        try:
            mid = repo.remember(content, tags=tags, category=category, source=source)
            if mid == 0:
                return "Memory already exists (deduped on (content, source))"
            return f"Stored memory {mid}"
        except RoutingRuleViolationError as e:
            return _tool_error("routing_rule_violation", error_message=str(e))
        except ValueError as e:
            return _tool_error("validation_error", error_message=str(e))

    def memory_search(
        query: str, top_k: int = 10, hybrid_text_weight: float = 0.5
    ) -> str:
        try:
            hits = repo.search(query, top_k=top_k, hybrid_text_weight=hybrid_text_weight)
            return _json([h.__dict__ for h in hits])
        except ValueError as e:
            return _tool_error("validation_error", error_message=str(e))

    def memory_forget(memory_id: int) -> str:
        try:
            ok = repo.forget(memory_id)
            return f"Forgot memory {memory_id}" if ok else f"Memory {memory_id} not found or already deleted"
        except MemoryNotFoundError as e:
            return _tool_error("not_found", error_message=str(e))

    def memory_status() -> str:
        return _json(repo.status())

    return {
        "memory_remember": memory_remember,
        "memory_search": memory_search,
        "memory_forget": memory_forget,
        "memory_status": memory_status,
    }


# ---------------------------------------------------------------------------
# Wiki tools
# ---------------------------------------------------------------------------
def make_wiki_tools(repo: WikiRepo) -> dict[str, Callable]:
    def wiki_create(
        slug: str,
        title: str,
        body_md: str,
        category: str | None = None,
        tags: list[str] | None = None,
    ) -> str:
        try:
            did = repo.create(slug, title, body_md, category=category, tags=tags)
            return f"Created wiki document {slug} (id={did})"
        except ValueError as e:
            return _tool_error("validation_error", error_message=str(e))

    def wiki_read(slug: str) -> str:
        d = repo.read(slug)
        if d is None:
            return _tool_error("not_found", error_message=f"wiki doc '{slug}' not found")
        return _json(d.__dict__)

    def wiki_link(source_slug: str, target_slug: str, context: str | None = None) -> str:
        ok = repo.link(source_slug, target_slug, context=context)
        return f"Linked {source_slug} → {target_slug}" if ok else _tool_error(
            "link_failed",
            message=f"could not link {source_slug} → {target_slug} "
                    f"(missing slug or self-link?)",
        )

    def wiki_backlinks(target_slug: str) -> str:
        return _json([d.__dict__ for d in repo.backlinks(target_slug)])

    def wiki_related(slug: str, max_hops: int = 2) -> str:
        try:
            return _json([d.__dict__ for d in repo.related(slug, max_hops=max_hops)])
        except ValueError as e:
            return _tool_error("validation_error", error_message=str(e))

    def wiki_search(query: str, top_k: int = 10) -> str:
        return _json([d.__dict__ for d in repo.search(query, top_k=top_k)])

    return {
        "wiki_create": wiki_create,
        "wiki_read": wiki_read,
        "wiki_link": wiki_link,
        "wiki_backlinks": wiki_backlinks,
        "wiki_related": wiki_related,
        "wiki_search": wiki_search,
    }


# ---------------------------------------------------------------------------
# Journal tools
# ---------------------------------------------------------------------------
def make_journal_tools(repo: JournalRepo) -> dict[str, Callable]:
    def journal_log_session(profile: str, metadata: dict[str, Any] | None = None) -> str:
        try:
            sid = repo.log_session(profile, metadata=metadata)
            return f"Logged session {sid} (profile={profile})"
        except ValueError as e:
            return _tool_error("validation_error", error_message=str(e))

    def journal_log_message(
        session_id: int,
        role: str,
        content: str,
        tool_calls: dict[str, Any] | None = None,
    ) -> str:
        try:
            mid = repo.log_message(session_id, role, content, tool_calls=tool_calls)
            return f"Logged message {mid} (session={session_id}, role={role})"
        except ValueError as e:
            return _tool_error("validation_error", error_message=str(e))

    def journal_search(
        query: str,
        top_k: int = 10,
        session_id: int | None = None,
        role: str | None = None,
    ) -> str:
        return _json([
            m.__dict__ for m in repo.search(
                query, top_k=top_k, session_id=session_id, role=role
            )
        ])

    return {
        "journal_log_session": journal_log_session,
        "journal_log_message": journal_log_message,
        "journal_search": journal_search,
    }


# ---------------------------------------------------------------------------
# Skills tools
# ---------------------------------------------------------------------------
def make_skills_tools(repo: SkillsRepo) -> dict[str, Callable]:
    def skill_index_search(query: str, top_k: int = 20) -> str:
        return _json([s.__dict__ for s in repo.search(query, top_k=top_k)])

    def skill_register(
        name: str,
        version: str,
        description: str = "",
        owner: str | None = None,
        tags: list[str] | None = None,
    ) -> str:
        try:
            ok = repo.register(
                name, version, description=description, owner=owner, tags=tags
            )
            return f"Registered {name}@{version}" if ok else f"{name}@{version} already registered"
        except ValueError as e:
            return _tool_error("validation_error", error_message=str(e))

    def skill_link(source: str, target: str, kind: str) -> str:
        try:
            ok = repo.link(source, target, kind)  # type: ignore[arg-type]
            return f"Linked {source} -[{kind}]-> {target}" if ok else _tool_error(
                "link_failed",
                message=f"could not link {source} -[{kind}]-> {target}",
            )
        except ValueError as e:
            return _tool_error("validation_error", error_message=str(e))

    def skill_graph(root: str, max_hops: int = 2) -> str:
        try:
            return _json(repo.graph(root, max_hops=max_hops))
        except ValueError as e:
            return _tool_error("validation_error", error_message=str(e))

    return {
        "skill_index_search": skill_index_search,
        "skill_register": skill_register,
        "skill_link": skill_link,
        "skill_graph": skill_graph,
    }


# ---------------------------------------------------------------------------
# Metrics tools
# ---------------------------------------------------------------------------
def make_metrics_tools(repo: MetricsRepo) -> dict[str, Callable]:
    def metrics_record(
        profile: str,
        name: str,
        value: float,
        tags: dict[str, str] | None = None,
    ) -> str:
        try:
            eid = repo.record(profile, name, value, tags=tags)
            return f"Recorded metric {eid} ({name}={value})"
        except ValueError as e:
            return _tool_error("validation_error", error_message=str(e))

    def metrics_query(
        profile: str,
        name: str,
        from_ts: str | None = None,
        to_ts: str | None = None,
        bucket: str = "1 minute",
    ) -> str:
        from datetime import datetime
        from_ts_dt = datetime.fromisoformat(from_ts) if from_ts else None
        to_ts_dt = datetime.fromisoformat(to_ts) if to_ts else None
        return _json([
            p.__dict__ for p in repo.query(
                profile, name, from_ts=from_ts_dt, to_ts=to_ts_dt, bucket=bucket
            )
        ])

    return {
        "metrics_record": metrics_record,
        "metrics_query": metrics_query,
    }


# ---------------------------------------------------------------------------
# Kanban tools
# ---------------------------------------------------------------------------
def make_kanban_tools(repo: KanbanRepo) -> dict[str, Callable]:
    def kanban_tenant_create(
        slug: str,
        name: str,
        description: str = "",
        icon: str = "",
        color: str = "",
    ) -> str:
        try:
            tid = repo.tenant_create(slug, name, description=description, icon=icon, color=color)
            return f"Created tenant {slug} (id={tid})"
        except ValueError as e:
            return _tool_error("validation_error", error_message=str(e))

    def kanban_tenants() -> str:
        return _json([t.__dict__ for t in repo.list_tenants()])

    def kanban_create(
        tenant_slug: str,
        title: str,
        body: str = "",
        priority: int = 0,
        assignee: str | None = None,
        parent_id: str | None = None,
        tags: list[str] | None = None,
        skills_json: str | None = None,
    ) -> str:
        try:
            tid = repo.create(
                tenant_slug, title, body=body, priority=priority,
                assignee=assignee, parent_id=parent_id, tags=tags,
                skills_json=skills_json,
            )
            return f"Created task {tid} (tenant={tenant_slug})"
        except ValueError as e:
            return _tool_error("validation_error", error_message=str(e))

    def kanban_list(
        tenant_slug: str,
        status: str | None = None,
        assignee: str | None = None,
        limit: int = 100,
    ) -> str:
        try:
            return _json([t.__dict__ for t in repo.list(
                tenant_slug, status=status, assignee=assignee, limit=limit
            )])
        except ValueError as e:
            return _tool_error("validation_error", error_message=str(e))

    def kanban_get(task_id: str) -> str:
        t = repo.get(task_id)
        if t is None:
            return _tool_error("not_found", error_message=f"task {task_id} not found")
        return _json(t.__dict__)

    def kanban_claim(assignee: str, max_runtime_seconds: int = 0) -> str:
        try:
            t = repo.claim(assignee, max_runtime_seconds=max_runtime_seconds)
            if t is None:
                return "No ready tasks to claim"
            return _json(t.__dict__)
        except ValueError as e:
            return _tool_error("validation_error", error_message=str(e))

    def kanban_heartbeat(task_id: str) -> str:
        try:
            ok = repo.heartbeat(task_id)
            return f"Heartbeat ok: {task_id}" if ok else _tool_error(
                "not_found", error_message=f"task {task_id} not found"
            )
        except ValueError as e:
            return _tool_error("validation_error", error_message=str(e))

    def kanban_complete(task_id: str, summary: str, result: str | None = None) -> str:
        try:
            ok = repo.complete(task_id, summary, result=result)
            return f"Completed {task_id}" if ok else _tool_error(
                "complete_failed", error_message=f"could not complete {task_id}"
            )
        except ValueError as e:
            return _tool_error("validation_error", error_message=str(e))

    def kanban_fail(task_id: str, error: str, status: str = "failed") -> str:
        try:
            ok = repo.fail(task_id, error, status=status)  # type: ignore[arg-type]
            return f"Failed {task_id} (status={status})" if ok else _tool_error(
                "fail_failed", error_message=f"could not fail {task_id}"
            )
        except ValueError as e:
            return _tool_error("validation_error", error_message=str(e))

    def kanban_comment(task_id: str, body: str, author: str | None = None) -> str:
        try:
            cid = repo.comment(task_id, body, author=author)
            return f"Comment {cid} on {task_id}"
        except ValueError as e:
            return _tool_error("validation_error", error_message=str(e))

    def kanban_history(task_id: str, limit: int = 100) -> str:
        return _json([e.__dict__ for e in repo.history(task_id, limit=limit)])

    def kanban_link(parent_id: str, child_id: str) -> str:
        ok = repo.link(parent_id, child_id)
        return f"Linked {parent_id} → {child_id}" if ok else _tool_error(
            "link_failed", error_message=f"could not link {parent_id} → {child_id}"
        )

    def kanban_children(parent_id: str) -> str:
        return _json([t.__dict__ for t in repo.children(parent_id)])

    def kanban_parents(child_id: str) -> str:
        return _json([t.__dict__ for t in repo.parents(child_id)])

    def kanban_subscribe(
        task_id: str, platform: str, chat_id: str,
        thread_id: str | None = None, user_id: str | None = None,
    ) -> str:
        ok = repo.subscribe(
            task_id, platform, chat_id, thread_id=thread_id, user_id=user_id
        )
        return f"Subscribed {platform}:{chat_id} to {task_id}" if ok else _tool_error(
            "subscribe_failed", error_message=f"could not subscribe to {task_id}"
        )

    def kanban_unsubscribe(
        task_id: str, platform: str, chat_id: str, thread_id: str | None = None
    ) -> str:
        ok = repo.unsubscribe(task_id, platform, chat_id, thread_id=thread_id)
        return f"Unsubscribed {platform}:{chat_id} from {task_id}" if ok else _tool_error(
            "unsubscribe_failed", error_message=f"could not unsubscribe from {task_id}"
        )

    def kanban_search(
        query: str, tenant_slug: str | None = None, limit: int = 20
    ) -> str:
        return _json([
            t.__dict__ for t in repo.search(query, tenant_slug=tenant_slug, limit=limit)
        ])

    return {
        "kanban_tenant_create": kanban_tenant_create,
        "kanban_tenants": kanban_tenants,
        "kanban_create": kanban_create,
        "kanban_list": kanban_list,
        "kanban_get": kanban_get,
        "kanban_claim": kanban_claim,
        "kanban_heartbeat": kanban_heartbeat,
        "kanban_complete": kanban_complete,
        "kanban_fail": kanban_fail,
        "kanban_comment": kanban_comment,
        "kanban_history": kanban_history,
        "kanban_link": kanban_link,
        "kanban_children": kanban_children,
        "kanban_parents": kanban_parents,
        "kanban_subscribe": kanban_subscribe,
        "kanban_unsubscribe": kanban_unsubscribe,
        "kanban_search": kanban_search,
    }


# ---------------------------------------------------------------------------
# Observability tools
# ---------------------------------------------------------------------------
def make_observability_tools(repo: ObservabilityRepo) -> dict[str, Callable]:
    def obs_log(
        level: str,
        message: str,
        profile: str = "default",
        fields: dict[str, Any] | None = None,
    ) -> str:
        try:
            eid = repo.log(level, message, profile=profile, fields=fields)  # type: ignore[arg-type]
            return f"Logged event {eid}"
        except ValueError as e:
            return _tool_error("validation_error", error_message=str(e))

    def obs_record_llm(
        profile: str, model: str,
        prompt_tokens: int, completion_tokens: int, duration_ms: int,
        status: str = "ok",
    ) -> str:
        try:
            eid = repo.record_llm_call(
                profile, model, prompt_tokens, completion_tokens,
                duration_ms, status=status,
            )
            return f"Recorded LLM call {eid}"
        except ValueError as e:
            return _tool_error("validation_error", error_message=str(e))

    def obs_record_tool(
        profile: str, tool: str, duration_ms: int,
        status: str = "ok", error: str | None = None,
    ) -> str:
        try:
            eid = repo.record_tool_call(
                profile, tool, duration_ms, status=status, error=error
            )
            return f"Recorded tool call {eid}"
        except ValueError as e:
            return _tool_error("validation_error", error_message=str(e))

    def obs_flush() -> str:
        n = repo.flush()
        return f"Flushed {n} events"

    return {
        "obs_log": obs_log,
        "obs_record_llm": obs_record_llm,
        "obs_record_tool": obs_record_tool,
        "obs_flush": obs_flush,
    }


# ---------------------------------------------------------------------------
# Sessions tools
# ---------------------------------------------------------------------------
def make_sessions_tools(repo: SessionsRepo) -> dict[str, Callable]:
    def session_open(profile: str, metadata: dict[str, Any] | None = None) -> str:
        try:
            sid = repo.open_session(profile, metadata=metadata)
            return f"Opened session {sid} (profile={profile})"
        except ValueError as e:
            return _tool_error("validation_error", error_message=str(e))

    def session_append(
        session_id: int, role: str, content: str,
        tool_calls: dict[str, Any] | None = None,
    ) -> str:
        try:
            mid = repo.append_message(session_id, role, content, tool_calls=tool_calls)
            return f"Appended message {mid} (session={session_id}, role={role})"
        except ValueError as e:
            return _tool_error("validation_error", error_message=str(e))

    def session_messages(
        session_id: int, limit: int = 100, since: str | None = None
    ) -> str:
        from datetime import datetime
        since_dt = datetime.fromisoformat(since) if since else None
        try:
            return _json([
                m.__dict__ for m in repo.get_messages(
                    session_id, limit=limit, since=since_dt
                )
            ])
        except ValueError as e:
            return _tool_error("validation_error", error_message=str(e))

    def session_lock_acquire(
        session_id: int, holder: str, ttl_seconds: int = 300
    ) -> str:
        try:
            ok = repo.acquire_compression_lock(
                session_id, holder, ttl_seconds=ttl_seconds
            )
            return f"Lock acquired by {holder}" if ok else _tool_error(
                "lock_busy", error_message=f"session {session_id} already locked"
            )
        except ValueError as e:
            return _tool_error("validation_error", error_message=str(e))

    def session_lock_release(session_id: int, holder: str) -> str:
        ok = repo.release_compression_lock(session_id, holder)
        return f"Lock released by {holder}" if ok else _tool_error(
            "lock_not_held", error_message=f"holder {holder} does not own session {session_id} lock"
        )

    def session_close(session_id: int) -> str:
        ok = repo.close_session(session_id)
        return f"Closed session {session_id}" if ok else _tool_error(
            "not_found", error_message=f"session {session_id} not found"
        )

    return {
        "session_open": session_open,
        "session_append": session_append,
        "session_messages": session_messages,
        "session_lock_acquire": session_lock_acquire,
        "session_lock_release": session_lock_release,
        "session_close": session_close,
    }


# ---------------------------------------------------------------------------
# All tools in one place — for the register() function
# ---------------------------------------------------------------------------
def make_all_tools(repos: dict[str, Any]) -> dict[str, Callable]:
    """Build every tool from a dict of {surface: repo_instance}.

    `repos` keys: memory, wiki, journal, skills, metrics, kanban,
    observability, sessions. Missing repos mean those tools aren't
    registered (useful for partial installs).
    """
    out: dict[str, Callable] = {}
    if repos.get("memory") is not None:
        out.update(make_memory_tools(repos["memory"]))
    if repos.get("wiki") is not None:
        out.update(make_wiki_tools(repos["wiki"]))
    if repos.get("journal") is not None:
        out.update(make_journal_tools(repos["journal"]))
    if repos.get("skills") is not None:
        out.update(make_skills_tools(repos["skills"]))
    if repos.get("metrics") is not None:
        out.update(make_metrics_tools(repos["metrics"]))
    if repos.get("kanban") is not None:
        out.update(make_kanban_tools(repos["kanban"]))
    if repos.get("observability") is not None:
        out.update(make_observability_tools(repos["observability"]))
    if repos.get("sessions") is not None:
        out.update(make_sessions_tools(repos["sessions"]))
    return out
