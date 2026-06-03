-- 0001_agent_memory.sql
-- Idempotent migration for the agent_memory schema. This is the same
-- shape as docker/postgres/initdb.d/01-template-bootstrap.sh, exposed
-- here for migration runner use against existing profile DBs that were
-- cloned before this schema was added.

CREATE SCHEMA IF NOT EXISTS agent_memory;

CREATE TABLE IF NOT EXISTS agent_memory.memories (
    id              bigserial PRIMARY KEY,
    content         text NOT NULL,
    content_tsv     tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    vector_768      vector(768),
    vector_1024     vector(1024),
    vector_1536     vector(1536),
    tags            text[] DEFAULT '{}',
    category        ltree,
    metadata        jsonb DEFAULT '{}'::jsonb,
    source          text,
    created_at      timestamptz DEFAULT now(),
    updated_at      timestamptz DEFAULT now(),
    deleted_at      timestamptz
);

CREATE INDEX IF NOT EXISTS idx_memory_tsv         ON agent_memory.memories USING GIN (content_tsv);
CREATE INDEX IF NOT EXISTS idx_memory_vector_768  ON agent_memory.memories USING HNSW (vector_768  vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_memory_vector_1024 ON agent_memory.memories USING HNSW (vector_1024 vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_memory_vector_1536 ON agent_memory.memories USING HNSW (vector_1536 vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_memory_tags        ON agent_memory.memories USING GIN (tags);
CREATE INDEX IF NOT EXISTS idx_memory_category    ON agent_memory.memories USING GIST (category);
CREATE INDEX IF NOT EXISTS idx_memory_created     ON agent_memory.memories (created_at DESC);

CREATE TABLE IF NOT EXISTS agent_memory.models (
    dim          smallint PRIMARY KEY CHECK (dim IN (768, 1024, 1536)),
    provider     text NOT NULL,
    model        text NOT NULL,
    base_url     text,
    api_key_env  text
);
INSERT INTO agent_memory.models (dim, provider, model, base_url, api_key_env) VALUES
    (768,  'ollama_local', 'nomic-embed-text',    NULL, 'OLLAMA_API_KEY'),
    (1024, 'kimi',         'bge_m3_embed',        NULL, 'KIMI_API_KEY'),
    (1536, 'kimi',         'bge_m3_embed',        NULL, 'KIMI_API_KEY')
ON CONFLICT (dim) DO NOTHING;

CREATE TABLE IF NOT EXISTS agent_memory.settings (
    key   text PRIMARY KEY,
    value jsonb NOT NULL
);
INSERT INTO agent_memory.settings (key, value) VALUES
    ('default_dim', '1024'::jsonb),
    ('hybrid_text_weight', '0.5'::jsonb)
ON CONFLICT (key) DO NOTHING;
