"""TDD: PgJournalRepo — sessions, messages, FTS."""

from __future__ import annotations

import pytest


@pytest.fixture
def journal_repo(pg_conn):
    from hermes_memory.pg_repos import PgJournalRepo
    return PgJournalRepo(pg_conn)


def test_log_session_returns_id(journal_repo) -> None:
    sid = journal_repo.log_session("default", metadata={"platform": "telegram"})
    assert sid > 0


def test_log_message_attaches_to_session(journal_repo) -> None:
    sid = journal_repo.log_session("default")
    mid = journal_repo.log_message(sid, "user", "Hello, agent.")
    assert mid > 0
    assert isinstance(mid, int)


def test_log_message_validates_role(journal_repo) -> None:
    sid = journal_repo.log_session("default")
    with pytest.raises(ValueError):
        journal_repo.log_message(sid, "robot", "I am not a role")


def test_search_finds_messages_by_keyword(journal_repo) -> None:
    sid = journal_repo.log_session("default")
    journal_repo.log_message(sid, "user", "Postgres is our database of choice")
    journal_repo.log_message(sid, "assistant", "Acknowledged, using Postgres 18.")
    journal_repo.log_message(sid, "user", "Pinecone is for vector search")

    hits = journal_repo.search("postgres", top_k=5)
    assert len(hits) >= 2
    assert all(h.session_id == sid for h in hits)


def test_search_filters_by_session(journal_repo) -> None:
    s1 = journal_repo.log_session("default")
    s2 = journal_repo.log_session("default")
    journal_repo.log_message(s1, "user", "deploy to production")
    journal_repo.log_message(s2, "user", "deploy to staging")
    hits = journal_repo.search("deploy", top_k=5, session_id=s1)
    assert all(h.session_id == s1 for h in hits)


def test_search_empty_query_returns_empty(journal_repo) -> None:
    assert journal_repo.search("") == []
    assert journal_repo.search("   ") == []
