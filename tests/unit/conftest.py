"""Shared fixtures for unit tests.

The `conn` fixture is also defined here (in addition to test_migrate.py)
so that any unit test in the `tests/unit/` tree can request a real
ephemeral Postgres DB without having to redefine the fixture.

Why TEMPLATE=template0 (not the default template1)?
- The local hermes-postgres container's default template1 can
  pick up objects from prior debug/test runs that did
  `CREATE DATABASE foo` without specifying a clean template.
  On a fresh agent box where someone runs a v1 install.sh that
  calls `CREATE DATABASE`, the install artifacts (schemas,
  tables) end up in template1, and EVERY new database clones
  them. Using template0 sidesteps that.

This is a real footgun observed in the v2 live smoke test
(2026-06-06): a debug script that did `CREATE DATABASE foo`
without `TEMPLATE = template0` polluted template1, and a
subsequent production database (hermes_default) was wiped
during template cleanup. Don't repeat that mistake here.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import psycopg
import psycopg.sql
import pytest


@pytest.fixture
def conn():
    """Per-test ephemeral DB, created from TEMPLATE=template0."""
    pwd_file = Path(os.path.expanduser("~/.hermes/state/hermes-postgres.password"))
    if not pwd_file.exists():
        pytest.skip("no local hermes-postgres available; skipping migration tests")
    real_pwd = pwd_file.read_text().strip()

    # Build the DSN at runtime so the redaction marker doesn't survive
    mark = chr(42) + chr(42) + chr(42)
    admin_dsn = "host=127.0.0.1 port=10432 user=hermes password=" + mark + " dbname=postgres"
    admin_dsn = admin_dsn.replace(mark, real_pwd)

    test_db = f"mig_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin, admin.cursor() as cur:
        # TEMPLATE = template0 is the key: never inherit pollution
        # from prior test runs that may have left schemas/tables
        # in the default template1.
        cur.execute(
            psycopg.sql.SQL("CREATE DATABASE {} TEMPLATE = template0").format(
                psycopg.sql.Identifier(test_db)
            )
        )

    # Use kwargs (not a DSN string) — the libpq shipped with psycopg
    # can drop the dbname field from a space-separated DSN, leaving
    # the conn pointing at the wrong database. Kwargs bypass that.
    test_conn = psycopg.connect(
        host="127.0.0.1",
        port=10432,
        user="hermes",
        password=real_pwd,
        dbname=test_db,
    )
    with test_conn.cursor() as cur:
        for ext in ("vector", "pg_trgm"):
            cur.execute(
                psycopg.sql.SQL("CREATE EXTENSION IF NOT EXISTS {}").format(
                    psycopg.sql.Identifier(ext)
                )
            )
    test_conn.commit()
    try:
        yield test_conn
    finally:
        test_conn.close()
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            with admin.cursor() as cur:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (test_db,),
                )
            with admin.cursor() as cur:
                cur.execute(
                    psycopg.sql.SQL("DROP DATABASE IF EXISTS {}").format(
                        psycopg.sql.Identifier(test_db)
                    )
                )
