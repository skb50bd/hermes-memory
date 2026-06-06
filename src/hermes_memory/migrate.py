"""SQL migration runner for the v2 install.

Apply SQL files in numeric order, idempotently. After all migrations
land, sync PG sequences so the next INSERT doesn't collide with
explicit IDs from prior installs.

Public surface:
- apply_migrations(conn, migrations_dir) -> None
    Apply every .sql in migrations_dir that hasn't been applied yet.
    Each file runs in its own transaction; a failure rolls back
    that file and stops the run (re-runs retry the failing file).
- sync_sequences(conn) -> None
    For every bigserial column in the connected DB, set the
    owning sequence to MAX(column) so the next INSERT picks an
    unused id. This is the fix for the prod v1-install bug where
    backfilled rows left sequences lagging.

Why a separate module from install/steps.py:
- Migration runner is a pure PG function. It can be unit-tested
  with a real PG connection (no docker / no plugin state).
- The install step is a thin wrapper that calls this module.
"""

from __future__ import annotations

import re
from pathlib import Path

import psycopg


class MigrationError(RuntimeError):
    """A migration file raised an exception during apply. Carries the
    version name in the message so install status can surface it."""


class MigrationsAlreadyAppliedError(RuntimeError):
    """Raised only by callers that explicitly want to refuse re-runs.
    The runner itself is idempotent and never raises this."""


# Filename -> version. "0001_foo.sql" -> "0001_foo"
_VERSION_RE = re.compile(r"^(\d{4,}[^.]*)\.sql$")


def _list_migrations(migrations_dir: Path) -> list[tuple[str, Path]]:
    """Return [(version, path), ...] sorted by version (numeric prefix
    sorts lexically iff zero-padded, which the file naming convention
    guarantees)."""
    if not migrations_dir.is_dir():
        raise FileNotFoundError(f"migrations dir not found: {migrations_dir}")
    out: list[tuple[str, Path]] = []
    for p in migrations_dir.iterdir():
        m = _VERSION_RE.match(p.name)
        if not m:
            continue
        out.append((m.group(1), p))
    out.sort(key=lambda vp: vp[0])
    return out


def _ensure_tracker_table(conn: psycopg.Connection) -> None:
    """Create agent_migrations.schema_migrations if absent."""
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS agent_migrations")
        cur.execute(
            "CREATE TABLE IF NOT EXISTS agent_migrations.schema_migrations ("
            "  version    text PRIMARY KEY,"
            "  applied_at timestamptz NOT NULL DEFAULT now()"
            ")"
        )
    conn.commit()


def _applied_versions(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM agent_migrations.schema_migrations")
        return {row[0] for row in cur.fetchall()}


def apply_migrations(conn: psycopg.Connection, migrations_dir: Path) -> list[str]:
    """Apply every .sql in migrations_dir that hasn't been applied yet.

    Returns the list of versions applied during THIS call (in order).
    Already-applied versions are skipped; a failing file aborts the
    run, the connection is rolled back, and the failing version is
    NOT recorded (so the next run retries it).

    Files in the dir that aren't `<digits>_*.sql` are ignored.
    """
    _ensure_tracker_table(conn)
    applied = _applied_versions(conn)
    todo = [(v, p) for v, p in _list_migrations(migrations_dir) if v not in applied]
    newly_applied: list[str] = []
    for version, path in todo:
        sql = path.read_text()
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO agent_migrations.schema_migrations (version) VALUES (%s)",
                    (version,),
                )
            conn.commit()
            newly_applied.append(version)
        except Exception as e:
            conn.rollback()
            raise MigrationError(
                f"migration {version!r} ({path.name}) failed: {type(e).__name__}: {e}"
            ) from e
    return newly_applied


# ---------------------------------------------------------------------------
# Sequence sync — fix for the prod v1-install bug where backfilled
# rows left bigserial sequences behind the table's MAX(id).
# ---------------------------------------------------------------------------


def _find_bigserial_columns(conn: psycopg.Connection) -> list[tuple[str, str, str]]:
    """Return [(schema, table, column), ...] for every bigserial PK-like
    column in the DB.

    Filters:
    - excludes system schemas (pg_catalog, information_schema) and
      agent_migrations
    - INNER JOINs against pg_class to drop orphan rows in
      information_schema.columns (observed in prod: a v1 install created
      then dropped tables; the information_schema rows lingered, and
      pg_get_serial_sequence on them errors with
      "cross-database references are not implemented")
    - skips dotted table_name values like ``hermes_journal.messages_y2026m06``
      (Postgres reports these when a schema-qualified name shows up in
      an old search_path; pg_get_serial_sequence interprets the
      3-part name as database.schema.table and errors)
    """
    sql = (
        "SELECT c.table_schema, c.table_name, c.column_name "
        "FROM information_schema.columns c "
        "JOIN pg_class pc ON pc.relname = c.table_name "
        "JOIN pg_namespace pn ON pn.oid = pc.relnamespace "
        "    AND pn.nspname = c.table_schema "
        "WHERE c.data_type = 'bigint' "
        "  AND c.column_default LIKE 'nextval%' "
        "  AND c.table_schema NOT IN ('pg_catalog', 'information_schema', 'agent_migrations') "
        "  AND c.table_name NOT LIKE '%.%' "
        "ORDER BY c.table_schema, c.table_name, c.ordinal_position"
    )
    with conn.cursor() as cur:
        cur.execute(sql)
        return [(r[0], r[1], r[2]) for r in cur.fetchall()]


def _sequence_name(conn: psycopg.Connection, schema: str, table: str, column: str) -> str | None:
    """Resolve the owning sequence name for a (schema, table, column)
    bigserial column. Returns None if the column has no sequence.

    Defensive: if pg_get_serial_sequence errors (e.g. cross-database
    reference on a stale information_schema row that slipped past the
    INNER JOIN filter), log a warning and return None so sync_sequences
    can continue with the rest of the tables.
    """
    fqtn = f"{schema}.{table}"
    with conn.cursor() as cur:
        try:
            cur.execute("SELECT pg_get_serial_sequence(%s, %s)", (fqtn, column))
        except psycopg.Error:
            # Roll back the failed txn so the connection is reusable,
            # then return None. Caller will skip this table.
            conn.rollback()
            return None
        row = cur.fetchone()
        return row[0] if row else None


def sync_sequences(conn: psycopg.Connection) -> dict[str, int]:
    """For every bigserial column, set its owning sequence to
    COALESCE(MAX(column), 1). Returns a dict of
    {sequence_name: new_last_value} for inspection/logging.

    Idempotent. Safe to run on a DB with no data (sets to 1).

    The `is_called` flag matters: setval(seq, n, true) means the
    sequence is "primed" — next nextval returns n+1. For an empty
    table, we want nextval to return 1, so is_called=false. For a
    populated table, we want nextval to return MAX+1, so is_called=true.
    """
    updates: dict[str, int] = {}
    for schema, table, column in _find_bigserial_columns(conn):
        seq = _sequence_name(conn, schema, table, column)
        if not seq:
            continue
        with conn.cursor() as cur:
            cur.execute(
                'SELECT COALESCE(MAX("' + column + '"), 0) FROM "' + schema + '"."' + table + '"'
            )
            max_id_row = cur.fetchone()
            max_id = max_id_row[0] if max_id_row else 0
        # is_called=false on an empty table means nextval will return
        # 1 (the sequence's natural starting value). is_called=true on
        # a populated table means nextval returns MAX+1.
        if max_id == 0:
            with conn.cursor() as cur:
                cur.execute("SELECT setval(%s, 1, false)", (seq,))
        else:
            with conn.cursor() as cur:
                cur.execute("SELECT setval(%s, %s, true)", (seq, max_id))
        updates[seq] = int(max_id)
    conn.commit()
    return updates


# Re-export for tests that want to introspect what was changed
__all__ = [
    "MigrationError",
    "MigrationsAlreadyAppliedError",
    "apply_migrations",
    "sync_sequences",
    "_find_bigserial_columns",
    "_list_migrations",
]
