"""Postgres-backed implementations of the repo base classes.

These subclass the unit-tested base classes in `repos/` and add the
psycopg3 connection + SQL plumbing. They are exercised by the
integration tests (real Postgres). Unit tests don't touch this module.

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

import json
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
from hermes_memory.repos.wiki_repo import Document, WikiRepo


def _json(obj) -> str:
    """Serialize Python → JSON string for psycopg's jsonb columns."""
    if obj is None:
        return "{}"
    return json.dumps(obj)

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
        # Verify the connection at construction so we fail fast.
        with _conn(dsn) as c, c.cursor() as cur:
            cur.execute("SELECT 1")

    def _insert_document(
        self, slug, title, body_md, *, category, tags, metadata
    ) -> int:
        with _conn(self._dsn) as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO hermes_wiki.documents
                    (slug, title, body_md, category, metadata)
                VALUES (%s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (slug) DO UPDATE
                  SET title = EXCLUDED.title,
                      body_md = EXCLUDED.body_md,
                      category = EXCLUDED.category,
                      metadata = EXCLUDED.metadata,
                      updated_at = now()
                RETURNING id
                """,
                (slug, title, body_md, category,
                 _json(metadata)),
            )
            doc_id = cur.fetchone()[0]
            # Tags (best-effort — duplicate names reused via ON CONFLICT)
            for tag in tags or []:
                cur.execute(
                    "INSERT INTO hermes_wiki.tags(name) VALUES (%s) "
                    "ON CONFLICT (name) DO NOTHING",
                    (tag,),
                )
                cur.execute(
                    "INSERT INTO hermes_wiki.document_tags(document_id, tag_id) "
                    "SELECT %s, id FROM hermes_wiki.tags WHERE name = %s "
                    "ON CONFLICT DO NOTHING",
                    (doc_id, tag),
                )
            return doc_id

    def _fetch_document(self, slug) -> Document | None:
        with _conn(self._dsn) as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, slug, title, body_md, category, metadata "
                "FROM hermes_wiki.documents WHERE slug = %s",
                (slug,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                "SELECT t.name FROM hermes_wiki.document_tags dt "
                "JOIN hermes_wiki.tags t ON t.id = dt.tag_id "
                "WHERE dt.document_id = %s ORDER BY t.name",
                (row["id"],),
            )
            tags = tuple(r["name"] for r in cur.fetchall())
            return Document(
                id=row["id"],
                slug=row["slug"],
                title=row["title"],
                body_md=row["body_md"],
                category=row["category"],
                metadata=row["metadata"] or {},
                tags=tags,
            )

    def _insert_link(self, source_slug, target_slug, context) -> bool:
        with _conn(self._dsn) as c, c.cursor() as cur:
            cur.execute(
                "INSERT INTO hermes_wiki.document_links(source_id, target_id, context) "
                "SELECT s.id, t.id, %s FROM hermes_wiki.documents s, hermes_wiki.documents t "
                "WHERE s.slug = %s AND t.slug = %s "
                "ON CONFLICT (source_id, target_id) DO UPDATE SET context = EXCLUDED.context "
                "RETURNING source_id",
                (context, source_slug, target_slug),
            )
            return cur.fetchone() is not None

    def _fetch_backlinks(self, target_slug) -> list[Document]:
        with _conn(self._dsn) as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT s.id, s.slug, s.title, s.body_md, s.category, s.metadata
                FROM hermes_wiki.document_links l
                JOIN hermes_wiki.documents t ON t.id = l.target_id
                JOIN hermes_wiki.documents s ON s.id = l.source_id
                WHERE t.slug = %s
                ORDER BY s.title
                """,
                (target_slug,),
            )
            return [
                Document(
                    id=r["id"], slug=r["slug"], title=r["title"],
                    body_md=r["body_md"], category=r["category"],
                    metadata=r["metadata"] or {}, tags=(),
                )
                for r in cur.fetchall()
            ]

    def _fetch_related(self, slug, max_hops) -> list[Document]:
        # BFS on the document_links graph.
        with _conn(self._dsn) as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id FROM hermes_wiki.documents WHERE slug = %s",
                (slug,),
            )
            row = cur.fetchone()
            if row is None:
                return []
            start = row["id"]
            seen: set[int] = {start}
            frontier: list[int] = [start]
            collected: list[int] = []
            for _ in range(max_hops):
                if not frontier:
                    break
                cur.execute(
                    """
                    SELECT DISTINCT d.id, d.slug, d.title, d.body_md, d.category, d.metadata
                    FROM hermes_wiki.document_links l
                    JOIN hermes_wiki.documents d ON d.id = l.target_id
                    WHERE l.source_id = ANY(%s)
                    """,
                    (frontier,),
                )
                next_frontier: list[int] = []
                for r in cur.fetchall():
                    if r["id"] in seen:
                        continue
                    seen.add(r["id"])
                    next_frontier.append(r["id"])
                    collected.append(r["id"])
                frontier = next_frontier
            if not collected:
                return []
            cur.execute(
                "SELECT id, slug, title, body_md, category, metadata "
                "FROM hermes_wiki.documents WHERE id = ANY(%s) "
                "ORDER BY title",
                (collected,),
            )
            return [
                Document(
                    id=r["id"], slug=r["slug"], title=r["title"],
                    body_md=r["body_md"], category=r["category"],
                    metadata=r["metadata"] or {}, tags=(),
                )
                for r in cur.fetchall()
            ]

    def _search(self, query, *, top_k) -> list[Document]:
        with _conn(self._dsn) as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT id, slug, title, body_md, category, metadata,
                       ts_rank_cd(body_tsv, plainto_tsquery('english', %s)) AS score
                FROM hermes_wiki.documents
                WHERE body_tsv @@ plainto_tsquery('english', %s)
                ORDER BY score DESC
                LIMIT %s
                """,
                (query, query, top_k),
            )
            return [
                Document(
                    id=r["id"], slug=r["slug"], title=r["title"],
                    body_md=r["body_md"], category=r["category"],
                    metadata=r["metadata"] or {}, tags=(),
                )
                for r in cur.fetchall()
            ]


class PgJournalRepo(JournalRepo):
    def __init__(self, dsn):
        super().__init__()
        self._dsn = dsn
        with _conn(dsn) as c, c.cursor() as cur:
            cur.execute("SELECT 1")

    def _insert_session(self, profile, metadata) -> int:
        with _conn(self._dsn) as c, c.cursor() as cur:
            cur.execute(
                "INSERT INTO hermes_journal.sessions(profile, metadata) "
                "VALUES (%s, %s::jsonb) RETURNING id",
                (profile, _json(metadata)),
            )
            row = cur.fetchone()
            assert row is not None
            return row[0]

    def _insert_message(self, session_id, role, content, tool_calls) -> int:
        with _conn(self._dsn) as c, c.cursor() as cur:
            cur.execute(
                "INSERT INTO hermes_journal.messages "
                "(session_id, role, content, tool_calls) "
                "VALUES (%s, %s, %s, %s::jsonb) RETURNING id",
                (session_id, role, content, _json(tool_calls)),
            )
            row = cur.fetchone()
            assert row is not None
            return row[0]

    def _search(self, query, *, top_k, session_id, role):
        from hermes_memory.repos.journal_repo import Message
        clauses: list[str] = [
            "SELECT m.id, m.session_id, m.role, m.content, m.tool_calls, ",
            "ts_rank_cd(m.content_tsv, plainto_tsquery('english', %s)) AS score ",
            "FROM hermes_journal.messages m ",
            "WHERE m.content_tsv @@ plainto_tsquery('english', %s) ",
        ]
        params: list = [query, query]
        if session_id is not None:
            clauses.append("AND m.session_id = %s ")
            params.append(session_id)
        if role is not None:
            clauses.append("AND m.role = %s ")
            params.append(role)
        clauses.append("ORDER BY score DESC LIMIT %s")
        params.append(top_k)
        sql: str = "".join(clauses)
        with _conn(self._dsn) as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)  # type: ignore[arg-type]
            return [
                Message(
                    id=r["id"],
                    session_id=r["session_id"],
                    role=r["role"],
                    content=r["content"],
                    tool_calls=r["tool_calls"],
                )
                for r in cur.fetchall()
            ]


class PgSkillsRepo(SkillsRepo):
    def __init__(self, dsn):
        super().__init__()
        self._dsn = dsn
        with _conn(dsn) as c, c.cursor() as cur:
            cur.execute("SELECT 1")

    def _insert_skill(self, name, version, description, owner, tags) -> bool:
        with _conn(self._dsn) as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO hermes_skills.skills
                    (name, version, owner, description, tags)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (name) DO UPDATE
                  SET version = EXCLUDED.version,
                      owner = EXCLUDED.owner,
                      description = EXCLUDED.description,
                      tags = EXCLUDED.tags,
                      updated_at = now()
                RETURNING id
                """,
                (name, version, owner, description, tags),
            )
            return cur.fetchone() is not None

    def _search(self, query, *, top_k) -> list:
        from hermes_memory.repos.skills_repo import Skill
        with _conn(self._dsn) as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT name, version, owner, description, tags,
                       ts_rank_cd(body_tsv, plainto_tsquery('english', %s)) AS score
                FROM hermes_skills.skills
                WHERE body_tsv @@ plainto_tsquery('english', %s)
                ORDER BY score DESC
                LIMIT %s
                """,
                (query, query, top_k),
            )
            return [
                Skill(
                    name=r["name"],
                    version=r["version"],
                    description=r["description"] or "",
                    owner=r["owner"],
                    tags=tuple(r["tags"] or ()),
                )
                for r in cur.fetchall()
            ]

    def _insert_link(self, source, target, kind) -> bool:
        with _conn(self._dsn) as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO hermes_skills.skill_links(source_id, target_id, kind)
                SELECT s.id, t.id, %s
                FROM hermes_skills.skills s, hermes_skills.skills t
                WHERE s.name = %s AND t.name = %s
                ON CONFLICT (source_id, target_id, kind) DO NOTHING
                RETURNING source_id
                """,
                (kind, source, target),
            )
            return cur.fetchone() is not None

    def _graph(self, root, max_hops) -> dict[str, list[str]]:
        with _conn(self._dsn) as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id FROM hermes_skills.skills WHERE name = %s",
                (root,),
            )
            row = cur.fetchone()
            if row is None:
                return {}
            start = row["id"]
            seen: set[int] = {start}
            frontier: list[int] = [start]
            edges: dict[int, set[int]] = {start: set()}
            for _ in range(max_hops):
                if not frontier:
                    break
                cur.execute(
                    """
                    SELECT l.source_id, l.target_id
                    FROM hermes_skills.skill_links l
                    WHERE l.source_id = ANY(%s) OR l.target_id = ANY(%s)
                    """,
                    (frontier, frontier),
                )
                next_frontier: list[int] = []
                for r in cur.fetchall():
                    for nid in (r["source_id"], r["target_id"]):
                        if nid in seen:
                            continue
                        seen.add(nid)
                        next_frontier.append(nid)
                    edges.setdefault(r["source_id"], set()).add(r["target_id"])
                frontier = next_frontier
            if len(seen) == 1:
                return {root: []}
            ids = list(seen)
            cur.execute(
                "SELECT id, name FROM hermes_skills.skills WHERE id = ANY(%s)",
                (ids,),
            )
            id_to_name = {r["id"]: r["name"] for r in cur.fetchall()}
            out: dict[str, list[str]] = {}
            for sid, targets in edges.items():
                src_name = id_to_name.get(sid)
                if src_name is None:
                    continue
                out[src_name] = [
                    id_to_name[t] for t in targets if t in id_to_name
                ]
            return out


class PgMetricsRepo(MetricsRepo):
    def __init__(self, dsn):
        super().__init__()
        self._dsn = dsn
        with _conn(dsn) as c, c.cursor() as cur:
            cur.execute("SELECT 1")

    def _insert_event(self, profile, name, value, tags) -> int:
        with _conn(self._dsn) as c, c.cursor() as cur:
            cur.execute(
                "INSERT INTO hermes_metrics.events "
                "(ts, profile, metric_name, value, tags) "
                "VALUES (now(), %s, %s, %s, %s::jsonb)",
                (profile, name, value, _json(tags)),
            )
            return 1

    def _query(self, profile, name, *, from_ts, to_ts, bucket):
        from hermes_memory.repos.metrics_repo import MetricPoint
        # Postgres date_trunc wants a unit name (minute, hour, day...), not
        # the human "1 minute" form. Map common bucket strings.
        unit = _bucket_to_pg_unit(bucket)
        clauses = [
            "SELECT date_trunc(%s, ts) AS bucket, "
            "count(*) AS cnt, "
            "percentile_cont(0.5) WITHIN GROUP (ORDER BY value) AS p50, "
            "percentile_cont(0.95) WITHIN GROUP (ORDER BY value) AS p95, "
            "percentile_cont(0.99) WITHIN GROUP (ORDER BY value) AS p99 "
            "FROM hermes_metrics.events "
            "WHERE profile = %s AND metric_name = %s "
        ]
        params: list = [unit, profile, name]
        if from_ts is not None:
            clauses.append("AND ts >= %s ")
            params.append(from_ts)
        if to_ts is not None:
            clauses.append("AND ts <= %s ")
            params.append(to_ts)
        clauses.append("GROUP BY bucket ORDER BY bucket")
        sql: str = "".join(clauses)
        with _conn(self._dsn) as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)  # type: ignore[arg-type]
            return [
                MetricPoint(
                    ts=r["bucket"],
                    p50=float(r["p50"]) if r["p50"] is not None else 0.0,
                    p95=float(r["p95"]) if r["p95"] is not None else 0.0,
                    p99=float(r["p99"]) if r["p99"] is not None else 0.0,
                    count=int(r["cnt"]),
                )
                for r in cur.fetchall()
            ]


def _bucket_to_pg_unit(bucket: str) -> str:
    """Translate "1 minute"/"5 minutes"/"1 hour"/... to date_trunc unit."""
    b = bucket.strip().lower()
    if b.endswith(("minute", "minutes", "min", "mins")):
        return "minute"
    if b.endswith(("hour", "hours", "hr", "hrs")):
        return "hour"
    if b.endswith(("day", "days")):
        return "day"
    if b.endswith(("week", "weeks")):
        return "week"
    if b.endswith(("month", "months")):
        return "month"
    return "minute"  # safe default


class PgKanbanRepo(KanbanRepo):
    def __init__(self, dsn):
        super().__init__()
        self._dsn = dsn
        with _conn(dsn) as c, c.cursor() as cur:
            cur.execute("SELECT 1")

    # ── tenants ────────────────────────────────────────────────────────
    def _insert_tenant(self, slug, name, description, icon, color) -> int:
        with _conn(self._dsn) as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO hermes_kanban.tenants
                    (slug, name, description, icon, color)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (slug) DO UPDATE
                  SET name = EXCLUDED.name,
                      description = EXCLUDED.description,
                      icon = EXCLUDED.icon,
                      color = EXCLUDED.color
                RETURNING id
                """,
                (slug, name, description, icon, color),
            )
            row = cur.fetchone()
            assert row is not None
            return row[0]

    def _fetch_tenants(self):
        from hermes_memory.repos.kanban_repo import Tenant
        with _conn(self._dsn) as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, slug, name, description, icon, color "
                "FROM hermes_kanban.tenants "
                "WHERE archived = false ORDER BY name"
            )
            return [
                Tenant(
                    id=r["id"], slug=r["slug"], name=r["name"],
                    description=r["description"] or "",
                    icon=r["icon"] or "",
                    color=r["color"] or "",
                )
                for r in cur.fetchall()
            ]

    # ── tasks ──────────────────────────────────────────────────────────
    def _insert_task(
        self, task_id, tenant_slug, title, body, status, priority,
        assignee, parent_id, tags, skills_json,
    ) -> None:
        with _conn(self._dsn) as c, c.cursor() as cur:
            # Tenant → tenant_id
            cur.execute(
                "SELECT id FROM hermes_kanban.tenants WHERE slug = %s",
                (tenant_slug,),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"tenant not found: {tenant_slug}")
            tenant_id = row[0]
            cur.execute(
                """
                INSERT INTO hermes_kanban.tasks
                    (id, tenant_id, title, body, status, priority, assignee,
                     skills)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    task_id, tenant_id, title, body, status, priority,
                    assignee, _json(skills_json or []),
                ),
            )
            # Parent link, if any
            if parent_id:
                cur.execute(
                    "INSERT INTO hermes_kanban.task_links(parent_id, child_id) "
                    "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (parent_id, task_id),
                )
            # Event
            cur.execute(
                "INSERT INTO hermes_kanban.task_events "
                "(task_id, kind, actor, payload) "
                "VALUES (%s, 'created', %s, %s::jsonb)",
                (task_id, assignee or "system", _json({"title": title, "tags": tags})),
            )

    def _fetch_tasks(self, tenant_slug, *, status, assignee, limit):
        from hermes_memory.repos.kanban_repo import Task
        clauses = [
            "SELECT t.id, t.title, t.body, t.status, t.priority, t.assignee, "
            "       t.created_at, t.started_at, t.completed_at, t.skills, "
            "       ten.slug AS tenant_slug, "
            "       EXISTS(SELECT 1 FROM hermes_kanban.task_links l "
            "              WHERE l.child_id = t.id) AS has_parent ",
            "FROM hermes_kanban.tasks t "
            "JOIN hermes_kanban.tenants ten ON ten.id = t.tenant_id "
            "WHERE ten.slug = %s "
        ]
        params: list = [tenant_slug]
        if status is not None:
            clauses.append("AND t.status = %s ")
            params.append(status)
        if assignee is not None:
            clauses.append("AND t.assignee = %s ")
            params.append(assignee)
        clauses.append("ORDER BY t.priority DESC, t.created_at LIMIT %s")
        params.append(limit)
        sql: str = "".join(clauses)
        with _conn(self._dsn) as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)  # type: ignore[arg-type]
            return [
                Task(
                    id=r["id"], tenant_slug=r["tenant_slug"],
                    title=r["title"], body=r["body"] or "",
                    status=r["status"], priority=r["priority"],
                    assignee=r["assignee"],
                    parent_id="(has parent)" if r["has_parent"] else None,
                    tags=(),
                    skills_json=r["skills"],
                    created_at=str(r["created_at"]) if r["created_at"] else None,
                    updated_at=str(r["started_at"]) if r["started_at"] else None,
                )
                for r in cur.fetchall()
            ]

    def _fetch_task(self, task_id):
        from hermes_memory.repos.kanban_repo import Task
        with _conn(self._dsn) as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT t.id, t.title, t.body, t.status, t.priority, t.assignee,
                       t.created_at, t.started_at, t.completed_at, t.skills,
                       ten.slug AS tenant_slug,
                       (SELECT parent_id FROM hermes_kanban.task_links
                        WHERE child_id = t.id LIMIT 1) AS parent_id
                FROM hermes_kanban.tasks t
                JOIN hermes_kanban.tenants ten ON ten.id = t.tenant_id
                WHERE t.id = %s
                """,
                (task_id,),
            )
            r = cur.fetchone()
            if r is None:
                return None
            return Task(
                id=r["id"], tenant_slug=r["tenant_slug"],
                title=r["title"], body=r["body"] or "",
                status=r["status"], priority=r["priority"],
                assignee=r["assignee"],
                parent_id=r["parent_id"],
                tags=(),
                skills_json=r["skills"],
                created_at=str(r["created_at"]) if r["created_at"] else None,
                updated_at=str(r["started_at"]) if r["started_at"] else None,
            )

    # ── claim (SKIP LOCKED) ────────────────────────────────────────────
    def _claim_next(self, assignee, max_runtime_seconds):
        from hermes_memory.repos.kanban_repo import Task
        # CTE: pick one ready task with FOR UPDATE SKIP LOCKED, then mark
        # it running. The CTE pattern is the standard
        # claim-from-queue idiom in PG.
        sql: str = """
            WITH next_task AS (
                SELECT id FROM hermes_kanban.tasks
                WHERE status = 'ready'
                ORDER BY priority DESC, created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE hermes_kanban.tasks t
            SET status = 'running',
                assignee = %s,
                started_at = now(),
                max_runtime_seconds = %s
            FROM next_task
            WHERE t.id = next_task.id
            RETURNING t.id, t.title, t.body, t.status, t.priority,
                      t.assignee, t.created_at, t.started_at,
                      t.completed_at, t.skills,
                      (SELECT ten.slug FROM hermes_kanban.tenants ten
                       WHERE ten.id = t.tenant_id) AS tenant_slug
        """
        with _conn(self._dsn) as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, (assignee, max_runtime_seconds or None))
            r = cur.fetchone()
            if r is None:
                return None
            # Record the claim event
            cur.execute(
                "INSERT INTO hermes_kanban.task_events "
                "(task_id, kind, actor, payload) "
                "VALUES (%s, 'claimed', %s, %s::jsonb)",
                (r["id"], assignee, _json({"max_runtime_seconds": max_runtime_seconds})),
            )
            # Start a run record
            cur.execute(
                "INSERT INTO hermes_kanban.task_runs "
                "(task_id, profile, status, max_runtime_seconds) "
                "VALUES (%s, %s, 'running', %s) RETURNING id",
                (r["id"], assignee, max_runtime_seconds or None),
            )
            return Task(
                id=r["id"], tenant_slug=r["tenant_slug"] or "",
                title=r["title"], body=r["body"] or "",
                status=r["status"], priority=r["priority"],
                assignee=r["assignee"],
                parent_id=None,
                tags=(),
                skills_json=r["skills"],
                created_at=str(r["created_at"]) if r["created_at"] else None,
                updated_at=str(r["started_at"]) if r["started_at"] else None,
            )

    # ── heartbeat / complete / fail ────────────────────────────────────
    def _heartbeat(self, task_id) -> bool:
        with _conn(self._dsn) as c, c.cursor() as cur:
            cur.execute(
                "UPDATE hermes_kanban.tasks "
                "SET last_heartbeat_at = now() "
                "WHERE id = %s AND status = 'running' RETURNING id",
                (task_id,),
            )
            return cur.fetchone() is not None

    def _complete_task(self, task_id, summary, result) -> bool:
        with _conn(self._dsn) as c, c.cursor() as cur:
            cur.execute(
                """
                UPDATE hermes_kanban.tasks
                SET status = 'done',
                    completed_at = now(),
                    result = %s
                WHERE id = %s AND status = 'running'
                RETURNING id
                """,
                (result, task_id),
            )
            updated = cur.fetchone() is not None
            if updated:
                cur.execute(
                    "INSERT INTO hermes_kanban.task_events "
                    "(task_id, kind, actor, payload) "
                    "VALUES (%s, 'completed', 'system', %s::jsonb)",
                    (task_id, _json({"summary": summary, "result": result})),
                )
                # Close any running run record
                cur.execute(
                    "UPDATE hermes_kanban.task_runs "
                    "SET status = 'done', ended_at = now(), summary = %s, "
                    "    outcome = 'success' "
                    "WHERE task_id = %s AND status = 'running'",
                    (summary, task_id),
                )
            return updated

    def _fail_task(self, task_id, error, status) -> bool:
        with _conn(self._dsn) as c, c.cursor() as cur:
            cur.execute(
                """
                UPDATE hermes_kanban.tasks
                SET status = %s,
                    last_failure_error = %s,
                    consecutive_failures = consecutive_failures + 1
                WHERE id = %s AND status = 'running'
                RETURNING id
                """,
                (status, error, task_id),
            )
            updated = cur.fetchone() is not None
            if updated:
                cur.execute(
                    "INSERT INTO hermes_kanban.task_events "
                    "(task_id, kind, actor, payload) "
                    "VALUES (%s, 'failed', 'system', %s::jsonb)",
                    (task_id, _json({"error": error, "status": status})),
                )
                cur.execute(
                    "UPDATE hermes_kanban.task_runs "
                    "SET status = %s, ended_at = now(), error = %s "
                    "WHERE task_id = %s AND status = 'running'",
                    (status, error, task_id),
                )
            return updated

    # ── comments + history ─────────────────────────────────────────────
    def _insert_comment(self, task_id, body, author) -> int:
        with _conn(self._dsn) as c, c.cursor() as cur:
            cur.execute(
                "INSERT INTO hermes_kanban.task_comments "
                "(task_id, author, body) VALUES (%s, %s, %s) RETURNING id",
                (task_id, author or "system", body),
            )
            row = cur.fetchone()
            assert row is not None
            cid = row[0]
            cur.execute(
                "INSERT INTO hermes_kanban.task_events "
                "(task_id, kind, actor, payload) "
                "VALUES (%s, 'commented', %s, %s::jsonb)",
                (task_id, author or "system", _json({"comment_id": cid, "body": body})),
            )
            return cid

    def _fetch_history(self, task_id, limit):
        from hermes_memory.repos.kanban_repo import Event
        with _conn(self._dsn) as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, task_id, kind, actor, payload, created_at "
                "FROM hermes_kanban.task_events "
                "WHERE task_id = %s "
                "ORDER BY created_at DESC LIMIT %s",
                (task_id, limit),
            )
            return [
                Event(
                    id=r["id"], task_id=r["task_id"],
                    kind=r["kind"], actor=r["actor"] or "system",
                    payload=r["payload"] or {},
                    created_at=str(r["created_at"]) if r["created_at"] else "",
                )
                for r in cur.fetchall()
            ]

    # ── parent/child links ─────────────────────────────────────────────
    def _insert_link(self, parent_id, child_id) -> bool:
        with _conn(self._dsn) as c, c.cursor() as cur:
            cur.execute(
                "INSERT INTO hermes_kanban.task_links(parent_id, child_id) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING RETURNING parent_id",
                (parent_id, child_id),
            )
            return cur.fetchone() is not None

    def _fetch_children(self, parent_id):
        from hermes_memory.repos.kanban_repo import Task
        with _conn(self._dsn) as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT t.id, t.title, t.body, t.status, t.priority, t.assignee,
                       t.created_at, t.started_at, t.completed_at, t.skills,
                       ten.slug AS tenant_slug
                FROM hermes_kanban.task_links l
                JOIN hermes_kanban.tasks t ON t.id = l.child_id
                JOIN hermes_kanban.tenants ten ON ten.id = t.tenant_id
                WHERE l.parent_id = %s
                """,
                (parent_id,),
            )
            return [
                Task(
                    id=r["id"], tenant_slug=r["tenant_slug"],
                    title=r["title"], body=r["body"] or "",
                    status=r["status"], priority=r["priority"],
                    assignee=r["assignee"], parent_id=parent_id,
                    tags=(), skills_json=r["skills"],
                    created_at=str(r["created_at"]) if r["created_at"] else None,
                )
                for r in cur.fetchall()
            ]

    def _fetch_parents(self, child_id):
        from hermes_memory.repos.kanban_repo import Task
        with _conn(self._dsn) as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT t.id, t.title, t.body, t.status, t.priority, t.assignee,
                       t.created_at, t.started_at, t.completed_at, t.skills,
                       ten.slug AS tenant_slug
                FROM hermes_kanban.task_links l
                JOIN hermes_kanban.tasks t ON t.id = l.parent_id
                JOIN hermes_kanban.tenants ten ON ten.id = t.tenant_id
                WHERE l.child_id = %s
                """,
                (child_id,),
            )
            return [
                Task(
                    id=r["id"], tenant_slug=r["tenant_slug"],
                    title=r["title"], body=r["body"] or "",
                    status=r["status"], priority=r["priority"],
                    assignee=r["assignee"], parent_id=None,
                    tags=(), skills_json=r["skills"],
                    created_at=str(r["created_at"]) if r["created_at"] else None,
                )
                for r in cur.fetchall()
            ]

    # ── notifications ──────────────────────────────────────────────────
    def _insert_subscription(self, task_id, platform, chat_id, thread_id, user_id) -> bool:
        with _conn(self._dsn) as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO hermes_kanban.notify_subs
                    (task_id, platform, chat_id, thread_id, user_id)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (task_id, platform, chat_id, thread_id)
                DO NOTHING
                RETURNING task_id
                """,
                (task_id, platform, chat_id, thread_id or "", user_id),
            )
            return cur.fetchone() is not None

    def _delete_subscription(self, task_id, platform, chat_id, thread_id) -> bool:
        with _conn(self._dsn) as c, c.cursor() as cur:
            cur.execute(
                "DELETE FROM hermes_kanban.notify_subs "
                "WHERE task_id = %s AND platform = %s AND chat_id = %s "
                "  AND thread_id = %s RETURNING task_id",
                (task_id, platform, chat_id, thread_id or ""),
            )
            return cur.fetchone() is not None

    # ── search ─────────────────────────────────────────────────────────
    def _search(self, query, *, tenant_slug, limit):
        from hermes_memory.repos.kanban_repo import Task
        clauses = [
            "SELECT t.id, t.title, t.body, t.status, t.priority, t.assignee, "
            "       t.created_at, t.started_at, t.completed_at, t.skills, "
            "       ten.slug AS tenant_slug, "
            "       ts_rank_cd(t.body_tsv, plainto_tsquery('english', %s)) AS score ",
            "FROM hermes_kanban.tasks t "
            "JOIN hermes_kanban.tenants ten ON ten.id = t.tenant_id "
            "WHERE t.body_tsv @@ plainto_tsquery('english', %s) "
        ]
        params: list = [query, query]
        if tenant_slug is not None:
            clauses.append("AND ten.slug = %s ")
            params.append(tenant_slug)
        clauses.append("ORDER BY score DESC LIMIT %s")
        params.append(limit)
        sql: str = "".join(clauses)
        with _conn(self._dsn) as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)  # type: ignore[arg-type]
            return [
                Task(
                    id=r["id"], tenant_slug=r["tenant_slug"],
                    title=r["title"], body=r["body"] or "",
                    status=r["status"], priority=r["priority"],
                    assignee=r["assignee"],
                    parent_id=None,
                    tags=(), skills_json=r["skills"],
                    created_at=str(r["created_at"]) if r["created_at"] else None,
                )
                for r in cur.fetchall()
            ]


class PgObservabilityRepo(ObservabilityRepo):
    def __init__(self, dsn):
        super().__init__()
        self._dsn = dsn
        with _conn(dsn) as c, c.cursor() as cur:
            cur.execute("SELECT 1")

    def _insert_log(self, event) -> int:
        with _conn(self._dsn) as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO hermes_observability.logs
                    (ts, level, logger, message, profile, metadata)
                VALUES (now(), %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    event.level,
                    "hermes_memory",
                    event.message,
                    event.profile,
                    _json(event.fields),
                ),
            )
            return 1

    def _insert_llm_call(self, call) -> int:
        with _conn(self._dsn) as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO hermes_observability.llm_calls
                    (ts, profile, model,
                     prompt_tokens, completion_tokens, total_tokens,
                     latency_ms, metadata)
                VALUES (now(), %s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    call.profile, call.model,
                    call.prompt_tokens, call.completion_tokens,
                    call.prompt_tokens + call.completion_tokens,
                    call.duration_ms,
                    _json({"status": call.status}),
                ),
            )
            return 1

    def _insert_tool_call(self, call) -> int:
        with _conn(self._dsn) as c, c.cursor() as cur:
            cur.execute(
                """
                INSERT INTO hermes_observability.tool_calls
                    (ts, profile, tool_name, latency_ms, success, error, metadata)
                VALUES (now(), %s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    call.profile, call.tool, call.duration_ms,
                    call.status == "ok", call.error,
                    _json({"status": call.status}),
                ),
            )
            return 1

    def _flush(self) -> int:
        # No buffering in this implementation — autocommit already
        # persists on each insert. Return 0 to honour the contract.
        return 0

    def _close(self) -> None:
        # Pool-owned connections, nothing to close per-instance.
        pass


class PgSessionsRepo(SessionsRepo):
    def __init__(self, dsn):
        super().__init__()
        self._dsn = dsn
        # The base class's contract is `int` for session_id, but the PG
        # schema uses `text` PKs. We bridge by keeping an in-memory
        # int → text mapping for the lifetime of the repo.
        self._sid_map: dict[int, str] = {}
        self._sid_counter = 0
        with _conn(dsn) as c, c.cursor() as cur:
            cur.execute("SELECT 1")

    def _next_session_int(self, text_sid: str) -> int:
        self._sid_counter += 1
        # Make sure the int is positive and unique.
        int_id = self._sid_counter
        # Combine the counter (low bits) with a hash of the text
        # (high bits) so two repos don't accidentally clash if the
        # counters reset.
        import hashlib
        h = int.from_bytes(
            hashlib.sha256(text_sid.encode()).digest()[:4], "big", signed=False
        )
        full = (h << 32) | int_id
        self._sid_map[full] = text_sid
        return full

    def _int_to_sid(self, session_id: int) -> str:
        return self._sid_map[session_id]

    def _insert_session(self, profile, metadata) -> int:
        import secrets
        text_sid = "s_" + secrets.token_urlsafe(16)
        with _conn(self._dsn) as c, c.cursor() as cur:
            cur.execute(
                "INSERT INTO hermes_sessions.sessions(id, profile, metadata) "
                "VALUES (%s, %s, %s::jsonb) RETURNING id",
                (text_sid, profile, _json(metadata)),
            )
            row = cur.fetchone()
            assert row is not None
            return self._next_session_int(row[0])

    def _insert_message(self, session_id, role, content, tool_calls) -> int:
        # session_id is the int returned from _insert_session; reverse
        # the mapping by looking up the actual text id.
        text_sid = self._int_to_sid(session_id)
        with _conn(self._dsn) as c, c.cursor() as cur:
            cur.execute(
                "INSERT INTO hermes_sessions.messages "
                "(session_id, role, content, tool_calls) "
                "VALUES (%s, %s, %s, %s::jsonb) RETURNING id",
                (text_sid, role, content, _json(tool_calls)),
            )
            row = cur.fetchone()
            assert row is not None
            return row[0]

    def _fetch_messages(self, session_id, limit, since):
        from hermes_memory.repos.sessions_repo import SessionMessage
        text_sid = self._int_to_sid(session_id)
        clauses = [
            "SELECT id, session_id, role, content, tool_calls, timestamp "
            "FROM hermes_sessions.messages WHERE session_id = %s "
        ]
        params: list = [text_sid]
        if since is not None:
            clauses.append("AND timestamp >= %s ")
            params.append(since)
        clauses.append("ORDER BY timestamp LIMIT %s")
        params.append(limit)
        sql: str = "".join(clauses)
        with _conn(self._dsn) as c, c.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)  # type: ignore[arg-type]
            return [
                SessionMessage(
                    id=r["id"],
                    session_id=session_id,
                    role=r["role"],
                    content=r["content"],
                    tool_calls=r["tool_calls"],
                    created_at=r["timestamp"],
                )
                for r in cur.fetchall()
            ]

    def _acquire_lock(self, session_id, holder, ttl_seconds) -> bool:
        text_sid = self._int_to_sid(session_id)
        with _conn(self._dsn) as c, c.cursor() as cur:
            # Two-step:
            # 1) Delete any expired locks
            cur.execute(
                "DELETE FROM hermes_sessions.compression_locks "
                "WHERE expires_at < now()"
            )
            # 2) Try to insert a new lock; the PK is session_id so
            #    duplicates fail.
            cur.execute(
                """
                INSERT INTO hermes_sessions.compression_locks
                    (session_id, holder, expires_at)
                VALUES (%s, %s, now() + (%s * interval '1 second'))
                ON CONFLICT (session_id) DO NOTHING
                RETURNING session_id
                """,
                (text_sid, holder, ttl_seconds),
            )
            return cur.fetchone() is not None

    def _release_lock(self, session_id, holder) -> bool:
        text_sid = self._int_to_sid(session_id)
        with _conn(self._dsn) as c, c.cursor() as cur:
            cur.execute(
                "DELETE FROM hermes_sessions.compression_locks "
                "WHERE session_id = %s AND holder = %s RETURNING session_id",
                (text_sid, holder),
            )
            return cur.fetchone() is not None

    def _close_session(self, session_id) -> bool:
        text_sid = self._int_to_sid(session_id)
        with _conn(self._dsn) as c, c.cursor() as cur:
            cur.execute(
                "UPDATE hermes_sessions.sessions "
                "SET ended_at = now() "
                "WHERE id = %s AND ended_at IS NULL RETURNING id",
                (text_sid,),
            )
            return cur.fetchone() is not None

    # ── id mapping helpers ─────────────────────────────────────────────
    # The base class's contract is `int` for session_id, but the PG
    # schema uses `text` PKs. These two helpers keep the in-process
    # int contract intact by mapping text <-> int via a stable hash.
