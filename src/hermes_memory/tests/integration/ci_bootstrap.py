"""CI bootstrap — create hermes_template from a fresh PG.

When integration tests run against a freshly started PG (e.g. CI's
`pgvector/pgvector:pg18` service container), `hermes_template` does
not exist. `bootstrap_if_needed()` creates it with the minimum
schemas needed by the 8 PG backends. This is intentionally narrower
than the full production `hermes_init.sh` — no timescaledb
hypertables, no age graphs, no pg_cron jobs (none of those are
exercised by the integration tests).

Production users still get the full image via `hermes-memory install`,
which pulls the multi-arch `hermes-postgres:latest` with the full
extension set. The CI bootstrap is for hermes-memory's own test
harness only.
"""

from __future__ import annotations

import psycopg

SCHEMA_DDL = """
-- agent_memory
CREATE SCHEMA IF NOT EXISTS agent_memory;
CREATE TABLE IF NOT EXISTS agent_memory.memories (
    id BIGSERIAL PRIMARY KEY,
    content TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}',
    embedding vector(1024),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS agent_memory.memory_chunks (
    id BIGSERIAL PRIMARY KEY,
    memory_id BIGINT NOT NULL REFERENCES agent_memory.memories(id) ON DELETE CASCADE,
    chunk_index INT NOT NULL,
    content TEXT NOT NULL,
    embedding vector(1024),
    UNIQUE (memory_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS memory_chunks_emb_hnsw ON agent_memory.memory_chunks
    USING hnsw (embedding vector_cosine_ops);

-- hermes_wiki
CREATE SCHEMA IF NOT EXISTS hermes_wiki;
CREATE TABLE IF NOT EXISTS hermes_wiki.documents (
    id BIGSERIAL PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    tags TEXT[] NOT NULL DEFAULT '{}',
    links TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- hermes_journal
CREATE SCHEMA IF NOT EXISTS hermes_journal;
CREATE TABLE IF NOT EXISTS hermes_journal.sessions (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS hermes_journal.messages (
    id BIGSERIAL PRIMARY KEY,
    session_id BIGINT NOT NULL REFERENCES hermes_journal.sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- hermes_skills
CREATE SCHEMA IF NOT EXISTS hermes_skills;
CREATE TABLE IF NOT EXISTS hermes_skills.skills (
    id BIGSERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    body TEXT NOT NULL,
    tags TEXT[] NOT NULL DEFAULT '{}',
    links TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- hermes_metrics
CREATE SCHEMA IF NOT EXISTS hermes_metrics;
CREATE TABLE IF NOT EXISTS hermes_metrics.metrics (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    value DOUBLE PRECISION NOT NULL,
    tags JSONB NOT NULL DEFAULT '{}',
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- hermes_kanban
CREATE SCHEMA IF NOT EXISTS hermes_kanban;
CREATE TABLE IF NOT EXISTS hermes_kanban.boards (
    id BIGSERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS hermes_kanban.columns (
    id BIGSERIAL PRIMARY KEY,
    board_id BIGINT NOT NULL REFERENCES hermes_kanban.boards(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    position INT NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS hermes_kanban.cards (
    id BIGSERIAL PRIMARY KEY,
    column_id BIGINT NOT NULL REFERENCES hermes_kanban.columns(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    position INT NOT NULL DEFAULT 0,
    assignee TEXT,
    state TEXT NOT NULL DEFAULT 'open',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS hermes_kanban.events (
    id BIGSERIAL PRIMARY KEY,
    card_id BIGINT REFERENCES hermes_kanban.cards(id) ON DELETE SET NULL,
    column_id BIGINT REFERENCES hermes_kanban.columns(id) ON DELETE SET NULL,
    actor TEXT,
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- hermes_observability
CREATE SCHEMA IF NOT EXISTS hermes_observability;
CREATE TABLE IF NOT EXISTS hermes_observability.logs (
    id BIGSERIAL PRIMARY KEY,
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    context JSONB NOT NULL DEFAULT '{}',
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS hermes_observability.llm_calls (
    id BIGSERIAL PRIMARY KEY,
    model TEXT NOT NULL,
    prompt_tokens INT NOT NULL,
    completion_tokens INT NOT NULL,
    duration_ms INT NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS hermes_observability.tool_calls (
    id BIGSERIAL PRIMARY KEY,
    tool_name TEXT NOT NULL,
    duration_ms INT NOT NULL,
    succeeded BOOLEAN NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- hermes_sessions
CREATE SCHEMA IF NOT EXISTS hermes_sessions;
CREATE TABLE IF NOT EXISTS hermes_sessions.sessions (
    id BIGSERIAL PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_active_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    compression_lock_id INT
);
CREATE TABLE IF NOT EXISTS hermes_sessions.messages (
    id BIGSERIAL PRIMARY KEY,
    session_id BIGINT NOT NULL REFERENCES hermes_sessions.sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def _ensure_pgvector_extension(admin_dsn: str) -> None:
    """Idempotently create the `vector` extension if missing.

    `pgvector/pgvector:pg18` ships it pre-installed, so this is a
    no-op there. A custom PG base image without pgvector will fail
    loudly here rather than at HNSW index creation time.
    """
    with psycopg.connect(admin_dsn, autocommit=True) as c, c.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")


def _create_template_database(admin_dsn: str, name: str) -> None:
    """Create a fresh database and apply the bootstrap DDL to it."""
    with psycopg.connect(admin_dsn, autocommit=True) as c, c.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}"')
    dsn = admin_dsn.rsplit("/", 1)[0] + f"/{name}"
    _ensure_pgvector_extension(dsn)
    with psycopg.connect(dsn, autocommit=True) as c, c.cursor() as cur:
        cur.execute(SCHEMA_DDL)


def bootstrap_if_needed(admin_dsn: str, template_db: str = "hermes_template") -> str:
    """If `template_db` is missing on this PG, create it with the
    minimum schemas. Returns the template name to use.

    Safe to call repeatedly. Idempotent: a second call with the
    template already present is a single SELECT that returns the name
    unchanged.
    """
    with psycopg.connect(admin_dsn, autocommit=True) as c, c.cursor() as cur:
        cur.execute(  # pyright: ignore[call-overload]
            "SELECT 1 FROM pg_database WHERE datname = %s", (template_db,)
        )
        if cur.fetchone() is not None:
            return template_db
    _create_template_database(admin_dsn, template_db)
    return template_db
