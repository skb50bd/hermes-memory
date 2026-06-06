"""TDD: PgWikiRepo — documents, links, FTS, related/backlinks."""

from __future__ import annotations

import pytest


@pytest.fixture
def wiki_repo(pg_conn):
    from hermes_memory.pg_repos import PgWikiRepo
    return PgWikiRepo(pg_conn)


def test_create_and_read(wiki_repo) -> None:
    did = wiki_repo.create(
        "platform-overview",
        "Platform Overview",
        "Hermes is a multi-agent gateway for LLM backends.",
    )
    assert did > 0
    doc = wiki_repo.read("platform-overview")
    assert doc is not None
    assert doc.slug == "platform-overview"
    assert doc.title == "Platform Overview"
    assert "multi-agent gateway" in doc.body_md


def test_read_missing_returns_none(wiki_repo) -> None:
    assert wiki_repo.read("does-not-exist") is None


def test_search_finds_by_keyword(wiki_repo) -> None:
    wiki_repo.create("a", "Auth", "OAuth 2.0 with PKCE is recommended.")
    wiki_repo.create("b", "Routing", "Hermes routes by tenant to a chosen model.")
    hits = wiki_repo.search("oauth", top_k=5)
    slugs = [h.slug for h in hits]
    assert "a" in slugs
    assert "b" not in slugs


def test_link_creates_backlink(wiki_repo) -> None:
    wiki_repo.create("a", "A", "alpha")
    wiki_repo.create("b", "B", "beta")
    assert wiki_repo.link("a", "b") is True
    backlinks = wiki_repo.backlinks("b")
    assert any(d.slug == "a" for d in backlinks)


def test_link_self_is_noop(wiki_repo) -> None:
    wiki_repo.create("a", "A", "alpha")
    assert wiki_repo.link("a", "a") is False


def test_related_walks_graph(wiki_repo) -> None:
    wiki_repo.create("a", "A", "x")
    wiki_repo.create("b", "B", "y")
    wiki_repo.create("c", "C", "z")
    wiki_repo.link("a", "b")
    wiki_repo.link("b", "c")
    related = wiki_repo.related("a", max_hops=2)
    slugs = [d.slug for d in related]
    assert "b" in slugs
    assert "c" in slugs
