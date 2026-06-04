"""PostgreSQL memory plugin for Hermes Agent (canonical schema).

Uses the hermes-memory canonical schema:
  agent_memory.memories    — vector memory with FTS + HNSW
  agent_memory.settings    — config key/value store
  agent_memory.models      — per-dim embedder registry

Schema differences from the old public-table layout:
  - Schema-qualified tables (agent_memory.*)
  - memories.id is bigserial (not uuid)
  - content_tsv is GENERATED ALWAYS (no manual maintenance)
  - deleted_at for soft delete (no is_active boolean)
  - category is ltree text (no memory_categories FK table)
  - source text (no source_session)
  - No confidence column (use metadata if needed)
  - No target column (use tags or metadata)
"""

from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

import psycopg2
import psycopg2.pool
from psycopg2.extensions import make_dsn

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

import sys as _sys
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)
from embedder import (  # noqa: E402
    Embedder, EmbeddingError, SUPPORTED_DIMS, DEFAULT_DIM,
    get_embedder, reset_embedder, get_all_embedders,
)
from budgeter import build_memory_block  # noqa: E402

logger = logging.getLogger(__name__)

_POOL = None
_POOL_LOCK = threading.Lock()

_FTS_WINDOW_OVERFETCH = 4
_FTS_WINDOW_MIN = 40
_HYBRID_TEXT_WEIGHT = 0.5


def _env_float(name: str, default: float, *, minimum: float = 0.0, maximum: float = 1.0) -> float:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        v = float(raw)
    except ValueError:
        logger.warning("Invalid %s=%r; using %s", name, raw, default)
        return default
    return max(minimum, min(maximum, v))


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(int(raw), minimum)
    except ValueError:
        logger.warning("Invalid %s=%r; using %s", name, raw, default)
        return default


# ── Connection-string resolver ──────────────────────────────────────────

def _normalize_pg_mem_dsn(dsn: str) -> str:
    raw = dsn.strip()
    if ";" not in raw or "=" not in raw.split(";", 1)[0]:
        return raw
    mapping = {
        "host": "host", "server": "host", "port": "port",
        "database": "dbname", "dbname": "dbname",
        "user": "user", "username": "user", "userid": "user", "uid": "user",
        "password": "password", "pwd": "password",
        "sslmode": "sslmode",
        "application_name": "application_name", "applicationname": "application_name",
    }
    kwargs: Dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        normalized = mapping.get(key.strip().replace(" ", "").lower())
        if normalized and value.strip():
            kwargs[normalized] = value.strip()
    if not kwargs:
        return raw
    return make_dsn(**kwargs)


def get_pg_mem_db_conn_str() -> str:
    dsn = os.environ.get("PG_MEM_DB_CONN_STR", "").strip()
    if dsn:
        return _normalize_pg_mem_dsn(dsn)
    raise RuntimeError(
        "No postgres connection configured. Set PG_MEM_DB_CONN_STR in "
        "~/.hermes/.env, e.g. "
        "PG_MEM_DB_CONN_STR='postgresql://hermes:***@10.0.0.1:5432/hermes'"
    )


def _postgres_dsn() -> str:
    base = get_pg_mem_db_conn_str()
    connect_timeout = _env_int("HERMES_POSTGRES_CONNECT_TIMEOUT", 5, minimum=1)
    statement_timeout = _env_int("HERMES_POSTGRES_STATEMENT_TIMEOUT_MS", 10_000, minimum=100)
    idle_tx_timeout = _env_int("HERMES_POSTGRES_IDLE_TX_TIMEOUT_MS", 30_000, minimum=100)
    return make_dsn(
        dsn=base,
        sslmode="prefer", connect_timeout=connect_timeout,
        application_name="hermes-memory-postgres",
        options=f"-c statement_timeout={statement_timeout} -c idle_in_transaction_session_timeout={idle_tx_timeout}",
    )


def _get_pool():
    global _POOL
    if _POOL is not None:
        return _POOL
    with _POOL_LOCK:
        if _POOL is None:
            minconn = _env_int("HERMES_POSTGRES_POOL_MIN", 0, minimum=0)
            maxconn = _env_int("HERMES_POSTGRES_POOL_MAX", 2, minimum=1)
            if minconn > maxconn:
                logger.warning("HERMES_POSTGRES_POOL_MIN exceeds max; clamping min to %s", maxconn)
                minconn = maxconn
            _POOL = psycopg2.pool.ThreadedConnectionPool(minconn, maxconn, _postgres_dsn())
        return _POOL


def _close_pool() -> None:
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            return
        _POOL.closeall()
        _POOL = None


# ── Column / dim resolution ─────────────────────────────────────────────

def _vector_column_for_dim(dim: int) -> str:
    if dim == 768:
        return "vector_768"
    if dim == 1024:
        return "vector_1024"
    if dim == 1536:
        return "vector_1536"
    raise ValueError(f"Unsupported dim {dim}. Supported: {list(SUPPORTED_DIMS)}.")


def _read_default_dim(conn) -> int:
    configured = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM agent_memory.settings WHERE key = 'default_dim'"
            )
            row = cur.fetchone()
        if row:
            try:
                configured = int(row[0])
            except (TypeError, ValueError):
                pass
    except psycopg2.errors.UndefinedTable:
        try:
            conn.rollback()
        except Exception:
            pass
    except Exception as exc:
        logger.debug("default_dim lookup failed: %s", exc)
        try:
            conn.rollback()
        except Exception:
            pass

    if configured in SUPPORTED_DIMS:
        return configured
    env_val = os.environ.get("HERMES_EMBED_DEFAULT_DIM", "").strip()
    if env_val:
        try:
            v = int(env_val)
            if v in SUPPORTED_DIMS:
                return v
        except ValueError:
            pass
    return DEFAULT_DIM


def _read_model_config_for_dim(dim: int) -> dict:
    try:
        import psycopg2 as _psy
        dsn = make_dsn(
            dsn=get_pg_mem_db_conn_str(),
            connect_timeout=5,
        )
        conn = _psy.connect(dsn)
    except Exception as exc:
        logger.debug("model config read failed: %s", exc)
        from embedder import _default_model_config_for_dim
        return _default_model_config_for_dim(dim)

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT provider, model, base_url, api_key_env "
                "FROM agent_memory.models WHERE dim = %s",
                (dim,),
            )
            row = cur.fetchone()
        if not row:
            from embedder import _default_model_config_for_dim
            return _default_model_config_for_dim(dim)
        provider, model, base_url, api_key_env = row
        api_key = ""
        if api_key_env:
            api_key = os.environ.get(api_key_env, "").strip()
        if not api_key:
            api_key = os.environ.get(f"HERMES_EMBED_API_KEY_{dim}", "").strip()
        if not api_key:
            api_key = os.environ.get("HERMES_EMBED_API_KEY", "").strip()
        if not api_key and provider == "kimi":
            api_key = os.environ.get("KIMI_API_KEY", "").strip()
        if not api_key and provider == "minimax":
            api_key = os.environ.get("MINIMAX_API_KEY", "").strip()
        if not api_key and provider in ("ollama_local", "ollama_cloud"):
            api_key = os.environ.get("OLLAMA_API_KEY", "").strip()
        return {
            "dim": dim,
            "provider": provider,
            "model": model,
            "api_key": api_key,
            "base_url": base_url or os.environ.get(f"HERMES_EMBED_BASE_URL_{dim}", ""),
        }
    finally:
        conn.close()


# ── PostgreSQL client ───────────────────────────────────────────────────

class _PostgresClient:
    def __init__(self):
        _get_pool()
        with self._cursor() as cur:
            self._default_dim = _read_default_dim(cur.connection)
        logger.info("postgres-memory plugin default_dim=%d", self._default_dim)

    @property
    def default_dim(self) -> int:
        return self._default_dim

    def refresh_default_dim(self) -> int:
        with self._cursor() as cur:
            self._default_dim = _read_default_dim(cur.connection)
        reset_embedder(self._default_dim)
        return self._default_dim

    @contextmanager
    def _cursor(self) -> Iterator[Any]:
        pool = _get_pool()
        conn = pool.getconn()
        cur = None
        try:
            conn.autocommit = True
            cur = conn.cursor()
            yield cur
        finally:
            if cur is not None:
                try:
                    cur.close()
                except Exception:
                    pass
            pool.putconn(conn, close=False)

    # ── CRUD ──────────────────────────────────────────────────────────────

    def add_memory(
        self,
        content: str,
        category: str = "fact",
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict] = None,
        source: Optional[str] = None,
    ) -> str:
        if self._default_dim not in SUPPORTED_DIMS:
            raise ValueError(
                f"Configured default_dim {self._default_dim} is not in "
                f"SUPPORTED_DIMS {list(SUPPORTED_DIMS)}."
            )
        column = _vector_column_for_dim(self._default_dim)
        embedding = get_embedder(self._default_dim).embed(content)

        with self._cursor() as cur:
            now = datetime.now(timezone.utc)
            cur.execute(
                f"""
                INSERT INTO agent_memory.memories
                (content, {column}, category, tags, metadata, source, created_at, updated_at)
                VALUES (%s, %s::vector, %s::ltree, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    content, embedding, category,
                    tags or [], json.dumps(metadata or {}), source,
                    now, now,
                ),
            )
            row = cur.fetchone()
            return str(row[0]) if row else ""

    def search_memories(
        self,
        query: str,
        category: Optional[str] = None,
        top_k: int = 10,
        dim: Optional[int] = None,
    ) -> List[Dict]:
        if dim is not None and dim not in SUPPORTED_DIMS:
            raise ValueError(f"Unsupported dim {dim}. Supported: {list(SUPPORTED_DIMS)}.")
        search_dim = dim if dim in SUPPORTED_DIMS else self._default_dim
        column = _vector_column_for_dim(search_dim)
        query_embedding = get_embedder(search_dim).embed(query)

        where_clauses = ["deleted_at IS NULL"]
        params: List[Any] = []
        if category:
            where_clauses.append("category = %s::ltree")
            params.append(category)
        where_sql = " AND ".join(where_clauses)

        text_weight = _env_float("HERMES_POSTGRES_HYBRID_TEXT_WEIGHT", _HYBRID_TEXT_WEIGHT)
        vector_weight = 1.0 - text_weight
        fts_window = max(top_k * _FTS_WINDOW_OVERFETCH, _FTS_WINDOW_MIN)

        # Hybrid search: FTS candidates + vector rerank
        sql = f"""
            WITH fts_candidates AS (
                SELECT
                    m.id, m.content, m.created_at, m.tags, m.metadata,
                    m.{column} AS embedding_vector,
                    ts_rank(m.content_tsv, plainto_tsquery('english', %s)) AS text_rank
                FROM agent_memory.memories m
                WHERE {where_sql}
                  AND m.{column} IS NOT NULL
                  AND m.content_tsv @@ plainto_tsquery('english', %s)
                ORDER BY text_rank DESC
                LIMIT %s
            )
            SELECT
                id, content, created_at, tags, metadata,
                text_rank,
                (1 - (embedding_vector <=> %s::vector)) AS vector_sim,
                ({text_weight} * COALESCE(text_rank, 0)
                 + {vector_weight} * COALESCE((1 - (embedding_vector <=> %s::vector)), 0)
                ) AS hybrid_score
            FROM fts_candidates
            ORDER BY hybrid_score DESC
            LIMIT %s
        """
        sql_params = [query] + list(params) + [query, fts_window,
                                              query_embedding, query_embedding, top_k]

        with self._cursor() as cur:
            cur.execute(sql, sql_params)
            rows = cur.fetchall()

        return [
            {
                "id": str(r[0]),
                "content": r[1],
                "created_at": r[2].isoformat() if r[2] else None,
                "tags": r[3],
                "metadata": r[4],
                "text_rank": float(r[5]) if r[5] is not None else 0.0,
                "vector_sim": float(r[6]) if r[6] is not None else None,
                "rank": float(r[7]) if r[7] is not None else 0.0,
            }
            for r in rows
        ]

    def get_recent_memories(self, limit: int = 20) -> List[Dict]:
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT id, content, created_at, tags, metadata
                FROM agent_memory.memories
                WHERE deleted_at IS NULL
                ORDER BY created_at DESC LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
        return [
            {
                "id": str(r[0]),
                "content": r[1],
                "created_at": r[2].isoformat() if r[2] else None,
                "tags": r[3],
                "metadata": r[4],
            }
            for r in rows
        ]

    def remove_memory(self, memory_id: str) -> bool:
        with self._cursor() as cur:
            cur.execute(
                "UPDATE agent_memory.memories SET deleted_at = %s WHERE id = %s",
                (datetime.now(timezone.utc), memory_id),
            )
            return cur.rowcount > 0

    def update_memory(self, memory_id: str, content: str) -> bool:
        column = _vector_column_for_dim(self._default_dim)
        embedding = get_embedder(self._default_dim).embed(content)
        with self._cursor() as cur:
            cur.execute(
                f"UPDATE agent_memory.memories SET content = %s, {column} = %s::vector, "
                f"updated_at = %s WHERE id = %s AND deleted_at IS NULL",
                (content, embedding, datetime.now(timezone.utc), memory_id),
            )
            return cur.rowcount > 0

    def count_memories(self) -> int:
        with self._cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM agent_memory.memories WHERE deleted_at IS NULL")
            return cur.fetchone()[0]

    def count_by_dim(self) -> Dict[int, int]:
        out: Dict[int, int] = {}
        with self._cursor() as cur:
            for d in SUPPORTED_DIMS:
                col = _vector_column_for_dim(d)
                cur.execute(
                    f"SELECT COUNT(*) FROM agent_memory.memories "
                    f"WHERE deleted_at IS NULL AND {col} IS NOT NULL "
                    f"AND {col} <> array_fill(0, ARRAY[%s])::vector",
                    (d,),
                )
                out[d] = cur.fetchone()[0]
        return out


# ── Tool schemas ────────────────────────────────────────────────────────

REMEMBER_SCHEMA = {
    "name": "pg_remember",
    "description": (
        "Persist a fact, preference, or observation to the PostgreSQL vector memory store. "
        "Use for anything worth recalling across sessions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The fact to remember."},
            "category": {
                "type": "string",
                "description": "Category path, e.g. 'user.preference', 'environment', 'project.convention'.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags for filtering.",
            },
        },
        "required": ["content"],
    },
}

SEARCH_SCHEMA = {
    "name": "pg_search",
    "description": (
        "Search the PostgreSQL memory store using full-text + semantic hybrid search."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "category": {
                "type": "string",
                "description": "Filter by category path (optional).",
            },
            "dim": {
                "type": "integer",
                "enum": [768, 1024, 1536],
                "description": "Override embedding dim for search.",
            },
            "top_k": {"type": "integer", "description": "Max results (default: 10, max: 50)."},
        },
        "required": ["query"],
    },
}

RECENT_SCHEMA = {
    "name": "pg_recent",
    "description": "List the most recently added memories.",
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Max results (default: 20, max: 100)."},
        },
        "required": [],
    },
}

FORGET_SCHEMA = {
    "name": "pg_forget",
    "description": "Soft-delete a memory by its ID.",
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string", "description": "The memory ID to delete."},
        },
        "required": ["memory_id"],
    },
}

STATUS_SCHEMA = {
    "name": "pg_status",
    "description": "Check PostgreSQL memory store status.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}

MODEL_SET_SCHEMA = {
    "name": "pg_model_set",
    "description": "Switch the default embedding dim and/or model.",
    "parameters": {
        "type": "object",
        "properties": {
            "dim": {"type": "integer", "enum": [768, 1024, 1536],
                    "description": "The new default dim."},
            "provider": {"type": "string", "description": "Override provider."},
            "model": {"type": "string", "description": "Override model name."},
        },
        "required": ["dim"],
    },
}


# ── MemoryProvider implementation ───────────────────────────────────────

class PostgresMemoryProvider(MemoryProvider):
    def __init__(self):
        self._client: Optional[_PostgresClient] = None
        self._session_id = ""

    @property
    def name(self) -> str:
        return "postgres"

    def is_available(self) -> bool:
        try:
            client = _PostgresClient()
            with client._cursor() as cur:
                cur.execute("SELECT EXISTS (SELECT FROM pg_extension WHERE extname = 'vector')")
                has_vector = cur.fetchone()[0]
                cur.execute(
                    "SELECT EXISTS (SELECT FROM information_schema.tables "
                    "WHERE table_schema = 'agent_memory' AND table_name = 'memories')"
                )
                has_table = cur.fetchone()[0]
            return has_vector and has_table
        except Exception:
            return False

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "conn_str",
             "description": "PostgreSQL connection string.",
             "env_var": "PG_MEM_DB_CONN_STR"},
            {"key": "default_dim", "description": "Default embedding dim",
             "default": "1024", "env_var": "HERMES_EMBED_DEFAULT_DIM"},
        ]

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        if self._client is None:
            self._client = _PostgresClient()

    def system_prompt_block(self) -> str:
        if not self._client:
            return ""
        try:
            count = self._client.count_memories()
            d = self._client.default_dim
            return (
                f"# PostgreSQL Vector Memory\n"
                f"Active. {count} memories stored. pgvector with HNSW index, full-text search, hybrid retrieval.\n"
                f"Default embedding dim: {d}."
            )
        except Exception as e:
            logger.warning("Postgres system_prompt_block failed: %s", e)
            return "# PostgreSQL Vector Memory\nActive."

    def prompt_memory_block(self, query: Optional[str] = None, char_budget: int = 2200) -> str:
        """Return a memory block for prompt injection, respecting char budget.
        This is the replacement for built-in MemoryStore.format_for_system_prompt()."""
        if not self._client:
            return ""
        try:
            return build_memory_block(self._client, query=query, char_budget=char_budget)
        except Exception as e:
            logger.warning("Postgres prompt_memory_block failed: %s", e)
            return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._client or not query or len(query.strip()) < 5:
            return ""
        try:
            results = self._client.search_memories(query.strip()[:500], top_k=5)
            if not results:
                return ""
            lines = ["## PostgreSQL Memory Context"]
            for r in results:
                tag_str = f" [{', '.join(r['tags'])}]" if r.get("tags") else ""
                lines.append(f"-{tag_str} {r['content'][:200]}")
            return "\n".join(lines)
        except Exception as e:
            logger.debug("Postgres prefetch failed: %s", e)
            return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        pass

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        pass

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [REMEMBER_SCHEMA, SEARCH_SCHEMA, RECENT_SCHEMA, FORGET_SCHEMA,
                STATUS_SCHEMA, MODEL_SET_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if not self._client:
            return tool_error("PostgreSQL memory provider not initialized")
        try:
            if tool_name == "pg_remember":
                return self._tool_remember(args)
            elif tool_name == "pg_search":
                return self._tool_search(args)
            elif tool_name == "pg_recent":
                return self._tool_recent(args)
            elif tool_name == "pg_forget":
                return self._tool_forget(args)
            elif tool_name == "pg_status":
                return self._tool_status()
            elif tool_name == "pg_model_set":
                return self._tool_model_set(args)
            return tool_error(f"Unknown tool: {tool_name}")
        except Exception as e:
            logger.error("Postgres tool %s failed: %s", tool_name, e)
            return tool_error(f"PostgreSQL tool '{tool_name}' failed: {e}")

    def on_memory_write(self, action: str, target: str, content: str, metadata: Optional[Dict] = None) -> None:
        if action not in {"add", "replace"} or not content or not self._client:
            return
        try:
            category = "user.profile" if target == "user" else "fact"
            self._client.add_memory(
                content=content,
                category=category,
                tags=["mirrored", "builtin", target],
                metadata={"source": "builtin_memory_tool", "action": action, **(metadata or {})},
            )
        except Exception as e:
            logger.debug("Postgres memory mirror failed: %s", e)

    def shutdown(self) -> None:
        _close_pool()

    # -- Tool implementations ------------------------------------------------

    def _tool_remember(self, args: Dict[str, Any]) -> str:
        content = args.get("content", "").strip()
        if not content:
            return tool_error("content is required")
        memory_id = self._client.add_memory(
            content=content,
            category=args.get("category", "fact"),
            tags=args.get("tags", []),
        )
        return json.dumps({"success": True, "memory_id": memory_id,
                           "message": "Memory stored in PostgreSQL."})

    def _tool_search(self, args: Dict[str, Any]) -> str:
        query = args.get("query", "").strip()
        if not query:
            return tool_error("query is required")
        results = self._client.search_memories(
            query,
            category=args.get("category"),
            dim=args.get("dim"),
            top_k=min(args.get("top_k", 10), 50),
        )
        if not results:
            return json.dumps({"results": [], "message": "No matching memories found."})
        return json.dumps({"results": results, "count": len(results)})

    def _tool_recent(self, args: Dict[str, Any]) -> str:
        results = self._client.get_recent_memories(
            limit=min(args.get("limit", 20), 100),
        )
        return json.dumps({"results": results, "count": len(results)})

    def _tool_forget(self, args: Dict[str, Any]) -> str:
        memory_id = args.get("memory_id", "").strip()
        if not memory_id:
            return tool_error("memory_id is required")
        success = self._client.remove_memory(memory_id)
        return json.dumps({"success": success,
                           "message": "Memory deleted." if success else "Memory not found."})

    def _tool_status(self) -> str:
        dsn = get_pg_mem_db_conn_str()
        try:
            from psycopg2.extensions import parse_dsn
            parsed = parse_dsn(dsn)
            host = parsed.get("host", "?")
            port = parsed.get("port", "5432")
            database = parsed.get("dbname", "?")
            user = parsed.get("user", "?")
            display = f"{host}:{port}/{database} (user={user})"
        except Exception:
            import re as _re
            display = _re.sub(r"://[^@/]+@", "://***@", dsn)
        with self._client._cursor() as cur:
            cur.execute("SELECT version()")
            version = cur.fetchone()[0]
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            vector_ver = cur.fetchone()
            cur.execute("SELECT COUNT(*) FROM agent_memory.memories WHERE deleted_at IS NULL")
            total = cur.fetchone()[0]
            cur.execute(
                "SELECT category, COUNT(*) FROM agent_memory.memories "
                "WHERE deleted_at IS NULL GROUP BY category"
            )
            by_category = {}
            for r in cur.fetchall():
                cat = r[0] or "(none)"
                by_category[str(cat)] = r[1]
        per_dim = self._client.count_by_dim()
        embedder_info: Dict[str, Any] = {}
        for d in SUPPORTED_DIMS:
            try:
                e = get_embedder(d)
                embedder_info[str(d)] = {
                    "provider": e.provider, "model": e.model, "stats": e.stats(),
                }
            except Exception as exc:
                embedder_info[str(d)] = {"error": str(exc)}
        return json.dumps({
            "status": "connected",
            "host": display,
            "postgres_version": version,
            "pgvector_version": vector_ver[0] if vector_ver else "not installed",
            "total_memories": total,
            "by_category": by_category,
            "default_dim": self._client.default_dim,
            "per_dim_embedded": per_dim,
            "embedders": embedder_info,
        })

    def _tool_model_set(self, args: Dict[str, Any]) -> str:
        dim = args.get("dim")
        if dim not in SUPPORTED_DIMS:
            return tool_error(f"dim must be one of {list(SUPPORTED_DIMS)}")
        provider = args.get("provider")
        model = args.get("model")
        with self._client._cursor() as cur:
            if provider or model:
                cur.execute(
                    "UPDATE agent_memory.models SET "
                    "  provider = COALESCE(%s, provider), "
                    "  model = COALESCE(%s, model), "
                    "  updated_at = now() "
                    "WHERE dim = %s RETURNING provider, model",
                    (provider, model, dim),
                )
                row = cur.fetchone()
                if not row:
                    cur.execute(
                        "INSERT INTO agent_memory.models (dim, provider, model, api_key_env) "
                        "VALUES (%s, %s, %s, %s) "
                        "ON CONFLICT (dim) DO UPDATE SET "
                        "  provider = EXCLUDED.provider, model = EXCLUDED.model, "
                        "  updated_at = now() "
                        "RETURNING provider, model",
                        (dim, provider or "kimi", model or "bge_m3_embed", "KIMI_API_KEY"),
                    )
                    row = cur.fetchone()
                provider, model = row
            cur.execute(
                "UPDATE agent_memory.settings SET value = %s::jsonb, updated_at = now() "
                "WHERE key = 'default_dim' "
                "RETURNING value",
                (str(dim),),
            )
            row = cur.fetchone()
        reset_embedder(dim)
        new_dim = self._client.refresh_default_dim()
        return json.dumps({
            "success": True,
            "new_default_dim": new_dim,
            "model_for_dim": {"dim": dim, "provider": provider, "model": model},
            "message": f"Default dim is now {new_dim}.",
        })


# ── Plugin entry point ──────────────────────────────────────────────────

def register(ctx) -> None:
    ctx.register_memory_provider(PostgresMemoryProvider())
