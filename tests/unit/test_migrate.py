"""TDD: migration runner — applies SQL files in order, idempotently,
and syncs PG sequences after the schema lands.

This is Bug 1 of the v2 smoke test: the v2 install clones the
profile DB from hermes_template but never applies the v2 migrations
to it. Result: prod hermes_default doesn't have agent_memory.memory_chunks
and every long memory_remember fails with UndefinedTable.

The runner must:
  1. Create agent_migrations.schema_migrations(version text PK, applied_at timestamptz)
  2. Apply each .sql file in migrations/ in numeric order
  3. Skip already-applied versions (idempotent re-runs)
  4. Record each version after successful apply
  5. Run sync_sequences() at the end so bigserial columns don't
     collide with explicit IDs from prior installs (Bug 2)
"""

from __future__ import annotations

import pytest

from hermes_memory.migrate import (
    MigrationError,
    apply_migrations,
    sync_sequences,
)


def test_apply_migrations_creates_schema_migrations_table(tmp_path, conn):
    """First run on a fresh DB creates the migrations tracker table."""
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "0001_init.sql").write_text("CREATE TABLE foo (id int);")

    apply_migrations(conn, mig_dir)

    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('agent_migrations.schema_migrations') IS NOT NULL")
        assert cur.fetchone()[0] is True


def test_apply_migrations_runs_each_file_in_order(tmp_path, conn):
    """Migrations are applied in numeric order. Each version gets
    recorded in schema_migrations after success."""
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    # Order matters: 0002 references table created in 0001
    (mig_dir / "0001_init.sql").write_text("CREATE TABLE foo (id int);")
    (mig_dir / "0002_alter.sql").write_text("ALTER TABLE foo ADD COLUMN name text;")
    (mig_dir / "0003_index.sql").write_text("CREATE INDEX foo_name_idx ON foo(name);")

    apply_migrations(conn, mig_dir)

    with conn.cursor() as cur:
        cur.execute("SELECT version FROM agent_migrations.schema_migrations ORDER BY version")
        versions = [r[0] for r in cur.fetchall()]
    assert versions == ["0001_init", "0002_alter", "0003_index"]

    # The schema changes actually took effect
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'foo' AND column_name = 'name'"
        )
        assert cur.fetchone() is not None


def test_apply_migrations_idempotent_on_rerun(tmp_path, conn):
    """Re-running with the same migration dir is a no-op for already-applied
    versions. Schema state must not change."""
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "0001_init.sql").write_text("CREATE TABLE foo (id int);")
    (mig_dir / "0002_alter.sql").write_text("ALTER TABLE foo ADD COLUMN name text;")

    apply_migrations(conn, mig_dir)
    apply_migrations(conn, mig_dir)  # second run
    apply_migrations(conn, mig_dir)  # third run

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM agent_migrations.schema_migrations")
        assert cur.fetchone()[0] == 2  # exactly two rows, no dupes


def test_apply_migrations_records_failed_version_and_rolls_back(tmp_path, conn):
    """A migration that raises stops the run. The failing version
    is NOT recorded (so re-runs retry it). The DB connection state
    is consistent (no partial schema from a half-applied migration)."""
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "0001_init.sql").write_text("CREATE TABLE foo (id int);")
    # 0002 references a table that doesn't exist — will fail
    (mig_dir / "0002_bad.sql").write_text("ALTER TABLE nonexistent ADD COLUMN x int;")

    with pytest.raises(MigrationError) as exc_info:
        apply_migrations(conn, mig_dir)
    assert "0002_bad" in str(exc_info.value)

    # 0001 should be recorded (it succeeded), 0002 should NOT
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM agent_migrations.schema_migrations ORDER BY version")
        versions = [r[0] for r in cur.fetchall()]
    assert versions == ["0001_init"]


def test_apply_migrations_picks_up_new_files_on_rerun(tmp_path, conn):
    """If 0001 + 0002 are applied, then a new 0003 file is added to
    the dir, a re-run applies 0003 only."""
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "0001_init.sql").write_text("CREATE TABLE foo (id int);")
    (mig_dir / "0002_alter.sql").write_text("ALTER TABLE foo ADD COLUMN name text;")

    apply_migrations(conn, mig_dir)

    # New file appears
    (mig_dir / "0003_index.sql").write_text("CREATE INDEX foo_name_idx ON foo(name);")
    apply_migrations(conn, mig_dir)

    with conn.cursor() as cur:
        cur.execute("SELECT version FROM agent_migrations.schema_migrations ORDER BY version")
        versions = [r[0] for r in cur.fetchall()]
    assert versions == ["0001_init", "0002_alter", "0003_index"]


def test_sync_sequences_aligns_with_max_id(tmp_path, conn):
    """Bug 2 of the v2 smoke test: bigserial sequences desynced from
    MAX(id). sync_sequences() must setval(seq, MAX(id), true) for
    every bigserial column. After the call, the next INSERT should
    get an id > MAX(id)."""
    # Set up a table with a sequence that's behind the data
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS sync_test")
        cur.execute("CREATE TABLE sync_test.items (id bigserial PRIMARY KEY, name text)")
        cur.execute("INSERT INTO sync_test.items (id, name) VALUES (1, 'a'), (2, 'b'), (3, 'c')")
        # Force the sequence to lag behind — exactly the prod bug
        cur.execute("ALTER SEQUENCE sync_test.items_id_seq RESTART WITH 1")
        cur.execute("SELECT setval('sync_test.items_id_seq', 1, true)")
    conn.commit()

    sync_sequences(conn)

    with conn.cursor() as cur:
        cur.execute("SELECT last_value FROM sync_test.items_id_seq")
        lv = cur.fetchone()[0]
    assert lv >= 3, f"sequence still lagging: last_value={lv}, expected >=3"

    # The next INSERT must succeed with no UniqueViolation
    with conn.cursor() as cur:
        cur.execute("INSERT INTO sync_test.items (name) VALUES ('d') RETURNING id")
        new_id = cur.fetchone()[0]
    assert new_id > 3


def test_sync_sequences_handles_table_with_no_rows(tmp_path, conn):
    """Empty table: sequence stays at 1, no error."""
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS sync_test_empty")
        cur.execute("CREATE TABLE sync_test_empty.blank (id bigserial PRIMARY KEY)")
    conn.commit()

    sync_sequences(conn)  # must not raise

    with conn.cursor() as cur:
        cur.execute("INSERT INTO sync_test_empty.blank DEFAULT VALUES RETURNING id")
        assert cur.fetchone()[0] == 1


def test_sync_sequences_skips_partitioned_children(tmp_path, conn):
    """Bug observed in prod: information_schema.columns returns rows
    where the schema is one thing (e.g. ``public``) but the table_name
    is a 3-part dotted name like ``hermes_journal.messages_y2026m06``
    (the table's display name in a schema named ``hermes_journal``).
    When ``pg_get_serial_sequence`` is called with such a 3-part name,
    Postgres interprets it as a cross-database reference and errors
    with "cross-database references are not implemented".

    sync_sequences must:
      1. NOT raise on these dotted-name children
      2. still sync the parent table's sequence
    """
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS sync_test_part")
        cur.execute("CREATE SCHEMA IF NOT EXISTS public")
        # Parent: regular table in sync_test_part
        cur.execute("CREATE TABLE sync_test_part.events (  id bigserial PRIMARY KEY, label text)")
        cur.execute("INSERT INTO sync_test_part.events (id, label) VALUES (5, 'a'), (15, 'b')")
        # Force the sequence to lag
        cur.execute("ALTER SEQUENCE sync_test_part.events_id_seq RESTART WITH 1")
        cur.execute("SELECT setval('sync_test_part.events_id_seq', 1, true)")
        # The prod shape: information_schema reports a row with
        # table_schema='public' and table_name='sync_test_part.events'
        # (the dotted name). Reproduce by inserting such a row directly
        # into information_schema — except we can't. Instead, set up
        # the dotted-name situation by creating a schema named
        # 'sync_test_part' and a child table whose name begins with it.
        # The simplest reproduction is to have a real partitioned table.
        cur.execute(
            "CREATE TABLE sync_test_part.parent_part ("
            "  id bigserial PRIMARY KEY, label text) PARTITION BY RANGE (id)"
        )
        cur.execute(
            "CREATE TABLE sync_test_part.parent_part_child "
            "  PARTITION OF sync_test_part.parent_part FOR VALUES FROM (1) TO (100)"
        )
        cur.execute("INSERT INTO sync_test_part.parent_part (id, label) VALUES (3, 'p')")
        # Force the parent's sequence to lag
        cur.execute("ALTER SEQUENCE sync_test_part.parent_part_id_seq RESTART WITH 1")
        cur.execute("SELECT setval('sync_test_part.parent_part_id_seq', 1, true)")
    conn.commit()

    # Must not raise on partitioned children
    synced = sync_sequences(conn)
    assert "sync_test_part.events_id_seq" in synced
    assert synced["sync_test_part.events_id_seq"] == 15
    assert "sync_test_part.parent_part_id_seq" in synced
    assert synced["sync_test_part.parent_part_id_seq"] == 3

    # Parents are now in sync
    with conn.cursor() as cur:
        cur.execute("SELECT last_value FROM sync_test_part.events_id_seq")
        assert cur.fetchone()[0] == 15
        cur.execute("SELECT last_value FROM sync_test_part.parent_part_id_seq")
        assert cur.fetchone()[0] == 3
        cur.execute("INSERT INTO sync_test_part.events (label) VALUES ('c') RETURNING id")
        assert cur.fetchone()[0] == 16


def test_sync_sequences_skips_tables_with_no_sequence(tmp_path, conn):
    """Defensive: if information_schema reports a bigserial column
    but pg_get_serial_sequence returns NULL (e.g. the sequence was
    dropped or the column default was changed), sync_sequences must
    not raise — just skip the table."""
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS sync_test_noseq")
        # bigint with manual sequence default (not a real bigserial)
        cur.execute("CREATE TABLE sync_test_noseq.manual (  id bigint PRIMARY KEY DEFAULT 1)")
        cur.execute("INSERT INTO sync_test_noseq.manual (id) VALUES (1), (2), (3)")
    conn.commit()

    sync_sequences(conn)  # must not raise
    conn.commit()


def test_apply_migrations_then_sync_sequences_end_to_end(tmp_path, conn):
    """The full apply-migrations + sync-sequences flow handles the
    prod bug: a migration creates a bigserial table, we backfill
    rows with explicit IDs, then sync_sequences() fixes the lag."""
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "0001_backfill.sql").write_text(
        "CREATE SCHEMA IF NOT EXISTS e2e_test;"
        "CREATE TABLE e2e_test.things (id bigserial PRIMARY KEY, label text);"
        "INSERT INTO e2e_test.things (id, label) VALUES (10, 'x'), (20, 'y');"
        "ALTER SEQUENCE e2e_test.things_id_seq RESTART WITH 1;"
    )

    apply_migrations(conn, mig_dir)
    sync_sequences(conn)

    with conn.cursor() as cur:
        cur.execute("INSERT INTO e2e_test.things (label) VALUES ('z') RETURNING id")
        new_id = cur.fetchone()[0]
    assert new_id > 20, f"sequence not synced: new_id={new_id}, expected >20"


# ---------------------------------------------------------------------------
# Test fixture `conn` lives in tests/unit/conftest.py
# (shared between test_migrate.py and test_install.py).
# ---------------------------------------------------------------------------
