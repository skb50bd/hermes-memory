"""Hermes-agent plugin entry point for hermes-memory.

This file IS the plugin — `register(ctx)` is what hermes-agent's
plugin loader calls. It wires up:
  1. The 46 in-process tools (one entry per repo)
  2. The memory tool override (issue #8)
  3. Hooks (on_session_end for stats flush, pre_tool_call for routing)

The repos are constructed in `register()` based on the env config
(HERMES_PG_CONN_STR). If a repo can't be constructed (e.g. PG is
down), the corresponding tools are skipped and a warning is logged.
"""

from __future__ import annotations

import inspect
import logging
import os
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


# Minimal JSON-schema fragment the hermes-agent tool registry requires.
# We let each tool function's signature drive its schema; this is a
# permissive default that the agent can call with kwargs.
def _passthrough_schema(name: str, description: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "additionalProperties": True,
        },
    }


# Tool descriptions — short, tells the agent when to reach for the tool.
# (name, description) tuples
_MEMORY_DESCRIPTIONS = {
    "memory_remember": "Store a memory. Idempotent on (content, source). Returns the memory id (0 = duplicate).",
    "memory_search": "Hybrid FTS + vector search over memories. Empty result is valid — try a rephrasing.",
    "memory_forget": "Soft-delete a memory by id. Sets deleted_at; row is excluded from search.",
    "memory_status": "Memory table stats + embedder cache stats.",
}


def register(ctx) -> None:
    """Plugin entry point. Called by hermes-agent's plugin loader."""
    from hermes_memory.override import build_memory_block, memory_tool
    from hermes_memory.tools import make_all_tools

    repos = _try_build_repos()
    memory_repo = repos.get("memory")

    # 1) Register all surface tools
    tools = make_all_tools(repos)
    for name, fn in tools.items():
        ctx.register_tool(
            name=name,
            toolset="hermes_postgres_memory",
            schema=_passthrough_schema(name, _docstring_or_default(fn, name)),
            handler=fn,
        )

    # 2) Override the built-in `memory` tool (issue #8).
    # hermes-agent's `register_tool` already supports `override=True`
    # (per tools/registry.py:247), so no upstream PR is required.
    def _memory_tool_wrapper(**kwargs):
        return memory_tool(
            action=kwargs.pop("action", None),
            **kwargs,
            repo=memory_repo,
        )

    ctx.register_tool(
        name="memory",
        toolset="hermes_postgres_memory",
        schema=_passthrough_schema(
            "memory",
            "Override of the built-in memory tool. Backed by PostgreSQL "
            "when memory.provider=postgres; otherwise by ~/.hermes/memories/MEMORY.md. "
            "Accepts action=add|replace|remove|search|list, plus content, "
            "memory_id, query, top_k, tags, category, source.",
        ),
        handler=_memory_tool_wrapper,
        override=True,
    )
    logger.info("hermes-memory: 'memory' tool override registered (issue #8 fix)")

    # 3) Hooks
    if hasattr(ctx, "register_hook"):
        def _on_session_end(**_):
            try:
                obs = repos.get("observability")
                if obs is not None:
                    obs.flush()
            except Exception as e:  # noqa: BLE001
                logger.debug("hermes-memory: on_session_end flush failed: %s", e)
        ctx.register_hook("on_session_end", _on_session_end)

        def _pre_tool_call(tool_name: str, **kwargs):
            """Pre-hook: source the system prompt MEMORY block from PG."""
            if tool_name in ("system_prompt_refresh", "memory") and memory_repo is not None:
                return {"memory_block": build_memory_block(memory_repo)}
            return None
        ctx.register_hook("pre_tool_call", _pre_tool_call)

    logger.info(
        "hermes-memory: registered %d tools + memory override (repos: %s)",
        len(tools) + 1,
        ", ".join(sorted(repos.keys())) or "none",
    )


def _docstring_or_default(fn: Callable, name: str) -> str:
    """Use the function's docstring as the tool description; fallback to
    the name."""
    doc = inspect.getdoc(fn)
    if doc:
        first_line = doc.strip().split("\n", 1)[0]
        return first_line
    return f"hermes-memory tool: {name}"


def _try_build_repos() -> dict[str, Any]:
    """Build repos from env config. Returns a dict of {surface: repo}.

    Surfaces whose repo can't be built are omitted (not set to None)
    so the tools layer can detect "missing" cleanly.
    """
    try:
        from hermes_memory.embeddings import EmbedderRegistry
        from hermes_memory.pg_repos import (
            PgJournalRepo,
            PgKanbanRepo,
            PgMemoryRepo,
            PgMetricsRepo,
            PgObservabilityRepo,
            PgSessionsRepo,
            PgSkillsRepo,
            PgWikiRepo,
        )
    except ImportError as e:
        logger.warning("hermes-memory: pg_repos import failed: %s", e)
        return {}

    dsn = os.environ.get("HERMES_PG_CONN_STR") or os.environ.get("PG_MEM_DB_CONN_STR")
    if not dsn:
        logger.warning(
            "hermes-memory: HERMES_PG_CONN_STR not set; no repos constructed. "
            "Run `hermes-memory install` first."
        )
        return {}

    embedders = EmbedderRegistry.from_env()
    repos: dict[str, Any] = {}

    for name, cls in [
        ("memory", PgMemoryRepo),
        ("wiki", PgWikiRepo),
        ("journal", PgJournalRepo),
        ("skills", PgSkillsRepo),
        ("metrics", PgMetricsRepo),
        ("kanban", PgKanbanRepo),
        ("observability", PgObservabilityRepo),
        ("sessions", PgSessionsRepo),
    ]:
        try:
            if name == "memory":
                repos[name] = cls(dsn, embedders=embedders)
            else:
                repos[name] = cls(dsn)
        except Exception as e:
            logger.warning("hermes-memory: %s repo construction failed: %s", name, e)
    return repos
