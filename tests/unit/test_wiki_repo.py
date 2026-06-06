"""TDD: wiki_repo.py — documents, links, search."""

from __future__ import annotations

import pytest

from hermes_memory.repos.wiki_repo import Document, WikiRepo


class FakeWikiRepo(WikiRepo):
    def __init__(self) -> None:
        self._docs: dict[str, Document] = {}
        self._links: list[tuple[str, str, str | None]] = []
        self._next_id = 1

    def _insert_document(self, slug, title, body_md, *, category, tags, metadata):
        if slug in self._docs:
            raise ValueError(f"slug already exists: {slug}")
        did = self._next_id
        self._next_id += 1
        self._docs[slug] = Document(
            id=did, slug=slug, title=title, body_md=body_md,
            category=category, metadata=metadata, tags=tuple(tags),
        )
        return did

    def _fetch_document(self, slug):
        return self._docs.get(slug)

    def _insert_link(self, source_slug, target_slug, context):
        if source_slug not in self._docs or target_slug not in self._docs:
            return False
        self._links.append((source_slug, target_slug, context))
        return True

    def _fetch_backlinks(self, target_slug):
        sources = [s for s, t, _ in self._links if t == target_slug]
        return [self._docs[s] for s in sources if s in self._docs]

    def _fetch_related(self, slug, max_hops):
        # BFS
        seen = {slug}
        frontier = [slug]
        for _ in range(max_hops):
            new_frontier = []
            for node in frontier:
                for s, t, _ in self._links:
                    if s == node and t not in seen:
                        seen.add(t)
                        new_frontier.append(t)
                    elif t == node and s not in seen:
                        seen.add(s)
                        new_frontier.append(s)
            frontier = new_frontier
        seen.discard(slug)
        return [self._docs[s] for s in seen if s in self._docs]

    def _search(self, query, *, top_k):
        hits = [
            d for d in self._docs.values()
            if query.lower() in d.title.lower()
            or query.lower() in d.body_md.lower()
        ]
        return hits[:top_k]


@pytest.fixture
def repo():
    return FakeWikiRepo()


def test_create_and_read(repo):
    did = repo.create("foo", "Foo", "hello world")
    assert did > 0
    d = repo.read("foo")
    assert d is not None
    assert d.title == "Foo"
    assert d.body_md == "hello world"


def test_create_duplicate_raises(repo):
    repo.create("foo", "Foo", "x")
    with pytest.raises(ValueError, match="already exists"):
        repo.create("foo", "Foo2", "y")


def test_create_empty_slug_raises(repo):
    with pytest.raises(ValueError, match="slug"):
        repo.create("", "title", "body")


def test_link_and_backlinks(repo):
    repo.create("a", "A", "")
    repo.create("b", "B", "")
    assert repo.link("a", "b", context="see also") is True
    bls = repo.backlinks("b")
    assert len(bls) == 1
    assert bls[0].slug == "a"


def test_link_self_ignored(repo):
    repo.create("a", "A", "")
    assert repo.link("a", "a") is False


def test_link_missing_target_returns_false(repo):
    repo.create("a", "A", "")
    assert repo.link("a", "ghost") is False


def test_related_1_hop(repo):
    repo.create("a", "A", "")
    repo.create("b", "B", "")
    repo.create("c", "C", "")
    repo.link("a", "b")
    repo.link("a", "c")
    rel = repo.related("a", max_hops=1)
    slugs = {d.slug for d in rel}
    assert slugs == {"b", "c"}


def test_related_2_hops(repo):
    repo.create("a", "A", "")
    repo.create("b", "B", "")
    repo.create("c", "C", "")
    repo.link("a", "b")
    repo.link("b", "c")
    rel = repo.related("a", max_hops=2)
    slugs = {d.slug for d in rel}
    assert slugs == {"b", "c"}


def test_related_0_hops_raises(repo):
    with pytest.raises(ValueError, match="max_hops"):
        repo.related("a", max_hops=0)


def test_search_finds_matches(repo):
    repo.create("a", "Postgres tips", "ways to use postgres")
    repo.create("b", "Wiki routing", "routing between memory and wiki")
    repo.create("c", "Unrelated", "blah")
    hits = repo.search("postgres", top_k=5)
    slugs = {d.slug for d in hits}
    assert "a" in slugs


def test_search_empty_query_returns_empty(repo):
    repo.create("a", "A", "x")
    assert repo.search("") == []


def test_create_with_tags_and_category(repo):
    repo.create(
        "x", "X", "body",
        category="projects.sportsverse",
        tags=["postgres", "wiki"],
        metadata={"author": "shakib"},
    )
    d = repo.read("x")
    assert d.category == "projects.sportsverse"
    assert d.tags == ("postgres", "wiki")
    assert d.metadata == {"author": "shakib"}
