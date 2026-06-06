"""Memory repository — Postgres + chunked embeddings (issue #5).

This is the canonical storage layer for agent_memory.memories. The
real implementation talks to Postgres via psycopg3; the
`MemoryRepo` base class is designed to be subclassed for the PG
implementation and for in-memory test fakes.

Public surface (what the override / tools layer calls):
  - remember(content, *, tags, category, source) -> int
  - search(query, *, top_k, hybrid_text_weight) -> list[Memory]
  - forget(memory_id) -> bool
  - status() -> dict

Override hook (for subclasses):
  - _insert_memory(...)
  - _insert_chunks(memory_id, chunks, dim)
  - _search(query_embedding, query_text, *, top_k, hybrid_text_weight)
  - _forget(memory_id) -> bool
  - _status() -> dict
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

# Issue #5: 32 KB raw text cap. Long content is chunked internally.
MEMORY_MAX_CHARS = 32 * 1024


@dataclass
class Memory:
    """A single stored memory. Returned by search/remember.

    Not frozen — the in-memory fake and the PG repo both need to
    flip `deleted` when forget() is called. Use the helper
    `mark_deleted()` so the mutation is named.
    """

    id: int
    content: str
    tags: tuple[str, ...]
    category: str | None
    source: str | None
    embedding_dim: int
    deleted: bool = False
    created_at: str | None = None

    def mark_deleted(self) -> None:
        self.deleted = True


class MemoryNotFoundError(Exception):
    """Raised when forget() targets a non-existent memory."""


class RoutingRuleViolationError(Exception):
    """Raised when content exceeds MEMORY_MAX_CHARS.

    The error message itself documents the memory-vs-wiki routing
    rule — that's how the agent learns the boundary (issue #5
    acceptance criterion).
    """


def routing_rule_message(*, size_bytes: int, cap: int) -> str:
    """The canonical routing-rule error message.

    Quoting the rule:
        • MEMORY  — short, durable facts (< 1 screen). Stored via
                    memory_remember. Surface: system prompt + searches.
        • WIKI    — long-form, structured, multi-paragraph. Stored via
                    wiki_create. Surface: explicit reads, cross-linked.
        • SESSION — never persist; use session_search.

    Did you mean: wiki_create with category="projects.<name>"?
    """
    return (
        f"Memory size {size_bytes:,} chars exceeds the {cap:,}-char cap "
        f"(MEMORY_MAX_CHARS).\n\n"
        f"Routing rule:\n"
        f"  • MEMORY  — short, durable facts (< 1 screen). Stored via\n"
        f"             memory_remember. Surface: system prompt + searches.\n"
        f"  • WIKI    — long-form, structured, multi-paragraph. Stored via\n"
        f"             wiki_create. Surface: explicit reads, cross-linked.\n"
        f"  • SESSION — never persist; use session_search.\n\n"
        f'Did you mean: wiki_create with category="projects.<name>"?'
    )


class MemoryRepo:
    """Base class. Subclasses implement the _insert/_search/_forget hooks."""

    #: Default vector dim. bge-m3 → 1024. Override per-installation.
    default_dim: int = 1024

    def remember(
        self,
        content: str,
        *,
        tags: Sequence[str] | None = None,
        category: str | None = None,
        source: str | None = None,
    ) -> int:
        """Store a memory. Returns the new memory id, or 0 if duplicate.

        Content > MEMORY_MAX_CHARS raises RoutingRuleViolationError
        (issue #5: hard cap with routing-rule message).
        """
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must be a non-empty string")
        if len(content) > MEMORY_MAX_CHARS:
            raise RoutingRuleViolationError(
                routing_rule_message(size_bytes=len(content), cap=MEMORY_MAX_CHARS)
            )
        return self._insert_memory(
            content,
            tags=list(tags) if tags else [],
            category=category,
            source=source,
            embedding_dim=self.default_dim,
        )

    def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        hybrid_text_weight: float = 0.5,
    ) -> list[Memory]:
        if not isinstance(query, str) or not query.strip():
            return []
        # Embedding is computed by the subclass via _embed_query; for
        # the base class, we pass the text and let the subclass do
        # the FTS / vector dispatch.
        query_embedding = self._embed_query(query)
        return self._search(
            query_embedding,
            query,
            top_k=top_k,
            hybrid_text_weight=hybrid_text_weight,
        )

    def forget(self, memory_id: int) -> bool:
        if not isinstance(memory_id, int) or memory_id <= 0:
            raise MemoryNotFoundError(f"invalid memory id: {memory_id}")
        return self._forget(memory_id)

    def status(self) -> dict[str, Any]:
        return self._status()

    # -- hooks for subclasses
    def _embed_query(self, query: str) -> list[float]:
        raise NotImplementedError

    def _insert_memory(
        self,
        content: str,
        *,
        tags: list[str],
        category: str | None,
        source: str | None,
        embedding_dim: int,
    ) -> int:
        raise NotImplementedError

    def _insert_chunks(self, memory_id: int, chunks: list[Any], dim: int) -> None:
        raise NotImplementedError

    def _search(
        self,
        query_embedding: list[float],
        query_text: str,
        *,
        top_k: int,
        hybrid_text_weight: float,
    ) -> list[Memory]:
        raise NotImplementedError

    def _forget(self, memory_id: int) -> bool:
        raise NotImplementedError

    def _status(self) -> dict[str, Any]:
        raise NotImplementedError
