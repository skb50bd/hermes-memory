"""Smoke test: conftest spins up a real PG test DB, Memory works end-to-end."""

from __future__ import annotations


def test_conftest_provides_real_pg(pg_conn: str) -> None:
    """The session-scoped fixture must hand back a working DSN."""
    assert pg_conn.startswith("postgresql://")
    assert "hermes_pytest_" in pg_conn


def test_memory_repo_inserts_and_searches(memory_repo) -> None:
    """The full base-class contract holds against a real Postgres."""
    mid = memory_repo.remember(
        "Hermes Agent is a Python/C# hybrid from Pixu. "
        "It uses Postgres for memory, FTS + vector hybrid search."
    )
    assert mid > 0
    hits = memory_repo.search("python memory hybrid search", top_k=5)
    assert any(h.id == mid for h in hits)


def test_memory_repo_forget_soft_deletes(memory_repo) -> None:
    mid = memory_repo.remember("Ephemeral fact to be forgotten")
    assert memory_repo.forget(mid) is True
    assert memory_repo.forget(mid) is False  # already deleted
