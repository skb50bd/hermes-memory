"""
Back-compat shim. Re-exports the same tool names the legacy postgres
plugin exposed, so existing hermes-agent installations keep working while they
migrate to calling the MCP server directly.

This file is a thin Python wrapper. The actual work happens in the
hermes-memory binary, which is a stdio MCP server. The shim translates
the old `pg_remember` style call into a JSON-RPC message over stdin
and prints the response.

Use the MCP server directly in new code.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

# Map of old plugin tool name -> MCP tool name
RENAMES = {
    "pg_remember":   "memory_remember",
    "pg_search":     "memory_search",
    "pg_forget":     "memory_forget",
    "pg_status":     "memory_status",
    "wiki_create":   "wiki_create",
    "wiki_read":     "wiki_read",
    "wiki_link":     "wiki_link",
    "wiki_search":   "wiki_search",
}


def _mcp_call(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Spawn a fresh hermes-memory process per call. Slow but simple.

    Long-term this should use a persistent subprocess; for v1 the per-call
    cost is negligible for a memory plugin that runs on user request.
    """
    binary = os.environ.get("HERMES_MEMORY_BIN", "hermes-memory")
    proc = subprocess.run(
        [binary, "--mcp", "--one-shot", method, json.dumps(params)],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"hermes-memory {method} failed: {proc.stderr}")
    return json.loads(proc.stdout) if proc.stdout else {}


def pg_remember(content: str, tags: list[str] | None = None, category: str | None = None, source: str | None = None) -> int:
    return _mcp_call(RENAMES["pg_remember"], {"content": content, "tags": tags, "category": category, "source": source}).get("id", 0)


def pg_search(query: str, top_k: int = 10, hybrid_text_weight: float = 0.5) -> list[dict[str, Any]]:
    raw = _mcp_call(RENAMES["pg_search"], {"query": query, "top_k": top_k, "hybrid_text_weight": hybrid_text_weight})
    if isinstance(raw, list):
        return raw
    # Newer MCP response shape wraps results in {"results": [...]}.
    if isinstance(raw, dict) and "results" in raw:
        return list(raw["results"])
    return []


def pg_forget(memory_id: int) -> bool:
    result = _mcp_call(RENAMES["pg_forget"], {"id": memory_id})
    if isinstance(result, dict):
        return bool(result.get("ok", False))
    return False


def pg_status() -> dict[str, Any]:
    return _mcp_call(RENAMES["pg_status"], {})


# Direct MCP re-exports for new code
wiki_create    = lambda **kw: _mcp_call("wiki_create", kw)
wiki_read      = lambda slug: _mcp_call("wiki_read", {"slug": slug})
wiki_link      = lambda **kw: _mcp_call("wiki_link", kw)
wiki_search    = lambda **kw: _mcp_call("wiki_search", kw)
memory_remember = lambda **kw: _mcp_call("memory_remember", kw)
memory_search   = lambda **kw: _mcp_call("memory_search", kw)
memory_forget   = lambda id_: _mcp_call("memory_forget", {"id": id_})
memory_status   = lambda: _mcp_call("memory_status", {})


if __name__ == "__main__":
    # Used by the shim itself: `python -m plugins.memory.hermes_memory.legacy <tool> <json_args>`
    if len(sys.argv) < 3:
        print("usage: legacy.py <tool> <json_args>", file=sys.stderr)
        sys.exit(2)
    tool = sys.argv[1]
    args = json.loads(sys.argv[2])
    print(json.dumps(_mcp_call(tool, args)))
