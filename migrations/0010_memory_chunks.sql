-- 0010_memory_chunks.sql
-- Issue #5: chunked storage for long memories.
--
-- Long pg_remember content (> 512 tokens) is split into overlapping
-- windows and stored as one row per chunk in this table. The parent
-- row in agent_memory.memories stores the full content (no embedding
-- if chunked; the parent's role is to be the human-readable record).
--
-- Each chunk gets its own embedding (1024-dim by default; 768 and
-- 1536 columns added for parity with agent_memory.memories). FTS
-- column is generated for hybrid search.
--
-- Migration is idempotent — safe to re-run.

CREATE EXTENSION IF NOT EXISTS ltree;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS agent_memory.memory_chunks (
    id            bigserial PRIMARY KEY,
    memory_id     bigint NOT NULL REFERENCES agent_memory.memories(id) ON DELETE CASCADE,
    chunk_index   int    NOT NULL,
    content       text   NOT NULL,
    token_count   int    NOT NULL CHECK (token_count > 0),
    vector_768    vector(768),
    vector_1024   vector(1024),
    vector_1536   vector(1536),
    content_tsv   tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    created_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (memory_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_memory_chunks_memory_id
    ON agent_memory.memory_chunks (memory_id);
CREATE INDEX IF NOT EXISTS idx_memory_chunks_tsv
    ON agent_memory.memory_chunks USING GIN (content_tsv);
CREATE INDEX IF NOT EXISTS idx_memory_chunks_vector_768
    ON agent_memory.memory_chunks USING HNSW (vector_768 vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_memory_chunks_vector_1024
    ON agent_memory.memory_chunks USING HNSW (vector_1024 vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_memory_chunks_vector_1536
    ON agent_memory.memory_chunks USING HNSW (vector_1536 vector_cosine_ops);
