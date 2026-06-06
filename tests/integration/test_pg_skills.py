"""TDD: PgSkillsRepo — skills catalog, links, FTS, graph walk."""

from __future__ import annotations

import pytest


@pytest.fixture
def skills_repo(pg_conn):
    from hermes_memory.pg_repos import PgSkillsRepo

    return PgSkillsRepo(pg_conn)


def test_register_and_search(skills_repo) -> None:
    skills_repo.register("plan", "1.0.0", description="Write plans for tasks")
    skills_repo.register("review", "1.0.0", description="Review code")
    hits = skills_repo.search("plan", top_k=5)
    names = [s.name for s in hits]
    assert "plan" in names
    assert "review" not in names


def test_link_creates_relationship(skills_repo) -> None:
    skills_repo.register("a", "1.0.0")
    skills_repo.register("b", "1.0.0")
    assert skills_repo.link("a", "b", "depends_on") is True


def test_link_self_is_noop(skills_repo) -> None:
    skills_repo.register("a", "1.0.0")
    assert skills_repo.link("a", "a", "depends_on") is False


def test_link_validates_kind(skills_repo) -> None:
    skills_repo.register("a", "1.0.0")
    skills_repo.register("b", "1.0.0")
    with pytest.raises(ValueError):
        skills_repo.link("a", "b", "bogus_kind")


def test_graph_walks_neighborhood(skills_repo) -> None:
    skills_repo.register("a", "1.0.0")
    skills_repo.register("b", "1.0.0")
    skills_repo.register("c", "1.0.0")
    skills_repo.link("a", "b", "depends_on")
    skills_repo.link("b", "c", "related")
    g = skills_repo.graph("a", max_hops=2)
    assert "a" in g
    assert "b" in g.get("a", [])
    assert "c" in g.get("b", [])
