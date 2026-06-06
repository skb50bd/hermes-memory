"""Smoke test: conftest spins up a real PG test DB, Memory works end-to-end."""

from __future__ import annotations

import os
import socket

import pytest


def test_conftest_provides_real_pg(pg_conn: str) -> None:
    """The session-scoped fixture must hand back a working DSN."""
    assert pg_conn.startswith("postgresql://")
    assert "hermes_pytest_" in pg_conn


def _embedder_reachable() -> bool:
    """Is the configured embedder (Ollama) up?

    CI has no embedder. Local dev usually does (Ollama on
    127.0.0.1:11434). Tests that hit a real embedder should be
    skipped when none is reachable.
    """
    base = os.environ.get("HERMES_EMBED_BASE_URL", "http://127.0.0.1:11434/v1")
    # Convert http://host:port/v1 to host:port
    from urllib.parse import urlparse

    p = urlparse(base)
    try:
        with socket.create_connection((p.hostname or "127.0.0.1", p.port or 80), timeout=0.5):
            return True
    except Exception:
        return False


@pytest.mark.skipif(
    not _embedder_reachable(),
    reason="no embedder reachable (set HERMES_EMBED_BASE_URL or start Ollama)",
)
def test_memory_repo_inserts_and_searches(memory_repo) -> None:
    """The full base-class contract holds against a real Postgres."""
    mid = memory_repo.remember(
        "Hermes Agent is a Python/C# hybrid from Pixu. "
        "It uses Postgres for memory, FTS + vector hybrid search."
    )
    assert mid > 0
    hits = memory_repo.search("python memory hybrid search", top_k=5)
    assert any(h.id == mid for h in hits)


@pytest.mark.skipif(
    not _embedder_reachable(),
    reason="no embedder reachable (set HERMES_EMBED_BASE_URL or start Ollama)",
)
def test_memory_repo_forget_soft_deletes(memory_repo) -> None:
    mid = memory_repo.remember("Ephemeral fact to be forgotten")
    assert memory_repo.forget(mid) is True
    assert memory_repo.forget(mid) is False  # already deleted
