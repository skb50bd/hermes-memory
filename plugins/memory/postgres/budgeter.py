"""Memory budgeter: select memories for prompt injection within a char limit.

Deterministic ranking combining:
  - semantic relevance (vector similarity to current query/context)
  - recency (time-decay)
  - category priority (user.profile > project.convention > environment > fact)
  - source priority (mirrored builtin > explicit pg_remember > imported)

Hard char budget with graceful degradation.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Category priority: higher = more important for prompt
_CATEGORY_PRIORITY = {
    "user.profile": 1.0,
    "user.preference": 0.95,
    "project.convention": 0.8,
    "workflow": 0.7,
    "environment": 0.6,
    "tool_quirk": 0.5,
    "lesson_learned": 0.5,
    "fact": 0.3,
}

# Source priority
_SOURCE_PRIORITY = {
    "mirrored": 1.0,
    "builtin": 0.9,
    "explicit": 0.8,
    "imported": 0.5,
}

# Recency half-life in days
_RECENCY_HALF_LIFE_DAYS = 30.0


def _recency_score(created_at_iso: Optional[str]) -> float:
    if not created_at_iso:
        return 0.5
    try:
        created = datetime.fromisoformat(created_at_iso.replace("Z", "+00:00"))
        age_days = (datetime.now(timezone.utc) - created).total_seconds() / 86400
        return math.exp(-age_days / _RECENCY_HALF_LIFE_DAYS)
    except Exception:
        return 0.5


def _category_score(category: Optional[str]) -> float:
    if not category:
        return _CATEGORY_PRIORITY.get("fact", 0.3)
    # Support ltree paths like "user.profile.name"
    parts = category.split(".")
    for i in range(len(parts), 0, -1):
        prefix = ".".join(parts[:i])
        if prefix in _CATEGORY_PRIORITY:
            return _CATEGORY_PRIORITY[prefix]
    return _CATEGORY_PRIORITY.get("fact", 0.3)


def _source_score(tags: Optional[List[str]]) -> float:
    if not tags:
        return _SOURCE_PRIORITY.get("explicit", 0.8)
    for tag in tags:
        if tag in _SOURCE_PRIORITY:
            return _SOURCE_PRIORITY[tag]
    return _SOURCE_PRIORITY.get("explicit", 0.8)


def rank_memories(
    memories: List[Dict],
    *,
    query: Optional[str] = None,
    vector_weight: float = 0.4,
    recency_weight: float = 0.2,
    category_weight: float = 0.2,
    source_weight: float = 0.2,
) -> List[Dict]:
    """Rank memories by composite score. Returns sorted list with 'score' added."""
    scored = []
    for m in memories:
        recency = _recency_score(m.get("created_at"))
        category = _category_score(m.get("category"))
        source = _source_score(m.get("tags"))
        vector_sim = m.get("vector_sim") or 0.0

        score = (
            vector_weight * vector_sim +
            recency_weight * recency +
            category_weight * category +
            source_weight * source
        )
        scored.append({**m, "score": score})

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


def budget_memories(
    memories: List[Dict],
    char_budget: int,
    *,
    entry_separator: str = "\n---\n",
    header: str = "## Relevant Memory Context\n",
    footer: str = "",
) -> str:
    """Select top memories within char budget. Returns formatted string."""
    available = char_budget - len(header) - len(footer)
    if available <= 0:
        return ""

    selected = []
    used = 0
    for m in memories:
        content = m.get("content", "")
        tag_str = f"[{', '.join(m.get('tags', []))}] " if m.get("tags") else ""
        entry = f"{tag_str}{content}"
        sep_len = len(entry_separator) if selected else 0
        if used + sep_len + len(entry) > available:
            break
        used += sep_len + len(entry)
        selected.append(entry)

    if not selected:
        return ""

    return header + entry_separator.join(selected) + footer


def build_memory_block(
    client,
    query: Optional[str] = None,
    char_budget: int = 2200,
    top_k: int = 50,
) -> str:
    """Build a memory block for prompt injection within char_budget.

    If query is provided, does semantic search. Otherwise returns recent
    memories ranked by composite score.
    """
    try:
        if query:
            memories = client.search_memories(query, top_k=top_k)
        else:
            memories = client.get_recent_memories(limit=top_k)

        if not memories:
            return ""

        ranked = rank_memories(memories, query=query)
        return budget_memories(ranked, char_budget)
    except Exception as e:
        logger.warning("Memory budgeter failed: %s", e)
        return ""
