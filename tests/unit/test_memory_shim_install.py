"""TDD: hermes-memory install drops the postgres MemoryProvider shim at
$HERMES_HOME/plugins/postgres/ so that `hermes memory status` reports
the plugin as installed.

Bug history:
- The v2 plugin (hermes-postgres-memory) lives at
  $HERMES_HOME/plugins/hermes-postgres-memory/ and is picked up by the
  generic plugin loader for tools/hooks/overrides
- But hermes-agent's `hermes memory status` command uses a different
  discovery path: `plugins/memory/__init__.py::discover_memory_providers()`
  which scans $HERMES_HOME/plugins/ directly and applies the
  _is_memory_provider_dir heuristic to each child directory
- The heuristic checks for the literal substring 'MemoryProvider' in
  the first 8KB of __init__.py
- Without a sibling dir at $HERMES_HOME/plugins/postgres/__init__.py,
  the discovery system doesn't see the v2 plugin as a memory provider
- This test asserts the install step drops the shim

Test setup: mock all the install steps to no-ops except _drop_plugin_assets,
then verify both the plugin dir and the memory shim dir exist with the
expected files.
"""

from __future__ import annotations

from hermes_memory.install.steps import (
    PACKAGE_DATA_ROOT,
    RegisterPluginStep,
)


def test_register_plugin_step_drops_memory_shim(tmp_path, monkeypatch):
    """Install drops the postgres MemoryProvider shim alongside the plugin."""

    # Redirect plugin + state + shim to tmp
    plugins_dir = tmp_path / "plugins" / "hermes-postgres-memory"
    shim_dir = tmp_path / "plugins" / "postgres"
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("hermes_memory.install.steps.HERMES_PLUGINS_DIR", plugins_dir)
    monkeypatch.setattr("hermes_memory.install.steps.HERMES_MEMORY_SHIM_DIR", shim_dir)
    monkeypatch.setattr("hermes_memory.install.steps.HERMES_STATE_PATH", state_dir / "state.json")
    monkeypatch.setattr("hermes_memory.install.steps.HERMES_CONFIG_PATH", state_dir / "config.yaml")
    monkeypatch.setattr("hermes_memory.install.steps.HERMES_HOME", tmp_path)

    # The shim should be at <hermes_home>/plugins/postgres/, NOT
    # <hermes_home>/plugins/hermes-postgres-memory/postgres/ and NOT
    # <hermes_home>/plugins/memory/postgres/
    expected_shim_dir = tmp_path / "plugins" / "postgres"

    # Run the step
    step = RegisterPluginStep(state_dir=state_dir)
    result = step.run()

    assert result.success, f"step failed: {result.message}"

    # Plugin dir has the original assets
    assert (plugins_dir / "plugin.yaml").exists()
    assert (plugins_dir / "entry.py").exists()

    # Memory shim dir exists with both files
    assert (expected_shim_dir / "__init__.py").exists(), (
        f"memory shim __init__.py not dropped at {expected_shim_dir / '__init__.py'}"
    )
    assert (expected_shim_dir / "plugin.yaml").exists(), (
        f"memory shim plugin.yaml not dropped at {expected_shim_dir / 'plugin.yaml'}"
    )

    # The __init__.py must satisfy the discovery heuristic
    init_text = (expected_shim_dir / "__init__.py").read_text()
    assert "MemoryProvider" in init_text[:8192], (
        "memory shim __init__.py does not satisfy the discovery "
        "heuristic ('MemoryProvider' substring required in first 8KB)"
    )

    # The __init__.py must implement the register(ctx) pattern
    # (the discovery load path calls register(collector) and looks
    # for collector.register_memory_provider(instance))
    assert "def register(collector)" in init_text or "def register(ctx)" in init_text, (
        "memory shim __init__.py must define a register() function for the discovery load path"
    )


def test_memory_shim_asset_ships_in_package():
    """The shim source must ship inside the installed package so
    install can drop it (otherwise install is broken on a clean
    install from PyPI)."""
    shim_init = PACKAGE_DATA_ROOT / "shim" / "postgres" / "__init__.py"
    shim_yaml = PACKAGE_DATA_ROOT / "shim" / "postgres" / "plugin.yaml"
    assert shim_init.is_file(), f"shim __init__.py not shipped in package: {shim_init}"
    assert shim_yaml.is_file(), f"shim plugin.yaml not shipped in package: {shim_yaml}"
