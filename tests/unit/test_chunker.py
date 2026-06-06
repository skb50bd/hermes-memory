"""TDD: chunker.py — 512-token windows with 50-token overlap.

Issue #5: pg_remember needs to chunk content > 2 KB (≈ 512 tokens)
into overlapping windows for embedding. Each chunk gets its own
embedding; the parent memory stays in agent_memory.memories.

Test order (RED → GREEN → REFACTOR):
  1. empty string → no chunks
  2. short string (≤ ~512 tokens) → 1 chunk, full text
  3. exactly at boundary (≈ 512 tokens) → 1 chunk
  4. just over boundary (~513 tokens) → 2 chunks, with overlap
  5. large string (10x boundary) → many chunks, no chunk > window
  6. overlap tokens appear in adjacent chunks
  7. token counting is approximate (1 token ≈ 4 chars English)
  8. custom window/overlap parameters respected
  9. chunks are ordered and non-empty
  10. very long string (32 KB) chunks correctly
"""

from __future__ import annotations

import pytest

from hermes_memory.embeddings.chunker import chunk_text, Chunk


# ---------------------------------------------------------------------------
# 1. Empty input
# ---------------------------------------------------------------------------
def test_empty_string_returns_no_chunks():
    assert chunk_text("") == []


def test_whitespace_only_returns_no_chunks():
    assert chunk_text("   \n\t  \n  ") == []


# ---------------------------------------------------------------------------
# 2. Short input → single chunk
# ---------------------------------------------------------------------------
def test_short_text_single_chunk():
    text = "The quick brown fox jumps over the lazy dog."
    chunks = chunk_text(text)
    assert len(chunks) == 1
    assert chunks[0].text == text
    assert chunks[0].index == 0
    assert chunks[0].token_count > 0
    assert chunks[0].token_count <= 512


# ---------------------------------------------------------------------------
# 3. Boundary case — text ≈ 512 tokens
# ---------------------------------------------------------------------------
def test_text_at_window_boundary_single_chunk():
    # 512 tokens ≈ 2048 chars (heuristic). Use exactly 2048 chars.
    text = "a" * 2048
    chunks = chunk_text(text)
    assert len(chunks) == 1
    assert chunks[0].token_count == 512


def test_text_just_over_boundary_two_chunks():
    # 513 tokens ≈ 2052 chars
    text = "a" * 2052
    chunks = chunk_text(text)
    assert len(chunks) == 2
    assert chunks[0].index == 0
    assert chunks[1].index == 1


# ---------------------------------------------------------------------------
# 4. Overlap — adjacent chunks share text
# ---------------------------------------------------------------------------
def test_chunks_overlap_by_default_50_tokens():
    # 1024 tokens / 462 step = ~2.2 → 3 chunks.
    text = "a" * 4096  # 1024 tokens
    chunks = chunk_text(text)
    assert len(chunks) == 3
    # First chunk has 512 tokens = 2048 chars
    assert len(chunks[0].text) == 2048
    # Chunk 1 starts at step 462 tokens = 1848 chars into the source,
    # so chunk[1].text starts where the source text is at offset 1848.
    assert chunks[1].text.startswith(text[1848:1856])


def test_chunks_do_not_exceed_window_size():
    text = "lorem ipsum " * 5000  # ~10k tokens
    chunks = chunk_text(text, window_tokens=512, overlap_tokens=50)
    for c in chunks:
        assert c.token_count <= 512, f"chunk {c.index} has {c.token_count} tokens"


def test_chunks_cover_full_input_in_order():
    text = ("The quick brown fox. " * 1000).strip()  # ~40k chars
    chunks = chunk_text(text)
    # First chunk starts at the beginning
    assert chunks[0].text.startswith("The quick brown fox.")
    # Chunks are index-ordered
    for i, c in enumerate(chunks):
        assert c.index == i
    # Chunks are non-empty
    for c in chunks:
        assert c.text.strip() != ""


# ---------------------------------------------------------------------------
# 5. Custom parameters
# ---------------------------------------------------------------------------
def test_custom_window_size():
    text = "a" * 800  # 200 tokens
    chunks = chunk_text(text, window_tokens=100, overlap_tokens=10)
    # 200 tokens / (100 - 10) = 2.22 → 3 chunks
    assert len(chunks) >= 2
    for c in chunks:
        assert c.token_count <= 100


def test_custom_overlap_respected():
    text = "b" * 4000  # 1000 tokens
    chunks = chunk_text(text, window_tokens=200, overlap_tokens=100)
    # 1000 tokens, window 200, overlap 100 → step of 100 tokens (400 chars).
    # Chunks at offsets 0, 400, 800, ..., 3200 = 9 chunks. Each is 800 chars.
    assert len(chunks) == 9
    for c in chunks:
        assert c.token_count == 200


def test_overlap_larger_than_window_raises():
    with pytest.raises(ValueError, match="overlap.*window"):
        chunk_text("hello", window_tokens=10, overlap_tokens=20)


# ---------------------------------------------------------------------------
# 6. Edge cases
# ---------------------------------------------------------------------------
def test_text_with_newlines_chunked_correctly():
    text = "line one\n" * 1000  # ~2000 tokens with newlines
    chunks = chunk_text(text)
    assert len(chunks) > 1
    # No chunk should lose the newline pattern badly
    assert all("line one" in c.text for c in chunks)


def test_unicode_text_preserved():
    text = "héllo wörld 🌍 " * 500
    chunks = chunk_text(text)
    assert len(chunks) >= 1
    assert "🌍" in chunks[0].text


# ---------------------------------------------------------------------------
# 7. 32 KB cap behavior
# ---------------------------------------------------------------------------
def test_32kb_chunks_correctly():
    # 32 KB ≈ 8k tokens, ~16 chunks with defaults
    text = "x" * 32_000
    chunks = chunk_text(text)
    # step = 512 - 50 = 462 tokens ≈ 1848 chars
    # 8192 tokens / 462 ≈ 17.7 → 18 chunks
    assert 15 <= len(chunks) <= 20
    for c in chunks:
        assert c.token_count <= 512


# ---------------------------------------------------------------------------
# 8. Dataclass behavior
# ---------------------------------------------------------------------------
def test_chunk_is_dataclass_with_required_fields():
    c = Chunk(index=0, text="hello", token_count=1)
    assert c.index == 0
    assert c.text == "hello"
    assert c.token_count == 1


def test_chunks_are_hashable_by_index():
    chunks = chunk_text("a" * 10000)
    ids = {c.index for c in chunks}
    assert len(ids) == len(chunks)  # all unique
