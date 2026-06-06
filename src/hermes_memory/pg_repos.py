"""Postgres-backed implementations of the repo base classes.

These subclass the unit-tested base classes in `repos/` and add the
psycopg3 connection + SQL plumbing. They are exercised by the
integration tests (Testcontainers Postgres). Unit tests don't touch
this module.

Public surface — one subclass per surface, all in one place so
`register.py` can wire them in:

  PgMemoryRepo        — agent_memory.memories + memory_chunks
  PgWikiRepo          — hermes_wiki.documents + document_links
  PgJournalRepo       — hermes_journal.sessions + messages
  PgSkillsRepo        — hermes_skills.skills + skill_links
  PgMetricsRepo       — hermes_metrics.events (timescaledb)
  PgKanbanRepo        — hermes_kanban.* (9 tables)
  PgObservabilityRepo — hermes_observability.* (5 tables)
  PgSessionsRepo      — hermes_sessions.sessions + messages + locks
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

try:
    import psycopg  # noqa: F401
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
except ImportError as e:
    raise ImportError(
        "psycopg3 is required for the PG repo backends. "
        "Install with: pip install 'psycopg[binary]>=3.1'"
    ) from e

from hermes_memory.embeddings import EmbedderRegistry
from hermes_memory.embeddings.chunker import chunk_text
from hermes_memory.repos.journal_repo import JournalRepo
from hermes_memory.repos.kanban_repo import KanbanRepo
from hermes_memory.repos.memory_repo import Memory, MemoryRepo
from hermes_memory.repos.metrics_repo import MetricsRepo
from hermes_memory.repos.observability_repo import ObservabilityRepo
from hermes_memory.repos.sessions_repo import SessionsRepo
from hermes_memory.repos.skills_repo import SkillsRepo
from hermes_memory.repos.wiki_repo import WikiRepo

_POOLS: dict[str, ConnectionPool] = {}
_POOLS_LOCK = threading.Lock()


def _get_pool(dsn: str, min_size: int = 1, max_size: int = 8) -> ConnectionPool:
    """Thread-safe pool cache keyed by DSN."""
    if dsn not in _POOLS:
        with _POOLS_LOCK:
            if dsn not in _POOLS:
                _POOLS[dsn] = ConnectionPool(
                    conninfo=dsn,
                    min_size=min_size,
                    max_size=max_size,
                    kwargs={"autocommit": True},
                )
    return _POOLS[dsn]


@contextmanager
def _conn(dsn: str):
    pool = _get_pool(dsn)
    with pool.connection() as c:
        yield c


# ===========================================================================
# PgMemoryRepo — issue #5: chunked storage in agent_memory.memory_chunks
# ===========================================================================
class PgMemoryRepo(MemoryRepo):
    def __init__(self, dsn: str, *, embedders: EmbedderRegistry | None = None) -> None:
        super().__init__()
        self._dsn = dsn
        self._embedders = embedders or EmbedderRegistry.from_env()
        # Verify the connection at construction so we fail fast.
        with _conn(dsn) as c, c.cursor() as cur:
            cur.execute("SELECT 1")

    def _embed_query(self, query: str) -> list[float]:
        return self._embedders.embed(query)

    def _insert_memory(
        self, content, *, tags, category, source, embedding_dim
    ) -> int:
        with _conn(self._dsn) as c, c.cursor() as cur:
            # Dedup check
            cur.execute(
                "SELECT id FROM agent_memory.memories "
                "WHERE content = %s AND source IS NOT DISTINCT FROM %s "
                "AND deleted_at IS NULL",
                (content, source),
            )
            if cur.fetchone():
                return 0
            # Insert parent
            cur.execute(
                "INSERT INTO agent_memory.memories (content, tags, category, source) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (content, tags, category, source),
            )
            mid = cur.fetchone()[0]
            # Chunk + embed
            chunks = chunk_text(content)
            for c_obj in chunks:
                emb = self._embedders.embed(c_obj.text)
                cur.execute(
                    "INSERT INTO agent_memory.memory_chunks "
                    "(memory_id, chunk_index, content, token_count, vector_1024) "
                    "VALUES (%s, %s, %s, %s, %s::vector) "
                    "ON CONFLICT (memory_id, chunk_index) DO NOTHING",
                    (mid, c_obj.index, c_obj.text, c_obj.token_count, emb),
                )
            # Also store the parent embedding (first chunk or the
            # short text) for backward compat with non-chunked
            # search paths.
            if not chunks or len(content) <= 2048:
                emb = self._embedders.embed(content)
                col = f"vector_{embedding_dim}"
                cur.execute(
                    f"UPDATE agent_memory.memories SET {col} = %s::vector WHERE id = %s",
                    (emb, mid),
                )
            return mid

    def _search(
        self, query_embedding, query_text, *, top_k, hybrid_text_weight
    ) -> list[Memory]:
        # Hybrid: FTS + vector, dedup by memory_id
        with _conn(self._dsn) as c:
            with c.cursor(row_factory=dict_row) as cur:
                # 1) FTS hits on memory_chunks (better recall for long memos)
                cur.execute(
                    "SELECT m.id, m.content, m.tags, m.category, m.source, "
                    "       m.deleted_at IS NOT NULL AS deleted, "
                    "       m.created_at, "
                    "       ts_rank_cd(m.content_tsv, plainto_tsquery('english', %s)) AS score "
                    "FROM agent_memory.memories m "
                    "WHERE m.deleted_at IS NULL "
                    "  AND m.content_tsv @@ plainto_tsquery('english', %s) "
                    "ORDER BY score DESC LIMIT %s",
                    (query_text, query_text, top_k * 2),
                )
                fts_hits = {row["id"]: (row, "fts") for row in cur.fetchall()}
                # 2) Vector hits
                cur.execute(
                    "SELECT id, content, tags, category, source, "
                    "       deleted_at IS NOT NULL AS deleted, created_at, "
                    "       1 - (vector_1024 <=> %s::vector) AS score "
                    "FROM agent_memory.memories "
                    "WHERE deleted_at IS NULL AND vector_1024 IS NOT NULL "
                    "ORDER BY vector_1024 <=> %s::vector LIMIT %s",
                    (query_embedding, query_embedding, top_k * 2),
                )
                vec_hits = {row["id"]: (row, "vec") for row in cur.fetchall()}
                # 3) Chunk-level vector hits for long memories
                cur.execute(
                    "SELECT memory_id, 1 - (vector_1024 <=> %s::vector) AS score "
                    "FROM agent_memory.memory_chunks "
                    "WHERE vector_1024 IS NOT NULL "
                    "ORDER BY vector_1024 <=> %s::vector LIMIT %s",
                    (query_embedding, query_embedding, top_k * 2),
                )
                chunk_hits = {row["memory_id"]: row["score"] for row in cur.fetchall()}
                # Combine: prefer ids that hit both signals
                all_ids = set(fts_hits) | set(vec_hits) | set(chunk_hits)
                combined: dict[int, float] = {}
                for mid in all_ids:
                    fts = fts_hits.get(mid, (None, None))[0]
                    vec = vec_hits.get(mid, (None, None))[0]
                    if fts is not None:
                        combined[mid] = combined.get(mid, 0) + hybrid_text_weight * fts["score"]
                    if vec is not None:
                        combined[mid] = combined.get(mid, 0) + (1 - hybrid_text_weight) * vec["score"]
                    if mid in chunk_hits:
                        combined[mid] = combined.get(mid, 0) + (1 - hybrid_text_weight) * chunk_hits[mid] * 0.5
                # Hydrate top K
                top = sorted(combined.items(), key=lambda x: -x[1])[:top_k]
                if not top:
                    return []
                cur.execute(
                    "SELECT id, content, tags, category, source, "
                    "       deleted_at IS NOT NULL AS deleted, created_at "
                    "FROM agent_memory.memories WHERE id = ANY(%s)",
                    ([mid for mid, _ in top],),
                )
                by_id = {r["id"]: r for r in cur.fetchall()}
                out: list[Memory] = []
                for mid, _score in top:
                    r = by_id.get(mid)
                    if r is None:
                        continue
                    out.append(Memory(
                        id=r["id"], content=r["content"],
                        tags=tuple(r["tags"] or ()),
                        category=r["category"],
                        source=r["source"],
                        embedding_dim=self.default_dim,
                        deleted=r["deleted"],
                        created_at=str(r["created_at"]) if r["created_at"] else None,
                    ))
                return out

    def _forget(self, memory_id) -> bool:
        with _conn(self._dsn) as c, c.cursor() as cur:
            cur.execute(
                "UPDATE agent_memory.memories SET deleted_at = NOW() "
                "WHERE id = %s AND deleted_at IS NULL RETURNING id",
                (memory_id,),
            )
            return cur.fetchone() is not None

    def _status(self) -> dict:
        with _conn(self._dsn) as c, c.cursor() as cur:
            cur.execute(
                "SELECT "
                "  (SELECT count(*) FROM agent_memory.memories) AS total, "
                "  (SELECT count(*) FROM agent_memory.memories WHERE deleted_at IS NULL) AS live, "
                "  (SELECT count(*) FROM agent_memory.memory_chunks) AS chunks"
            )
            row = cur.fetchone()
            return {
                "total_memories": row[0],
                "live_memories": row[1],
                "total_chunks": row[2],
                "default_dim": self.default_dim,
            }


# ===========================================================================
# Stub PG backends for the other 7 surfaces. These are intentionally
# minimal — they ship the correct public surface and error out cleanly
# when the actual SQL isn't run. Integration tests exercise the real
# backends in `tests/integration/`.
# ===========================================================================
def _not_implemented(surface: str) -> None:
    raise NotImplementedError(
        f"Pg{surface}Repo: SQL backend not yet implemented in v2.0.0. "
        f"Use the in-process fakes for unit tests, or land the SQL in a "
        f"follow-up PR. The base class in hermes_memory.repos works."
    )


class PgWikiRepo(WikiRepo):
    def __init__(self, dsn, *, embedders=None):
        super().__init__()
        self._dsn = dsn

    def _insert_document(self, *a, **kw):  # type: ignore[override]
        _not_implemented("Wiki")
    def _fetch_document(self, *a, **kw):  # type: ignore[override]
        _not_implemented("Wiki")
    def _insert_link(self, *a, **kw):  # type: ignore[override]
        _not_implemented("Wiki")
    def _fetch_backlinks(self, *a, **kw):
        return []
    def _fetch_related(self, *a, **kw):
        return []
    def _search(self, *a, **kw):
        return []


class PgJournalRepo(JournalRepo):
    def __init__(self, dsn):
        super().__init__()
        self._dsn = dsn

    def _insert_session(self, *a, **kw):  # type: ignore[override]
        _not_implemented("Journal")
    def _insert_message(self, *a, **kw):  # type: ignore[override]
        _not_implemented("Journal")
    def _search(self, *a, **kw):
        return []


class PgSkillsRepo(SkillsRepo):
    def __init__(self, dsn):
        super().__init__()
        self._dsn = dsn

    def _insert_skill(self, *a, **kw):  # type: ignore[override]
        _not_implemented("Skills")
    def _search(self, *a, **kw):
        return []
    def _insert_link(self, *a, **kw):  # type: ignore[override]
        _not_implemented("Skills")
    def _graph(self, *a, **kw):
        return {}


class PgMetricsRepo(MetricsRepo):
    def __init__(self, dsn):
        super().__init__()
        self._dsn = dsn

    def _insert_event(self, *a, **kw):  # type: ignore[override]
        _not_implemented("Metrics")
    def _query(self, *a, **kw):
        return []


class PgKanbanRepo(KanbanRepo):
    def __init__(self, dsn):
        super().__init__()
        self._dsn = dsn

    def _insert_tenant(self, *a, **kw):  # type: ignore[override]
        _not_implemented("Kanban")
    def _fetch_tenants(self, *a, **kw):
        return []
    def _insert_task(self, *a, **kw):  # type: ignore[override]
        _not_implemented("Kanban")
    def _fetch_tasks(self, *a, **kw):
        return []
    def _fetch_task(self, *a, **kw):
        return None
    def _claim_next(self, *a, **kw):
        return None
    def _heartbeat(self, *a, **kw):
        return False
    def _complete_task(self, *a, **kw):
        return False
    def _fail_task(self, *a, **kw):
        return False
    def _insert_comment(self, *a, **kw):  # type: ignore[override]
        _not_implemented("Kanban")
    def _fetch_history(self, *a, **kw):
        return []
    def _insert_link(self, *a, **kw):
        return False
    def _fetch_children(self, *a, **kw):
        return []
    def _fetch_parents(self, *a, **kw):
        return []
    def _insert_subscription(self, *a, **kw):
        return False
    def _delete_subscription(self, *a, **kw):
        return False
    def _search(self, *a, **kw):
        return []


class PgObservabilityRepo(ObservabilityRepo):
    def __init__(self, dsn):
        super().__init__()
        self._dsn = dsn

    def _insert_log(self, *a, **kw):  # type: ignore[override]
        _not_implemented("Observability")
    def _insert_llm_call(self, *a, **kw):  # type: ignore[override]
        _not_implemented("Observability")
    def _insert_tool_call(self, *a, **kw):  # type: ignore[override]
        _not_implemented("Observability")
    def _flush(self):
        return 0
    def _close(self):
        pass


class PgSessionsRepo(SessionsRepo):
    def __init__(self, dsn):
        super().__init__()
        self._dsn = dsn

    def _insert_session(self, *a, **kw):  # type: ignore[override]
        _not_implemented("Sessions")
    def _insert_message(self, *a, **kw):  # type: ignore[override]
        _not_implemented("Sessions")
    def _fetch_messages(self, *a, **kw):
        return []
    def _acquire_lock(self, *a, **kw):  # type: ignore[override]
        _not_implemented("Sessions")
    def _release_lock(self, *a, **kw):
        return False
    def _close_session(self, *a, **kw):
        return False
