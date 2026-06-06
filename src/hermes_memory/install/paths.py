"""Path constants shared by the install wizard and CLI."""

from __future__ import annotations

import os
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()
HERMES_ENV_PATH = HERMES_HOME / ".env"
HERMES_CONFIG_PATH = HERMES_HOME / "config.yaml"
HERMES_STATE_PATH = HERMES_HOME / "state" / "hermes-memory.json"
HERMES_PLUGINS_DIR = HERMES_HOME / "plugins" / "hermes-postgres-memory"

HERMES_PG_CONN_STR_DEFAULT = "postgresql://hermes:***@127.0.0.1:10432/hermes_default"

PLUGIN_NAME = "hermes-postgres-memory"
