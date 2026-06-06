"""hermes-memory — pure-Python, in-process memory stack for Hermes Agent.

This package ships:
  - 8 storage surfaces (memory, wiki, journal, skills, metrics, kanban,
    observability, sessions) — all PostgreSQL + pgvector
  - The hermes-agent plugin entry point (register.py) with 35+ tools
  - The built-in `memory` tool override (issue #8)
  - 32 KB chunked memory with the routing rule baked into the
    "too large" error (issue #5)
  - A `hermes-memory` CLI: install | uninstall | status | doctor |
    migrate | version | export | import | rollback
  - Migrations 0001..0010 applied automatically on install

Install:
    pip install hermes-memory
    hermes-memory install           # 8-step guided wizard

Issue tracking:
    - #5:  pg_remember chunked to 32KB with routing-rule error
    - #8:  memory tool override (provider=postgres routes to PG)
    - #6,#7: deferred to v2.1 (doctor, dump/restore — already partially
            implemented in the C# tree we replaced)
"""

from __future__ import annotations

__version__ = "2.0.0"

__all__ = [
    "__version__",
    "register",
]
