"""Memory provider shim for the v2 hermes-postgres-memory plugin.

This file is dropped at $HERMES_HOME/plugins/postgres/__init__.py by
`hermes-memory install`. It satisfies hermes-agent's
`discover_memory_providers()` contract so that `hermes memory status`
reports the plugin as installed.

Discovery contract (from plugins/memory/__init__.py):
- The directory must contain __init__.py at the user plugins root
  (i.e. $HERMES_HOME/plugins/<provider_name>/__init__.py — NOT
  $HERMES_HOME/plugins/memory/<provider_name>/__init__.py as the
  comment in the discovery source suggests; the actual scan iterates
  $HERMES_HOME/plugins/ directly and applies the
  _is_memory_provider_dir heuristic to each child)
- The first 8KB must contain 'MemoryProvider' or
  'register_memory_provider' for the heuristic to recognise this
  as a memory provider directory

Loading contract (from _load_provider_from_dir, lines 295-314 of
plugins/memory/__init__.py):
- If the module has a `register(ctx)` function, hermes-agent calls
  it with a _ProviderCollector; whatever the function passes to
  `ctx.register_memory_provider(instance)` becomes the loaded
  provider
- Otherwise, hermes-agent looks for a class that subclasses
  `agent.memory_provider.MemoryProvider` and instantiates it

This shim uses the `register(ctx)` pattern: we provide a
minimal _PostgresMemoryProvider instance via the collector.
The actual data path lives in the v2 hermes-memory package
(see $HERMES_HOME/plugins/hermes-postgres-memory/plugin.yaml + entry.py
which overrides the built-in `memory` tool entirely per issue #8).
The MemoryProvider ABC's sync_turn/prefetch hooks are NEVER CALLED
for the v2 flow — they only exist here so the status command can
report "installed" + "available" truthfully.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


class _PostgresMemoryProvider:
    """Minimal stand-in for agent.memory_provider.MemoryProvider.

    Intentionally does NOT import the agent's MemoryProvider ABC.
    That ABC is the canonical interface, but importing it here would
    couple this shim to hermes-agent's internals and risk a circular
    import at agent startup. The discovery loader only needs an
    object with `.name` and `.is_available()` to report status.

    The class name 'MemoryProvider' is intentionally NOT used at the
    module level — the discovery heuristic only scans for the
    substring, and using 'MemoryProvider' as our class name would
    shadow the agent's real ABC if anything later in the module
    tried to import it.
    """

    def __init__(self) -> None:
        self._name = "postgres"

    @property
    def name(self) -> str:
        return self._name

    def is_available(self) -> bool:
        """True iff HERMES_PG_CONN_STR is set in the environment.

        Cheap check only — no network. The agent's built-in memory
        path is bypassed regardless, so a False here just downgrades
        the status display; the v2 plugin's tool calls still work.

        The '***' marker check guards against the redaction layer
        leaving the marker literal in the env value (see the
        hermes-redacted-agent skill, quirks 11/11a). When the marker
        is present, the install step's env-heal logic will rewrite
        it from ~/.hermes/state/hermes-pg-*.password at runtime, so
        we treat it as "available" — the install verified reachability.
        """
        dsn = os.environ.get("HERMES_PG_CONN_STR", "").strip()
        if not dsn:
            return False
        marker = chr(42) * 3  # programmatic *** marker (redactor-safe)
        if marker in dsn and "://" in dsn:
            return True
        return True


def register(collector) -> None:
    """Called by hermes-agent's _load_provider_from_dir with a
    _ProviderCollector. We hand it our _PostgresMemoryProvider
    instance; the collector stores it as collector.provider and
    _load_provider_from_dir returns it to the caller.

    The collector's other registration methods (register_tool,
    register_hook, register_cli_command) are no-ops for our flow
    because the v2 plugin's actual tools/hooks are registered
    by the generic plugin loader via $HERMES_HOME/plugins/
    hermes-postgres-memory/entry.py, not through this shim.
    """
    try:
        collector.register_memory_provider(_PostgresMemoryProvider())
    except Exception as e:  # pragma: no cover — defensive
        log.warning("postgres memory shim: register_memory_provider failed: %s", e)


# Re-export the class under the name 'MemoryProvider' to satisfy the
# discovery scanner's substring heuristic in the source (it only
# checks the first 8KB of the file for the literal text 'MemoryProvider').
# The agent's own MemoryProvider ABC is never imported here, so the
# name collision is intentional and contained.
MemoryProvider = _PostgresMemoryProvider  # noqa: F811
