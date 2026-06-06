"""Integration test: CI bootstrap creates hermes_template from scratch.

When CI starts a fresh `pgvector/pgvector:pg18` service container,
`hermes_template` doesn't exist. The bootstrap must create it
with the minimum schemas needed by the 8 PG backends.

This test runs against the real prod PG (127.0.0.1:10432) but uses
a one-off `bootstrap_smoke_<random>` database so it doesn't pollute
the prod template. The bootstrap's idempotency is verified by
running it twice in a row on the same DSN.
"""

from __future__ import annotations

import secrets
import string

import psycopg
import pytest

from hermes_memory.tests.integration.ci_bootstrap import bootstrap_if_needed


def _random_suffix(n: int = 8) -> str:
    return "".join(secrets.choice(string.ascii_lowercase) for _ in range(n))


@pytest.fixture
def admin_dsn(pg_dsn: str) -> str:
    """The `postgres` admin DSN, derived from the test session DSN."""
    return pg_dsn.rsplit("/", 1)[0] + "/postgres"


def test_bootstrap_creates_minimum_schemas(admin_dsn: str) -> None:
    """A fresh database (renamed) can be bootstrapped with all 8 schemas."""
    db = f"hermes_bootstrap_smoke_{_random_suffix()}"
    with psycopg.connect(admin_dsn, autocommit=True) as c, c.cursor() as cur:
        cur.execute(  # pyright: ignore[call-overload]
            f'CREATE DATABASE "{db}"'
        )
    try:
        dsn = admin_dsn.rsplit("/", 1)[0] + f"/{db}"
        # Run the bootstrap DDL directly (we can't call bootstrap_if_needed
        # here because it operates on `hermes_template` and we don't want
        # to touch the prod template in a test).
        from hermes_memory.tests.integration.ci_bootstrap import (
            SCHEMA_DDL,
            _ensure_pgvector_extension,
        )

        _ensure_pgvector_extension(dsn)
        with psycopg.connect(dsn, autocommit=True) as c, c.cursor() as cur:
            cur.execute(SCHEMA_DDL)
        # Verify all 8 schemas + agent_memory exist
        with psycopg.connect(dsn) as c, c.cursor() as cur:
            cur.execute(  # pyright: ignore[call-overload]
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name IN ("
                "'agent_memory', 'hermes_wiki', 'hermes_journal', 'hermes_skills', "
                "'hermes_metrics', 'hermes_kanban', 'hermes_observability', 'hermes_sessions'"
                ")"
            )
            schemas = {row[0] for row in cur.fetchall()}
        assert len(schemas) == 8, f"missing schemas: {schemas}"
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as c, c.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (db,),
            )
            cur.execute(  # pyright: ignore[call-overload]
                f'DROP DATABASE IF EXISTS "{db}"'
            )


def test_bootstrap_idempotent_on_existing_template(pg_dsn: str) -> None:
    """If hermes_template exists, bootstrap_if_needed is a no-op (returns name)."""
    # pg_dsn is on the per-session ephemeral DB; we need admin to check
    # the template's existence, which lives on the same PG.
    admin_dsn = pg_dsn.rsplit("/", 1)[0] + "/postgres"
    # Skip if hermes_template doesn't exist on this PG (e.g. fresh CI)
    with psycopg.connect(admin_dsn, autocommit=True) as c, c.cursor() as cur:
        cur.execute(  # pyright: ignore[call-overload]
            "SELECT 1 FROM pg_database WHERE datname = 'hermes_template'"
        )
        if cur.fetchone() is None:
            pytest.skip("hermes_template not present on this PG")
    result = bootstrap_if_needed(admin_dsn, "hermes_template")
    assert result == "hermes_template"
    # Run again — must still return the name without erroring
    result2 = bootstrap_if_needed(admin_dsn, "hermes_template")
    assert result2 == "hermes_template"
