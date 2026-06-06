"""Hermes-agent plugin entry point for hermes-memory.

This file IS the plugin — `register(ctx)` is what hermes-agent's
plugin loader calls. It wires up:
  1. The 35+ tools (one entry per repo)
  2. The memory tool override (issue #8)
  3. Hooks (on_session_end for stats flush, pre_tool_call for routing)
  4. The override_builtin flag for the `memory` tool

The repos are constructed in `register()` based on the env config
(HERMES_PG_CONN_STR). If a repo can't be constructed (e.g. PG is
down), the corresponding tools are skipped and a warning is logged.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Tool lists for the manifest
ALL_TOOL_NAMES = [
    # memory
    "memory_remember", "memory_search", "memory_forget", "memory_status",
    # wiki
    "wiki_create", "wiki_read", "wiki_link", "wiki_backlinks",
    "wiki_related", "wiki_search",
    # journal
    "journal_log_session", "journal_log_message", "journal_search",
    # skills
    "skill_index_search", "skill_register", "skill_link", "skill_graph",
    # metrics
    "metrics_record", "metrics_query",
    # kanban (17)
    "kanban_tenant_create", "kanban_tenants", "kanban_create", "kanban_list",
    "kanban_get", "kanban_claim", "kanban_heartbeat", "kanban_complete",
    "kanban_fail", "kanban_comment", "kanban_history", "kanban_link",
    "kanban_children", "kanban_parents", "kanban_subscribe",
    "kanban_unsubscribe", "kanban_search",
    # observability
    "obs_log", "obs_record_llm", "obs_record_tool", "obs_flush",
    # sessions
    "session_open", "session_append", "session_messages",
    "session_lock_acquire", "session_lock_release", "session_close",
]


def _try_build_repos() -> dict[str, Any]:
    """Build repos from env config. Returns a dict of {surface: repo}.

    Surfaces whose repo can't be built are omitted (not set to None)
    so the tools layer can detect "missing" cleanly.
    """
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

    dsn = os.environ.get("HERMES_PG_CONN_STR") or os.environ.get("PG_MEM_DB_CONN_STR")
    if not dsn:
        logger.warning(
            "hermes-memory: HERMES_PG_CONN_STR not set; "
            "no repos will be constructed. Run `hermes-memory install` first."
        )
        return {}

    embedders = EmbedderRegistry.from_env()
    repos: dict[str, Any] = {}

    try:
        repos["memory"] = PgMemoryRepo(dsn, embedders=embedders)
    except Exception as e:
        logger.warning("hermes-memory: PgMemoryRepo failed: %s", e)
    try:
        repos["wiki"] = PgWikiRepo(dsn, embedders=embedders)
    except Exception as e:
        logger.warning("hermes-memory: PgWikiRepo failed: %s", e)
    try:
        repos["journal"] = PgJournalRepo(dsn)
    except Exception as e:
        logger.warning("hermes-memory: PgJournalRepo failed: %s", e)
    try:
        repos["skills"] = PgSkillsRepo(dsn)
    except Exception as e:
        logger.warning("hermes-memory: PgSkillsRepo failed: %s", e)
    try:
        repos["metrics"] = PgMetricsRepo(dsn)
    except Exception as e:
        logger.warning("hermes-memory: PgMetricsRepo failed: %s", e)
    try:
        repos["kanban"] = PgKanbanRepo(dsn)
    except Exception as e:
        logger.warning("hermes-memory: PgKanbanRepo failed: %s", e)
    try:
        repos["observability"] = PgObservabilityRepo(dsn)
    except Exception as e:
        logger.warning("hermes-memory: PgObservabilityRepo failed: %s", e)
    try:
        repos["sessions"] = PgSessionsRepo(dsn)
    except Exception as e:
        logger.warning("hermes-memory: PgSessionsRepo failed: %s", e)
    return repos


def register(ctx) -> None:
    """Plugin entry point. Called by hermes-agent's plugin loader.

    Wires up:
      - All 35+ tools via ctx.register_tool()
      - The 'memory' tool override (issue #8)
      - Hooks for routing, system-prompt refresh, and session-end flush
    """
    from hermes_memory.override import build_memory_block, memory_tool
    from hermes_memory.tools import make_all_tools

    repos = _try_build_repos()
    memory_repo = repos.get("memory")

    # 1) Register all 35+ tools
    tools = make_all_tools(repos)
    for name, fn in tools.items():
        ctx.register_tool(name, fn)

    # 2) Override the built-in 'memory' tool (issue #8)
    # The hermes-agent PR (~10 LOC) adds an `override_builtin` flag to
    # register_tool(). When that lands, the call below works as-is.
    def _memory_tool_wrapper(**kwargs):
        return memory_tool(
            action=kwargs.pop("action", None),
            **kwargs,
            repo=memory_repo,
        )

    try:
        ctx.register_tool("memory", _memory_tool_wrapper, override_builtin=True)
        logger.info("hermes-memory: 'memory' tool override registered (issue #8 fix)")
    except TypeError:
        # Older hermes-agent without the override_builtin flag.
        # Fall back to registering with a different name and warning
        # the user; the built-in 'memory' tool remains in place.
        ctx.register_tool("memory_postgres", _memory_tool_wrapper)
        logger.warning(
            "hermes-memory: hermes-agent is missing the override_builtin flag "
            "in register_tool(). The built-in 'memory' tool is unchanged; "
            "the override is available as 'memory_postgres'."
        )

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
            if tool_name in ("system_prompt_refresh", "memory"):
                if memory_repo is not None:
                    return {"memory_block": build_memory_block(memory_repo)}
            return None
        ctx.register_hook("pre_tool_call", _pre_tool_call)

    logger.info(
        "hermes-memory: registered %d tools + memory override (repos: %s)",
        len(tools) + 1,
        ", ".join(sorted(repos.keys())) or "none",
    )
