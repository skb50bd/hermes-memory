"""TDD: override.py — issue #8 fix."""

from __future__ import annotations

import json

import pytest

from hermes_memory.override import (
    PROVIDER_AUTO,
    PROVIDER_LOCAL,
    PROVIDER_POSTGRES,
    build_memory_block,
    memory_tool,
    pg_forget,
    pg_remember,
    pg_search,
    pg_status,
)
from hermes_memory.repos.memory_repo import (
    MEMORY_MAX_CHARS,
    Memory,
    MemoryNotFoundError,
    MemoryRepo,
    RoutingRuleViolationError,
)


# ---------------------------------------------------------------------------
# In-memory fake repo
# ---------------------------------------------------------------------------
class FakeRepo(MemoryRepo):
    def __init__(self):
        self.memories: dict[int, Memory] = {}
        self.next_id = 1

    def _insert_memory(self, content, *, tags, category, source, embedding_dim):
        for m in self.memories.values():
            if m.content == content and m.source == source and not m.deleted:
                return 0
        mid = self.next_id
        self.next_id += 1
        self.memories[mid] = Memory(
            id=mid,
            content=content,
            tags=tuple(tags),
            category=category,
            source=source,
            embedding_dim=embedding_dim,
            deleted=False,
        )
        return mid

    def _embed_query(self, query):
        return [0.0] * 1024

    def _search(self, query_embedding, query_text, *, top_k, hybrid_text_weight):
        return list(self.memories.values())[:top_k]

    def _forget(self, memory_id):
        m = self.memories.get(memory_id)
        if m is None:
            raise MemoryNotFoundError(str(memory_id))
        if m.deleted:
            return False
        m.mark_deleted()
        return True

    def _status(self):
        return {
            "live_memories": sum(1 for m in self.memories.values() if not m.deleted),
            "default_dim": 1024,
        }


@pytest.fixture
def repo():
    return FakeRepo()


@pytest.fixture
def postgres_env(monkeypatch):
    monkeypatch.setenv("MEMORY_PROVIDER", PROVIDER_POSTGRES)
    return monkeypatch


@pytest.fixture
def local_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORY_PROVIDER", PROVIDER_LOCAL)
    monkeypatch.setenv("MEMORY_LOCAL_PATH", str(tmp_path / "MEMORY.md"))
    return tmp_path


# ---------------------------------------------------------------------------
# pg_* functions
# ---------------------------------------------------------------------------
def test_pg_remember_stores(repo):
    out = pg_remember("hello", source="test", repo=repo)
    data = json.loads(out)
    assert data["status"] == "stored"
    assert data["id"] > 0


def test_pg_remember_no_repo():
    out = pg_remember("hello", repo=None)
    data = json.loads(out)
    assert "error" in data


def test_pg_remember_dedup(repo):
    pg_remember("hello", source="s", repo=repo)
    out = pg_remember("hello", source="s", repo=repo)
    data = json.loads(out)
    assert data["status"] == "duplicate"


def test_pg_remember_too_large_raises_routing(repo):
    """The pg_remember wrapper doesn't catch the rule; tools layer does."""
    with pytest.raises(RoutingRuleViolationError):
        pg_remember("x" * (MEMORY_MAX_CHARS + 1), repo=repo)


def test_pg_search_returns_json(repo):
    pg_remember("a", repo=repo)
    pg_remember("b", repo=repo)
    out = pg_search("a", repo=repo)
    data = json.loads(out)
    assert data["count"] == 2


def test_pg_forget_ok(repo):
    pg_remember("x", repo=repo)
    out = pg_forget(1, repo=repo)
    data = json.loads(out)
    assert data["status"] == "forgot"


def test_pg_forget_missing(repo):
    out = pg_forget(999, repo=repo)
    data = json.loads(out)
    assert "error" in data


def test_pg_status(repo):
    pg_remember("a", repo=repo)
    out = pg_status(repo=repo)
    data = json.loads(out)
    assert data["live_memories"] == 1


# ---------------------------------------------------------------------------
# memory_tool — the built-in override
# ---------------------------------------------------------------------------
def test_memory_tool_postgres_add(postgres_env, repo):
    out = memory_tool("add", content="hello", repo=repo)
    data = json.loads(out)
    assert data["status"] == "stored"


def test_memory_tool_postgres_search(postgres_env, repo):
    memory_tool("add", content="hello", repo=repo)
    out = memory_tool("search", query="hello", repo=repo)
    data = json.loads(out)
    assert data["count"] >= 1


def test_memory_tool_postgres_remove(postgres_env, repo):
    memory_tool("add", content="hello", repo=repo)
    out = memory_tool("remove", memory_id=1, repo=repo)
    data = json.loads(out)
    assert data["status"] == "forgot"


def test_memory_tool_postgres_replace(postgres_env, repo):
    memory_tool("add", content="hello", repo=repo)
    out = memory_tool("replace", memory_id=1, content="goodbye", repo=repo)
    data = json.loads(out)
    assert data["status"] == "stored"


def test_memory_tool_postgres_list(postgres_env, repo):
    memory_tool("add", content="x", repo=repo)
    out = memory_tool("list", repo=repo)
    data = json.loads(out)
    assert "live_memories" in data


def test_memory_tool_too_large_returns_routing_error(postgres_env, repo):
    """Critical for issue #5 acceptance criterion: the routing rule
    must surface when the agent tries to store something too big."""
    out = memory_tool("add", content="x" * (MEMORY_MAX_CHARS + 1), repo=repo)
    data = json.loads(out)
    assert data.get("error") == "routing_rule_violation"
    assert "wiki" in data.get("message", "").lower()


def test_memory_tool_validation_add_requires_content(postgres_env, repo):
    out = memory_tool("add", content=None, repo=repo)
    data = json.loads(out)
    assert "error" in data


def test_memory_tool_validation_remove_requires_id(postgres_env, repo):
    out = memory_tool("remove", memory_id=None, repo=repo)
    data = json.loads(out)
    assert "error" in data


def test_memory_tool_local_add(local_env):
    out = memory_tool("add", content="hello")
    data = json.loads(out)
    assert data["status"] == "stored"
    assert data["mode"] == "local"
    assert (local_env / "MEMORY.md").read_text() == "- hello\n"


def test_memory_tool_local_search(local_env):
    memory_tool("add", content="postgres tip")
    memory_tool("add", content="wiki note")
    out = memory_tool("search", query="postgres")
    data = json.loads(out)
    assert data["count"] == 1
    assert "postgres" in data["results"][0]


def test_memory_tool_local_list(local_env):
    memory_tool("add", content="x")
    out = memory_tool("list")
    data = json.loads(out)
    assert any("x" in line for line in data["lines"])


def test_memory_tool_auto_falls_back_to_local(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORY_PROVIDER", PROVIDER_AUTO)
    monkeypatch.setenv("MEMORY_LOCAL_PATH", str(tmp_path / "MEMORY.md"))
    # No repo wired → falls back to local
    out = memory_tool("add", content="hello", repo=None)
    data = json.loads(out)
    assert data["status"] == "stored"
    assert data["mode"] == "local"


def test_memory_tool_uses_postgres_when_auto_and_repo_available(monkeypatch, repo):
    monkeypatch.setenv("MEMORY_PROVIDER", PROVIDER_AUTO)
    out = memory_tool("add", content="hello", repo=repo)
    data = json.loads(out)
    # Postgres path; not local
    assert "path" not in data
    assert data["status"] == "stored"


# ---------------------------------------------------------------------------
# Bug 3: _read_provider must consult ~/.hermes/config.yaml's memory.provider
# when MEMORY_PROVIDER env var is unset. Issue #8 root cause.
# ---------------------------------------------------------------------------
def test_read_provider_prefers_env_var(monkeypatch, tmp_path):
    """MEMORY_PROVIDER env var wins over config.yaml."""
    from hermes_memory.override import _read_provider

    cfg = tmp_path / "config.yaml"
    cfg.write_text("memory:\n  provider: local\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("MEMORY_PROVIDER", "postgres")
    assert _read_provider() == PROVIDER_POSTGRES


def test_read_provider_falls_back_to_config_yaml(monkeypatch, tmp_path):
    """When env var is unset, read memory.provider from config.yaml."""
    from hermes_memory.override import _read_provider

    cfg = tmp_path / "config.yaml"
    cfg.write_text("memory:\n  provider: postgres\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("MEMORY_PROVIDER", raising=False)
    assert _read_provider() == PROVIDER_POSTGRES


def test_read_provider_config_yaml_local(monkeypatch, tmp_path):
    """config.yaml can also pin local."""
    from hermes_memory.override import _read_provider

    cfg = tmp_path / "config.yaml"
    cfg.write_text("memory:\n  provider: local\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("MEMORY_PROVIDER", raising=False)
    assert _read_provider() == PROVIDER_LOCAL


def test_read_provider_defaults_to_local_when_no_signal(monkeypatch, tmp_path):
    """No env var AND no config.yaml → local."""
    from hermes_memory.override import _read_provider

    (tmp_path / "config.yaml").write_text("unrelated_key: 1\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("MEMORY_PROVIDER", raising=False)
    assert _read_provider() == PROVIDER_LOCAL


def test_read_provider_handles_missing_config_yaml(monkeypatch, tmp_path):
    """If config.yaml doesn't exist, default to local (env-var-only path)."""
    from hermes_memory.override import _read_provider

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("MEMORY_PROVIDER", raising=False)
    # No file written
    assert _read_provider() == PROVIDER_LOCAL


def test_read_provider_handles_malformed_config_yaml(monkeypatch, tmp_path):
    """Malformed YAML must not raise; fall back to local."""
    from hermes_memory.override import _read_provider

    cfg = tmp_path / "config.yaml"
    cfg.write_text("this: is: not: valid: yaml: : :\n  bad indent\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("MEMORY_PROVIDER", raising=False)
    assert _read_provider() == PROVIDER_LOCAL


def test_memory_tool_routes_to_postgres_from_config_yaml(monkeypatch, tmp_path, repo):
    """End-to-end: config.yaml says postgres → memory_tool uses PG path."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("memory:\n  provider: postgres\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("MEMORY_PROVIDER", raising=False)
    out = memory_tool("add", content="hello", repo=repo)
    data = json.loads(out)
    # PG path stores with id; local path stores with status + path
    assert "id" in data
    assert data["status"] == "stored"


def test_memory_tool_routes_to_local_from_config_yaml(monkeypatch, tmp_path):
    """End-to-end: config.yaml says local → memory_tool uses local path."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("memory:\n  provider: local\n")
    local = tmp_path / "MEMORY.md"
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("MEMORY_LOCAL_PATH", str(local))
    monkeypatch.delenv("MEMORY_PROVIDER", raising=False)
    out = memory_tool("add", content="hello")
    data = json.loads(out)
    assert data["status"] == "stored"
    assert data["mode"] == "local"
    assert local.read_text() == "- hello\n"


# ---------------------------------------------------------------------------
# build_memory_block
# ---------------------------------------------------------------------------
def test_build_memory_block_with_repo(repo):
    pg_remember("a", repo=repo)
    pg_remember("b", repo=repo)
    block = build_memory_block(repo)
    assert "MEMORY" in block
    assert "postgres" in block
    assert "2 live" in block


def test_build_memory_block_no_repo():
    block = build_memory_block(None)
    assert "no memory store" in block
