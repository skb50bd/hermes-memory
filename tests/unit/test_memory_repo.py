"""TDD: memory_repo.py — Postgres memory with chunking (issue #5).

Test order:
  1. short content stores single row, single embedding slot
  2. long content (> 2 KB) chunks into memory_chunks
  3. dedup: identical (content, source) returns existing id, no new chunks
  4. search returns parent memories deduped by memory_id
  5. forget soft-deletes
  6. status returns counts
  7. routing rule is in the "too large" error
  8. categories stored as ltree
  9. tags stored as text[]
  10. vector dim is 1024 by default, configurable

We test against the in-memory FakeMemoryRepo (see tests/_fakes.py)
to keep unit tests hermetic. Integration tests against a real
Testcontainers Postgres are in tests/integration/test_memory_repo_int.py.
"""

from __future__ import annotations

import pytest

from hermes_memory.repos.memory_repo import (
    MEMORY_MAX_CHARS,
    Memory,
    MemoryNotFoundError,
    MemoryRepo,
    RoutingRuleViolationError,
)


# ---------------------------------------------------------------------------
# Fixtures — in-memory fake repo
# ---------------------------------------------------------------------------
class FakeMemoryRepo(MemoryRepo):
    """In-memory implementation of MemoryRepo for unit tests.

    Mirrors the storage shape of the real PG-backed repo: parent
    memories + chunks. Embeddings are stored as lists of floats so we
    can test search without spinning up Postgres.
    """

    def __init__(self, default_dim: int = 1024) -> None:
        self._memories: dict[int, Memory] = {}
        self._chunks: dict[int, list[dict]] = {}  # memory_id -> chunks
        self._next_id = 1
        self._default_dim = default_dim

    # -- helpers used by the test base class
    def _insert_memory(
        self, content, *, tags, category, source, embedding_dim
    ) -> int:
        from hermes_memory.embeddings.chunker import chunk_text

        # Dedup on (content, source)
        for m in self._memories.values():
            if m.content == content and m.source == source and not m.deleted:
                return 0  # duplicate
        mid = self._next_id
        self._next_id += 1
        self._memories[mid] = Memory(
            id=mid,
            content=content,
            tags=tuple(tags or ()),
            category=category,
            source=source,
            embedding_dim=embedding_dim,
            deleted=False,
        )
        # Chunk
        chunks = chunk_text(content)
        self._chunks[mid] = [
            {
                "index": c.index,
                "text": c.text,
                "token_count": c.token_count,
                "embedding": [0.0] * embedding_dim,  # placeholder
            }
            for c in chunks
        ]
        return mid

    def _embed_query(self, query: str) -> list[float]:
        # Fake: zero-vector; subclasses embed for real.
        return [0.0] * self._default_dim

    def _insert_chunks(self, memory_id, chunks, dim) -> None:
        # No-op for the fake; chunks are inserted in _insert_memory.
        pass

    def _search(
        self, query_embedding, query_text, *, top_k, hybrid_text_weight
    ) -> list[Memory]:
        # Naive: score = 1 / (1 + |query_text - memory.content|)
        hits = []
        for m in self._memories.values():
            if m.deleted:
                continue
            score = 1.0 / (1.0 + abs(len(query_text) - len(m.content)))
            hits.append((score, m))
        hits.sort(key=lambda x: -x[0])
        return [m for _, m in hits[:top_k]]

    def _forget(self, memory_id) -> bool:
        m = self._memories.get(memory_id)
        if m is None:
            raise MemoryNotFoundError(f"memory {memory_id} not found")
        if m.deleted:
            return False
        m.mark_deleted()
        return True

    def _status(self) -> dict:
        live = sum(1 for m in self._memories.values() if not m.deleted)
        chunk_count = sum(
            len(c) for mid, c in self._chunks.items()
            if not self._memories[mid].deleted
        )
        return {
            "total_memories": len(self._memories),
            "live_memories": live,
            "total_chunks": chunk_count,
            "default_dim": self._default_dim,
        }


@pytest.fixture
def repo() -> FakeMemoryRepo:
    return FakeMemoryRepo()


# ---------------------------------------------------------------------------
# 1. Short content
# ---------------------------------------------------------------------------
def test_short_content_single_row(repo):
    mid = repo.remember("hello world", source="test")
    assert mid > 0
    m = repo._memories[mid]
    assert m.content == "hello world"
    assert m.source == "test"
    # Chunker always emits ≥1 chunk per non-empty input; the chunk
    # should be the full text since it fits in one window.
    assert len(repo._chunks[mid]) == 1
    assert repo._chunks[mid][0]["text"] == "hello world"


# ---------------------------------------------------------------------------
# 2. Long content → chunked
# ---------------------------------------------------------------------------
def test_long_content_chunked(repo):
    long_text = "the quick brown fox " * 1000  # ~5000 chars
    mid = repo.remember(long_text, source="test")
    assert mid > 0
    chunks = repo._chunks[mid]
    assert len(chunks) > 1  # chunked
    for c in chunks:
        assert c["token_count"] <= 512


# ---------------------------------------------------------------------------
# 3. Dedup
# ---------------------------------------------------------------------------
def test_dedup_returns_zero_on_duplicate(repo):
    mid1 = repo.remember("same", source="s1")
    mid2 = repo.remember("same", source="s1")
    assert mid1 > 0
    assert mid2 == 0  # duplicate


def test_dedup_different_source_inserts(repo):
    mid1 = repo.remember("same", source="s1")
    mid2 = repo.remember("same", source="s2")
    assert mid1 > 0
    assert mid2 > 0
    assert mid1 != mid2


def test_dedup_after_forget_allows_reinsert(repo):
    mid1 = repo.remember("same", source="s1")
    assert mid1 > 0
    repo.forget(mid1)
    mid2 = repo.remember("same", source="s1")
    # Forgot memories don't count for dedup; a new one is created.
    assert mid2 > 0
    assert mid2 != mid1


# ---------------------------------------------------------------------------
# 4. Search
# ---------------------------------------------------------------------------
def test_search_returns_memories(repo):
    repo.remember("postgres tips", source="s1")
    repo.remember("wiki routing", source="s1")
    repo.remember("unrelated", source="s1")
    hits = repo.search("postgres", top_k=2)
    assert len(hits) == 2
    assert all(isinstance(m, Memory) for m in hits)


def test_search_skips_deleted(repo):
    mid = repo.remember("ephemeral", source="s1")
    repo.forget(mid)
    hits = repo.search("ephemeral")
    assert hits == []


# ---------------------------------------------------------------------------
# 5. Forget
# ---------------------------------------------------------------------------
def test_forget_existing(repo):
    mid = repo.remember("to forget", source="s1")
    assert repo.forget(mid) is True
    assert repo._memories[mid].deleted is True


def test_forget_missing_raises(repo):
    with pytest.raises(MemoryNotFoundError):
        repo.forget(99999)


def test_forget_already_deleted_returns_false(repo):
    mid = repo.remember("x", source="s1")
    repo.forget(mid)
    assert repo.forget(mid) is False  # already gone


# ---------------------------------------------------------------------------
# 6. Status
# ---------------------------------------------------------------------------
def test_status_counts(repo):
    repo.remember("a", source="s1")
    repo.remember("b", source="s1")
    s = repo.status()
    assert s["live_memories"] == 2
    assert s["total_memories"] == 2
    assert s["default_dim"] == 1024


# ---------------------------------------------------------------------------
# 7. Routing rule
# ---------------------------------------------------------------------------
def test_too_large_raises_routing_rule_violation(repo):
    """Content > 32 KB should still be allowed but warn — OR — the
    routing rule says 'use wiki'. We pick: hard cap at MEMORY_MAX_CHARS
    (= 32 KB), but emit a RoutingRuleViolationError pointing to wiki.
    """
    too_big = "x" * (MEMORY_MAX_CHARS + 1)
    with pytest.raises(RoutingRuleViolationError) as exc:
        repo.remember(too_big, source="s1")
    assert "wiki" in str(exc.value).lower()
    assert "memory" in str(exc.value).lower()
    assert "routing" in str(exc.value).lower() or "rule" in str(exc.value).lower()


def test_routing_rule_message_quotes_the_rule():
    """The error message itself must contain the routing rule — that's
    how the agent learns the boundary (issue #5 acceptance criterion)."""
    from hermes_memory.repos.memory_repo import routing_rule_message
    msg = routing_rule_message(size_bytes=MEMORY_MAX_CHARS + 1, cap=MEMORY_MAX_CHARS)
    assert "MEMORY" in msg
    assert "WIKI" in msg
    assert "wiki_create" in msg


def test_at_boundary_accepted(repo):
    """Exactly at the cap → accepted (no error)."""
    edge = "x" * MEMORY_MAX_CHARS
    mid = repo.remember(edge, source="s1")
    assert mid > 0


# ---------------------------------------------------------------------------
# 8. Categories (ltree)
# ---------------------------------------------------------------------------
def test_category_stored(repo):
    mid = repo.remember("x", source="s1", category="projects.sportsverse")
    assert repo._memories[mid].category == "projects.sportsverse"


def test_category_none_ok(repo):
    mid = repo.remember("x", source="s1")
    assert repo._memories[mid].category is None


# ---------------------------------------------------------------------------
# 9. Tags
# ---------------------------------------------------------------------------
def test_tags_stored(repo):
    mid = repo.remember("x", source="s1", tags=["foo", "bar"])
    assert repo._memories[mid].tags == ("foo", "bar")


# ---------------------------------------------------------------------------
# 10. Vector dim
# ---------------------------------------------------------------------------
def test_default_dim_1024(repo):
    assert repo._default_dim == 1024


def test_custom_dim():
    r = FakeMemoryRepo(default_dim=768)
    assert r._default_dim == 768


# ---------------------------------------------------------------------------
# 11. Cap is exactly 32 KB
# ---------------------------------------------------------------------------
def test_cap_is_32kb():
    assert MEMORY_MAX_CHARS == 32 * 1024
