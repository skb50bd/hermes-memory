"""TDD: register.py — plugin entry point.

The plugin's register(ctx) function:
  - Builds repos from env (graceful if HERMES_PG_CONN_STR is missing)
  - Registers 46 surface tools via ctx.register_tool
  - Overrides the built-in 'memory' tool (issue #8) using override=True
  - Registers hooks (on_session_end, pre_tool_call)
  - Works even if psycopg3 is not installed (logs warning, returns empty)
"""

from __future__ import annotations

from typing import Any

import pytest

from hermes_memory.register import register


class FakeCtx:
    """Records every call to register_tool / register_hook."""

    def __init__(self):
        self.tools: dict[tuple[str, str], dict[str, Any]] = {}
        self.hooks: list[tuple[str, Any]] = []

    def register_tool(
        self, *, name, toolset, schema, handler, override=False, **kw
    ):
        # Idempotency: same name+toolset twice = second wins
        key = (name, toolset)
        if key in self.tools and not override:
            raise RuntimeError(f"duplicate tool {name} in {toolset}")
        self.tools[key] = {
            "name": name, "toolset": toolset, "schema": schema,
            "handler": handler, "override": override, **kw,
        }

    def register_hook(self, name, fn):
        self.hooks.append((name, fn))


@pytest.fixture
def ctx():
    return FakeCtx()


def test_register_loads_with_no_dsn(monkeypatch, ctx):
    """When HERMES_PG_CONN_STR is unset, register() should still
    succeed and register the 46 tools (with empty repos, the tools
    return error messages — that's expected)."""
    monkeypatch.delenv("HERMES_PG_CONN_STR", raising=False)
    monkeypatch.delenv("PG_MEM_DB_CONN_STR", raising=False)
    # Inject 8 fake repos so the surface tools register.
    _inject_fake_repos(monkeypatch)
    register(ctx)
    # 46 surface tools + 1 memory override = 47
    assert len(ctx.tools) == 47


def test_register_memory_tool_uses_override(ctx):
    """The 'memory' tool must register with override=True (issue #8)."""
    _inject_fake_repos()
    register(ctx)
    memory = ctx.tools[("memory", "hermes_postgres_memory")]
    assert memory["override"] is True


def test_register_tools_have_toolset(ctx):
    """All tools register under the 'hermes_postgres_memory' toolset."""
    _inject_fake_repos()
    register(ctx)
    for (_name, toolset), _entry in ctx.tools.items():
        assert toolset == "hermes_postgres_memory"


def test_register_hooks(ctx):
    """on_session_end and pre_tool_call hooks are registered."""
    _inject_fake_repos()
    register(ctx)
    hook_names = {n for n, _ in ctx.hooks}
    assert "on_session_end" in hook_names
    assert "pre_tool_call" in hook_names


def test_register_idempotent(ctx):
    """Calling register() twice on the same ctx raises (idempotency check)."""
    _inject_fake_repos()
    register(ctx)
    with pytest.raises(RuntimeError, match="duplicate"):
        register(ctx)


def test_register_tool_schemas_have_description(ctx):
    """Every tool schema has a 'description' field for the LLM."""
    _inject_fake_repos()
    register(ctx)
    for entry in ctx.tools.values():
        schema = entry["schema"]
        assert "description" in schema, f"tool {entry['name']} has no description"
        assert schema["description"], f"tool {entry['name']} has empty description"


def test_register_calls_handler_with_kwargs(ctx):
    """The memory tool's handler accepts **kwargs and forwards to override."""
    _inject_fake_repos()
    register(ctx)
    memory = ctx.tools[("memory", "hermes_postgres_memory")]
    handler = memory["handler"]
    # Calling with action="add" and content="x" should not raise.
    # Without a real PG, the override falls back to local MEMORY.md
    # (since the fake repo object is not a real MemoryRepo, the
    # 'auto' provider takes the local path). Either way, the
    # response is a JSON object.
    import json
    out = handler(action="add", content="x")
    parsed = json.loads(out)
    assert isinstance(parsed, dict)
    # The result either stores, errors, or reports status.
    assert "status" in parsed or "error" in parsed


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _inject_fake_repos(monkeypatch=None) -> None:
    """Patch _try_build_repos to return 8 fake repos so surface tools register."""
    fake = {
        "memory": object(),
        "wiki": object(),
        "journal": object(),
        "skills": object(),
        "metrics": object(),
        "kanban": object(),
        "observability": object(),
        "sessions": object(),
    }
    import hermes_memory.register as reg_mod
    if monkeypatch is not None:
        monkeypatch.setattr(reg_mod, "_try_build_repos", lambda: fake)
    else:
        reg_mod._try_build_repos = lambda: fake  # type: ignore[assignment]
