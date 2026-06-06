"""Text chunker for pg_remember — 512-token windows, 50-token overlap.

Issue #5: pg_remember must accept facts up to 32 KB. Long content is
chunked into overlapping windows before embedding, with each chunk
getting its own vector. The parent memory stays in
agent_memory.memories; chunks live in agent_memory.memory_chunks.

Token counting is **approximate** (1 token ≈ 4 chars English). This
is intentional — we don't need exact counts for a sliding window,
just consistent window sizes. The CHUNKER_TOKEN_RATIO constant
documents the heuristic.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Approximate chars per token for English text. 4 is the standard
#: OpenAI/Claude heuristic; works well for English, Latin-script
#: European languages, and basic CJK (slight undercount for CJK).
CHUNKER_TOKEN_RATIO = 4

#: Default window size in tokens. 512 is the bge-m3 max input size
#: divided by 16, leaving comfortable headroom.
DEFAULT_WINDOW_TOKENS = 512

#: Default overlap between adjacent chunks, in tokens. 50 is ~10% of
#: the window — enough to maintain context across boundaries without
#: doubling storage.
DEFAULT_OVERLAP_TOKENS = 50


@dataclass(frozen=True)
class Chunk:
    """A single chunk of a longer text.

    Attributes:
        index: 0-based position in the chunk sequence.
        text: The chunk content (always non-empty after stripping).
        token_count: Approximate token count (chars / 4).
    """

    index: int
    text: str
    token_count: int

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError(f"chunk index must be >= 0, got {self.index}")
        if not self.text or not self.text.strip():
            raise ValueError(f"chunk {self.index} has empty text")
        if self.token_count <= 0:
            raise ValueError(
                f"chunk {self.index} has non-positive token_count={self.token_count}"
            )


def _approx_tokens(text: str) -> int:
    """Approximate token count using the char/4 heuristic.

    Empty/whitespace-only strings return 0 so chunk_text can filter
    them.
    """
    if not text or not text.strip():
        return 0
    return max(1, len(text) // CHUNKER_TOKEN_RATIO)


def _chars_for_tokens(tokens: int) -> int:
    """Convert token count back to char count."""
    return tokens * CHUNKER_TOKEN_RATIO


def chunk_text(
    text: str,
    window_tokens: int = DEFAULT_WINDOW_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[Chunk]:
    """Split text into overlapping chunks.

    Args:
        text: Input text. Empty/whitespace-only returns [].
        window_tokens: Maximum tokens per chunk. Must be > 0.
        overlap_tokens: Tokens shared between adjacent chunks. Must
            be < window_tokens (raises ValueError otherwise).

    Returns:
        List of Chunk, ordered, with non-overlapping indices. Each
        chunk's token_count is <= window_tokens.

    Notes:
        The first chunk is always anchored at the start of the text.
        The last chunk is whatever remains after the last step.
        Adjacent chunks overlap by `overlap_tokens` tokens (the
        last N tokens of chunk N reappear at the start of chunk
        N+1).
    """
    if window_tokens <= 0:
        raise ValueError(f"window_tokens must be > 0, got {window_tokens}")
    if overlap_tokens < 0:
        raise ValueError(f"overlap_tokens must be >= 0, got {overlap_tokens}")
    if overlap_tokens >= window_tokens:
        raise ValueError(
            f"overlap_tokens ({overlap_tokens}) must be < "
            f"window_tokens ({window_tokens})"
        )

    stripped = text.strip()
    if not stripped:
        return []

    window_chars = _chars_for_tokens(window_tokens)
    overlap_chars = _chars_for_tokens(overlap_tokens)
    step = window_chars - overlap_chars
    if step <= 0:
        # Cannot happen given the check above, but defensive.
        raise ValueError("chunk step must be positive")

    chunks: list[Chunk] = []
    start = 0
    index = 0
    n = len(text)
    while start < n:
        end = min(start + window_chars, n)
        chunk_text_str = text[start:end]
        # Last chunk may be tiny; emit as long as non-empty.
        if chunk_text_str.strip():
            token_count = _approx_tokens(chunk_text_str)
            chunks.append(
                Chunk(
                    index=index,
                    text=chunk_text_str,
                    token_count=min(token_count, window_tokens),
                )
            )
            index += 1
        if end == n:
            break
        start += step
        # Edge case: very last iteration where step pushes us back
        # to the same position. Avoid infinite loop.
        if start == end:
            break

    return chunks
