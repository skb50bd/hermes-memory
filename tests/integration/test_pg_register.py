"""TDD: register._try_build_repos() — all 8 surfaces construct against a real DSN."""

from __future__ import annotations


def test_all_surfaces_construct_with_real_dsn(pg_conn, monkeypatch) -> None:
    """The plugin loader should successfully build all 8 PG repos."""
    from hermes_memory.register import _try_build_repos

    # The function reads HERMES_PG_CONN_STR; we point it at our
    # ephemeral test DB.
    monkeypatch.setenv("HERMES_PG_CONN_STR", pg_conn)

    repos = _try_build_repos()
    assert set(repos.keys()) == {
        "memory",
        "wiki",
        "journal",
        "skills",
        "metrics",
        "kanban",
        "observability",
        "sessions",
    }


def test_no_dsn_returns_empty_dict(monkeypatch) -> None:
    from hermes_memory.register import _try_build_repos

    monkeypatch.delenv("HERMES_PG_CONN_STR", raising=False)
    monkeypatch.delenv("PG_MEM_DB_CONN_STR", raising=False)
    repos = _try_build_repos()
    assert repos == {}
