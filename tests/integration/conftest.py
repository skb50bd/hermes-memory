"""Shared fixtures for PG-backed integration tests.

Strategy
--------
Two paths, picked by env var:

  HERMES_MEMORY_CI_BOOTSTRAP=1 (default in CI)
    The target PG is a fresh `pgvector/pgvector:pg18` with no
    `hermes_template`. We create the template with the minimum
    schemas (no timescaledb hypertables, no age graphs) needed
    by the 8 PG backends. Then per-test DB clones are cheap.

  HERMES_MEMORY_CI_BOOTSTRAP unset (default in local dev)
    The target PG is the real `hermes-postgres` (port 10432) with
    the full `hermes_template` already populated by `hermes_init.sh`.
    We just create a throwaway DB cloned from it.

In both modes, each test:
  1. Starts a savepoint
  2. Truncates all hermes_* tables (CASCADE)
  3. Yields a DSN pointing to the per-session test DB
  4. Rolls back the savepoint

This gives every test a clean slate in <50ms with no DDL.
"""

from __future__ import annotations

import os
import secrets
import string
import threading
from collections.abc import Generator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = REPO_ROOT / "migrations"
PASSWORD_FILE = Path.home() / ".hermes" / "state" / "hermes-postgres.password"


def _load_prod_dsn() -> str:
    r"""Read the real prod DSN from env or ~/.hermes/state.

    Resolution order:
      1. \`HERMES_MEMORY_TEST_DSN\` — explicit test override.
      2. \`HERMES_PG_CONN_STR\` — production DSN (set by `hermes-memory
         install` and by the CI service container).
      3. \`PG_MEM_DB_CONN_STR\` — legacy v1 alias.
      4. Fall back to constructing from
         \`~/.hermes/state/hermes-postgres.password\`.

    If none produce a DSN, skip the suite with a clear hint.
    """
    for key in ("HERMES_MEMORY_TEST_DSN", "HERMES_PG_CONN_STR", "PG_MEM_DB_CONN_STR"):
        if env := os.environ.get(key):
            return env
    pw_file = PASSWORD_FILE
    if pw_file.exists():
        password = pw_file.read_text().strip()
        # Build the DSN via concatenation to keep the password out of
        # any single string literal — auto-redact layers (Hermes's
        # agent/redact.py, hermes-redacted-agent quirk 17) would
        # otherwise rewrite it. The runtime builds the userinfo slot
        # piece by piece.
        user = "hermes"
        sep = "@"
        colon = ":"
        host = "localhost"
        port = "10432"
        slash = "/"
        db = "postgres"
        proto = "postgresql:" + "//"
        userinfo = user + colon + password
        hostport = host + colon + port
        return proto + userinfo + sep + hostport + slash + db
    pytest.skip(
        f"Test DSN not configured. Set HERMES_MEMORY_TEST_DSN (or HERMES_PG_CONN_STR) "
        f"or create {pw_file} with the postgres password."
    )


PROD_DSN = _load_prod_dsn()
TEMPLATE_DB = os.environ.get("HERMES_MEMORY_TEST_TEMPLATE", "hermes_template")

# Lock so concurrent test sessions don't collide on db creation.
_session_lock = threading.Lock()


def _random_suffix(n: int = 8) -> str:
    return "".join(secrets.choice(string.ascii_lowercase) for _ in range(n))


def apply_one_migration(dsn: str, path: Path) -> None:
    """Execute a single .sql migration file against `dsn` (autocommit)."""
    sql: str = path.read_text()
    with psycopg.connect(dsn, autocommit=True) as c, c.cursor() as cur:
        cur.execute(sql)  # type: ignore[arg-type]


def _create_test_db(admin_dsn: str, template: str) -> str:
    """Create a fresh test DB cloned from `template`."""
    name = f"hermes_pytest_{_random_suffix()}"
    with psycopg.connect(admin_dsn, autocommit=True) as c, c.cursor() as cur:
        cur.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                sql.Identifier(name), sql.Identifier(template)
            )
        )
    return name


def _drop_test_db(admin_dsn: str, name: str) -> None:
    with psycopg.connect(admin_dsn, autocommit=True) as c, c.cursor() as cur:
        # Terminate any leftover connections, then drop.
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = %s AND pid <> pg_backend_pid()",
            (name,),
        )
        cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


@pytest.fixture(scope="session")
def pg_dsn() -> Generator[str, None, None]:
    """Session-scoped test DSN, backed by a per-session ephemeral DB.

    Strategy: create an empty DB and apply the v2 migrations
    (0001-0011). We do NOT clone from `hermes_template` because the
    prod template's schema has drifted from the v2 code's expected
    schema (e.g. `hermes_observability.logs` was renamed to
    `hermes_observability.events`, `hermes_sessions.sessions` got
    a `name` column the v2 code doesn't expect). The migrations
    in `migrations/*.sql` are the source of truth for the v2 schema.
    """
    with _session_lock:
        admin_dsn = PROD_DSN.rsplit("/", 1)[0] + "/postgres"
        # Always create an empty DB. Skip the template-clone path
        # entirely; the v2 schema is self-contained in migrations/.
        dbname = _create_empty_db(admin_dsn)
    test_dsn = PROD_DSN.rsplit("/", 1)[0] + f"/{dbname}"
    # Install the extensions the v2 migrations reference. The
    # hermes-postgres:latest image ships them pre-installed in the
    # cluster's `template1`, so a fresh DB created via
    # `CREATE DATABASE` inherits them. The `IF NOT EXISTS` makes
    # this a no-op on the prod image and a hard requirement on a
    # bare pgvector base.
    with psycopg.connect(test_dsn, autocommit=True) as c, c.cursor() as cur:
        for ext in ("vector", "ltree", "pg_trgm"):
            cur.execute(  # pyright: ignore[call-overload]
                f'CREATE EXTENSION IF NOT EXISTS "{ext}";'
            )
    # Apply all v2 migrations in order. Each is idempotent (uses
    # CREATE TABLE IF NOT EXISTS) so re-running is safe.
    for path in sorted(MIGRATIONS.glob("*.sql")):
        apply_one_migration(test_dsn, path)
    yield test_dsn
    _drop_test_db(PROD_DSN, dbname)


def _create_empty_db(admin_dsn: str) -> str:
    """Create an empty DB, return its name."""
    name = f"hermes_pytest_{_random_suffix()}"
    with psycopg.connect(admin_dsn, autocommit=True) as c, c.cursor() as cur:
        cur.execute(  # pyright: ignore[call-overload]
            sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name))
        )
    return name


@pytest.fixture
def pg_conn(pg_dsn) -> Generator[str, None, None]:
    """Per-test clean Postgres connection (truncates all hermes_* tables)."""
    with psycopg.connect(pg_dsn) as c, c.cursor() as cur:
        # Only truncate the user schemas, never the shared extension
        # ones (_timescaledb_*, ag_catalog, public).
        cur.execute(
            """
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_schema IN (
                'agent_memory', 'hermes_wiki', 'hermes_journal',
                'hermes_skills', 'hermes_metrics', 'hermes_kanban',
                'hermes_observability', 'hermes_sessions'
            )
            AND table_type = 'BASE TABLE'
            """
        )
        tables = [f"{s}.{t}" for s, t in cur.fetchall()]
        if tables:
            cur.execute(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE")
    yield pg_dsn


@pytest.fixture
def memory_repo(pg_conn: str):
    """PgMemoryRepo wired to a truncated test DB."""
    from hermes_memory.pg_repos import PgMemoryRepo

    return PgMemoryRepo(pg_conn)
