"""Plugin entry point — loaded by the hermes-agent plugin loader.

This file is dropped verbatim into ~/.hermes/plugins/hermes-postgres-memory/
by `hermes-memory install`. The loader discovers it next to plugin.yaml
and calls `register(ctx)` to wire the plugin's tools/hooks into the agent.

Why a shim and not a symlink?
- Symlinks/editable installs leave dirty entries in
  ~/.hermes/hermes-agent/'s working tree, which `hermes update` then
  flags and refuses. The whole v2 plan is "non-invasive on update".
- A small, stable shim is invisible to `hermes update` and survives
  a clean re-install: `hermes-memory install --step 7` is idempotent
  and re-emits the same file.

Public surface:
- register(ctx): called by the loader once at agent startup
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def register(ctx) -> None:  # type: ignore[no-untyped-def]
    """Wire the v2 hermes-memory plugin into the agent.

    Delegates to the canonical register() in hermes_memory.register.
    That function is what was TDD'd — it builds the 8 surfaces, sets
    `override=True` on the built-in `memory` tool (issue #8 fix), and
    registers the 35+ tools listed in plugin.yaml.

    Args:
        ctx: Plugin context from the hermes-agent loader. We don't
             introspect it here — the v2 register() resolves DSN,
             embedder, and profile from the environment so it stays
             independent of the loader's context shape.
    """
    # Import lazily so the shim stays small and avoids loading
    # psycopg/httpx at agent startup if the plugin is disabled.
    from hermes_memory.register import register as _v2_register

    _v2_register(ctx)
