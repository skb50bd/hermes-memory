"""Override the built-in hermes-agent `memory` tool with the postgres-backed one.

Issue #8: hermes-agent's `tools/memory_tool.py` (the `memory` tool I just
used to read/write `~/.hermes/memories/MEMORY.md`) ignores the
`memory.provider: postgres` config setting and always writes to the
local file store. This module is the in-process replacement.

The plugin loader calls `register(ctx)` to wire this in. The
hermes-agent PR needed is the `override_builtin` flag in
`tools/registry.py` (10 LOC). Until that lands, this module exposes
both the public `pg_remember`/`pg_search`/`pg_forget`/`pg_status`
functions AND a stand-alone `memory` tool implementation that
implements the same JSON-tool contract as the built-in.

Public surface
--------------
- pg_remember(content, *, tags, category, source) -> str
- pg_search(query, *, top_k) -> str
- pg_forget(memory_id) -> str
- pg_status() -> str
- memory_tool(action, **kwargs) -> str      # the built-in 'memory' tool replacement

Configuration
-------------
- memory.provider == "postgres"        : route to postgres
- memory.provider in ("", "local")     : no-op, return local-tool stub
                                        (the original built-in handles it)
- memory.provider == "auto"            : postgres if available, else local
"""

from __future__ import annotations

import json
import os
from typing import Literal

from hermes_memory.repos.memory_repo import (
    MemoryNotFoundError,
    MemoryRepo,
    RoutingRuleViolationError,
)

# Provider constants
PROVIDER_POSTGRES = "postgres"
PROVIDER_LOCAL = "local"
PROVIDER_AUTO = "auto"


def _read_provider() -> str:
    """Read memory.provider from env. Defaults to 'local'."""
    return os.environ.get("MEMORY_PROVIDER", PROVIDER_LOCAL).strip().lower()


def _read_local_store_path() -> str:
    """Path to the local MEMORY.md. Mirrors hermes-agent's default."""
    return os.environ.get(
        "MEMORY_LOCAL_PATH",
        os.path.expanduser("~/.hermes/memories/MEMORY.md"),
    )


# ---------------------------------------------------------------------------
# Postgres-backed functions
# ---------------------------------------------------------------------------
def pg_remember(
    content: str,
    *,
    tags: list[str] | None = None,
    category: str | None = None,
    source: str | None = None,
    repo: MemoryRepo | None = None,
) -> str:
    """Store a memory in postgres. Returns JSON.

    Raises RoutingRuleViolationError for content > 32 KB; the agent
    must catch and route to wiki_create per the routing rule.
    """
    if repo is None:
        return _tool_error(
            "no_repo",
            message="no MemoryRepo wired into this session "
                    "(install the plugin via 'hermes-memory install')",
        )
    mid = repo.remember(content, tags=tags, category=category, source=source)
    if mid == 0:
        return json.dumps({"status": "duplicate", "message": "memory exists (deduped)"})
    return json.dumps({"status": "stored", "id": mid})


def pg_search(
    query: str,
    *,
    top_k: int = 10,
    repo: MemoryRepo | None = None,
) -> str:
    """Hybrid FTS + vector search over memories. Returns JSON."""
    if repo is None:
        return _tool_error("no_repo", message="no MemoryRepo wired in")
    hits = repo.search(query, top_k=top_k)
    return json.dumps(
        {"query": query, "count": len(hits), "results": [h.__dict__ for h in hits]},
        indent=2,
    )


def pg_forget(memory_id: int, *, repo: MemoryRepo | None = None) -> str:
    """Soft-delete a memory. Returns JSON."""
    if repo is None:
        return _tool_error("no_repo", message="no MemoryRepo wired in")
    try:
        ok = repo.forget(memory_id)
        return json.dumps(
            {"status": "forgot" if ok else "not_found", "id": memory_id}
        )
    except MemoryNotFoundError as e:
        return _tool_error("not_found", message=str(e))


def pg_status(*, repo: MemoryRepo | None = None) -> str:
    """Return memory table stats. JSON."""
    if repo is None:
        return _tool_error("no_repo", message="no MemoryRepo wired in")
    return json.dumps(repo.status(), indent=2)


# ---------------------------------------------------------------------------
# The `memory` tool replacement (built-in override)
# ---------------------------------------------------------------------------
def memory_tool(
    action: Literal["add", "replace", "remove", "search", "list"],
    *,
    content: str | None = None,
    memory_id: int | None = None,
    query: str | None = None,
    top_k: int = 10,
    tags: list[str] | None = None,
    category: str | None = None,
    source: str | None = None,
    repo: MemoryRepo | None = None,
    local_path: str | None = None,
) -> str:
    """Replacement for the built-in `memory` tool.

    Mirrors the action-based API of `tools/memory_tool.py`:
      - add      : content
      - replace  : memory_id, content
      - remove   : memory_id
      - search   : query
      - list     : no args (returns all)

    Routes to postgres or local based on `memory.provider`.
    """
    provider = _read_provider()

    # Routing decision
    if provider == PROVIDER_POSTGRES:
        return _route_postgres(
            action, content=content, memory_id=memory_id, query=query,
            top_k=top_k, tags=tags, category=category, source=source, repo=repo,
        )
    if provider == PROVIDER_LOCAL or not provider:
        return _route_local(
            action, content=content, memory_id=memory_id, query=query,
            top_k=top_k, local_path=local_path or _read_local_store_path(),
        )
    if provider == PROVIDER_AUTO:
        # Try postgres; on no_repo, fall back to local
        out = _route_postgres(
            action, content=content, memory_id=memory_id, query=query,
            top_k=top_k, tags=tags, category=category, source=source, repo=repo,
        )
        if '"no_repo"' in out:
            return _route_local(
                action, content=content, memory_id=memory_id, query=query,
                top_k=top_k, local_path=local_path or _read_local_store_path(),
            )
        return out
    return _tool_error("invalid_provider", message=f"unknown provider: {provider}")


def _route_postgres(action, *, content, memory_id, query, top_k, tags, category, source, repo) -> str:
    if action == "add":
        if not content:
            return _tool_error("validation_error", message="add requires 'content'")
        try:
            return pg_remember(
                content, tags=tags, category=category, source=source, repo=repo
            )
        except RoutingRuleViolationError as e:
            return _tool_error("routing_rule_violation", message=str(e))
    if action == "replace":
        if memory_id is None or not content:
            return _tool_error("validation_error", message="replace requires 'memory_id' and 'content'")
        # Soft-delete old, then add new (idempotent at the dedup level)
        pg_forget(memory_id, repo=repo)
        return pg_remember(
            content, tags=tags, category=category, source=source, repo=repo
        )
    if action == "remove":
        if memory_id is None:
            return _tool_error("validation_error", message="remove requires 'memory_id'")
        return pg_forget(memory_id, repo=repo)
    if action == "search":
        if not query:
            return _tool_error("validation_error", message="search requires 'query'")
        return pg_search(query, top_k=top_k, repo=repo)
    if action == "list":
        return pg_status(repo=repo)
    return _tool_error("invalid_action", message=f"unknown action: {action}")


def _route_local(action, *, content, memory_id, query, top_k, local_path) -> str:
    """Local-file fallback. Reads/writes the MEMORY.md bullet list.

    This is a *thin* shim — it doesn't reimplement the full local
    store; it just provides a graceful fallback when postgres is
    unreachable. The real fallback in a hermes-agent install is the
    built-in `memory` tool (which we're not in front of here).
    """
    if action == "add" and content:
        try:
            with open(local_path, "a") as f:
                f.write(f"- {content}\n")
            return json.dumps({"status": "stored", "path": local_path, "mode": "local"})
        except OSError as e:
            return _tool_error("io_error", message=str(e))
    if action == "search" and query:
        try:
            with open(local_path) as f:
                lines = [
                    line.strip() for line in f
                    if query.lower() in line.lower()
                ]
            return json.dumps(
                {"query": query, "count": len(lines[:top_k]), "results": lines[:top_k]},
                indent=2,
            )
        except FileNotFoundError:
            return json.dumps({"query": query, "count": 0, "results": []})
    if action == "list":
        try:
            with open(local_path) as f:
                return json.dumps(
                    {"path": local_path, "lines": f.read().splitlines()},
                    indent=2,
                )
        except FileNotFoundError:
            return json.dumps({"path": local_path, "lines": []})
    return _tool_error(
        "not_implemented_in_local",
        message=f"action {action!r} not supported in local fallback; "
                f"see https://github.com/skb50bd/hermes-memory for the full tool",
    )


# ---------------------------------------------------------------------------
# System-prompt MEMORY block builder (reads from postgres when configured)
# ---------------------------------------------------------------------------
def build_memory_block(
    repo: MemoryRepo | None,
    *,
    char_limit: int = 2200,
) -> str:
    """Build the system-prompt MEMORY block.

    Mirrors the format from `agent/agent_init.py:1096-1108`:
        ════════════════════════════════════════════════
        MEMORY (your personal notes) [95% — 2,098/2,200 chars]
        ════════════════════════════════════════════════
        <bullets>
    """
    if repo is None:
        return "(no memory store wired; using built-in local store)"
    try:
        s = repo.status()
        live = s.get("live_memories", 0)
        # NB: the full text-pulling impl lives in the PG subclass via
        # the system-prompt hook; the base class returns a header + counts.
        header = (
            "═══════════════════════════════════════════════\n"
            "MEMORY (your personal notes) "
            f"[postgres: {live} live memories]\n"
            "═══════════════════════════════════════════════\n"
        )
        return header + f"\n(live: {live}, default_dim: {s.get('default_dim', '?')})\n"
    except Exception as e:  # noqa: BLE001
        return f"(memory block build failed: {e})"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _tool_error(kind: str, *, message: str) -> str:
    return json.dumps({"error": kind, "message": message}, indent=2)
