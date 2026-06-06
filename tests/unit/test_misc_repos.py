"""TDD: journal_repo, skills_repo, metrics_repo."""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta

from hermes_memory.repos.journal_repo import JournalRepo, Message
from hermes_memory.repos.skills_repo import SkillsRepo, Skill
from hermes_memory.repos.metrics_repo import MetricsRepo, MetricPoint


# ---------------------------------------------------------------------------
# Journal
# ---------------------------------------------------------------------------
class FakeJournalRepo(JournalRepo):
    def __init__(self):
        self._sessions: dict[int, str] = {}
        self._messages: list[Message] = []
        self._next_sid = 1
        self._next_mid = 1

    def _insert_session(self, profile, metadata):
        sid = self._next_sid
        self._next_sid += 1
        self._sessions[sid] = profile
        return sid

    def _insert_message(self, session_id, role, content, tool_calls):
        mid = self._next_mid
        self._next_mid += 1
        self._messages.append(Message(mid, session_id, role, content, tool_calls))
        return mid

    def _search(self, query, *, top_k, session_id, role):
        out = []
        for m in self._messages:
            if session_id and m.session_id != session_id:
                continue
            if role and m.role != role:
                continue
            if query.lower() in m.content.lower():
                out.append(m)
        return out[:top_k]


def test_journal_log_session_and_message():
    j = FakeJournalRepo()
    sid = j.log_session("default")
    assert sid > 0
    mid = j.log_message(sid, "user", "hello")
    assert mid > 0


def test_journal_invalid_role_raises():
    j = FakeJournalRepo()
    sid = j.log_session("default")
    with pytest.raises(ValueError, match="role"):
        j.log_message(sid, "hacker", "x")  # type: ignore[arg-type]


def test_journal_empty_content_raises():
    j = FakeJournalRepo()
    sid = j.log_session("default")
    with pytest.raises(ValueError, match="content"):
        j.log_message(sid, "user", "")


def test_journal_search_filters_by_session():
    j = FakeJournalRepo()
    s1 = j.log_session("default")
    s2 = j.log_session("work")
    j.log_message(s1, "user", "postgres tip")
    j.log_message(s2, "user", "postgres sql")
    hits = j.search("postgres", session_id=s1)
    assert len(hits) == 1
    assert hits[0].session_id == s1


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------
class FakeSkillsRepo(SkillsRepo):
    def __init__(self):
        self._skills: dict[str, Skill] = {}  # key: "name|version"
        self._links: list[tuple[str, str, str]] = []

    def _insert_skill(self, name, version, description, owner, tags):
        key = f"{name}|{version}"
        if key in self._skills:
            return False
        self._skills[key] = Skill(
            name, version, description, owner, tuple(tags)
        )
        return True

    def _search(self, query, *, top_k):
        return [
            s for s in self._skills.values()
            if query.lower() in s.name.lower() or query.lower() in s.description.lower()
        ][:top_k]

    def _insert_link(self, source, target, kind):
        if not any(s.name == source for s in self._skills.values()):
            return False
        if not any(s.name == target for s in self._skills.values()):
            return False
        self._links.append((source, target, kind))
        return True

    def _graph(self, root, max_hops):
        out: dict[str, list[str]] = {root: []}
        frontier = [root]
        for _ in range(max_hops):
            new = []
            for n in frontier:
                for s, t, _ in self._links:
                    if s == n and t not in out:
                        out.setdefault(t, []).append(_)
                        new.append(t)
                    elif t == n and s not in out:
                        out.setdefault(s, []).append(_)
                        new.append(s)
            frontier = new
        return out


def test_skills_register_and_search():
    s = FakeSkillsRepo()
    assert s.register("foo", "1.0.0", description="does foo things") is True
    hits = s.search("foo")
    assert any(h.name == "foo" for h in hits)


def test_skills_register_duplicate_version_returns_false():
    s = FakeSkillsRepo()
    s.register("foo", "1.0.0")
    assert s.register("foo", "1.0.0") is False


def test_skills_link_creates_relationship():
    s = FakeSkillsRepo()
    s.register("a", "1")
    s.register("b", "1")
    assert s.link("a", "b", "depends_on") is True


def test_skills_link_invalid_kind_raises():
    s = FakeSkillsRepo()
    s.register("a", "1")
    s.register("b", "1")
    with pytest.raises(ValueError, match="kind"):
        s.link("a", "b", "hates")  # type: ignore[arg-type]


def test_skills_graph_traverses():
    s = FakeSkillsRepo()
    s.register("a", "1")
    s.register("b", "1")
    s.register("c", "1")
    s.link("a", "b", "depends_on")
    s.link("b", "c", "depends_on")
    g = s.graph("a", max_hops=2)
    assert "a" in g
    assert "b" in g
    assert "c" in g


def test_skills_graph_0_hops_raises():
    s = FakeSkillsRepo()
    with pytest.raises(ValueError, match="max_hops"):
        s.graph("a", max_hops=0)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
class FakeMetricsRepo(MetricsRepo):
    def __init__(self):
        self._events: list[tuple[datetime, str, str, float]] = []

    def _insert_event(self, profile, name, value, tags):
        self._events.append((datetime.utcnow(), profile, name, value))
        return len(self._events)

    def _query(self, profile, name, *, from_ts, to_ts, bucket):
        # trivial: return one aggregate point with avg=value
        vals = [v for ts, p, n, v in self._events if p == profile and n == name]
        if not vals:
            return []
        avg = sum(vals) / len(vals)
        ts = from_ts or datetime.utcnow() - timedelta(minutes=5)
        return [MetricPoint(ts=ts, p50=avg, p95=avg, p99=avg, count=len(vals))]


def test_metrics_record_and_query():
    m = FakeMetricsRepo()
    m.record("default", "llm.latency_ms", 250.0)
    m.record("default", "llm.latency_ms", 300.0)
    points = m.query("default", "llm.latency_ms")
    assert len(points) == 1
    assert points[0].count == 2
    assert points[0].p50 == 275.0


def test_metrics_record_empty_name_raises():
    m = FakeMetricsRepo()
    with pytest.raises(ValueError):
        m.record("default", "", 1.0)


def test_metrics_query_empty_returns_empty():
    m = FakeMetricsRepo()
    points = m.query("default", "nope")
    assert points == []
