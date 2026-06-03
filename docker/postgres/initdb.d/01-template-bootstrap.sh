#!/bin/bash
#
# 01-template-bootstrap.sh
#
# Runs on first cluster init AND on every CREATE DATABASE done after
# (because the postgres entrypoint replays /docker-entrypoint-initdb.d
# for any database that didn't exist at cluster init time).
#
# We gate on POSTGRES_DB: only run the schema bootstrap when we're
# initializing the `hermes_template` database. New profile DBs cloned
# from this template via `CREATE DATABASE ... TEMPLATE hermes_template`
# inherit the schemas byte-perfect and don't need the bootstrap rerun.

set -e

# This env var is exported by the postgres entrypoint based on
# `POSTGRES_DB` (or the default `postgres`).
TARGET_DB="${POSTGRES_DB:-postgres}"

if [ "$TARGET_DB" != "hermes_template" ]; then
    echo "[01-template-bootstrap] Skipping: POSTGRES_DB=$TARGET_DB is not hermes_template"
    exit 0
fi

echo "[01-template-bootstrap] Bootstrapping hermes_template with 5 schemas..."

# Run the schema file against the template DB
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "hermes_template" <<'SQL'
-- =====================================================================
-- 1. agent_memory
-- =====================================================================
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

-- =====================================================================
-- 2. hermes_wiki
-- =====================================================================
CREATE SCHEMA IF NOT EXISTS hermes_wiki;

CREATE TABLE IF NOT EXISTS hermes_wiki.documents (
    id          bigserial PRIMARY KEY,
    slug        text UNIQUE NOT NULL,
    title       text NOT NULL,
    body_md     text NOT NULL,
    body_tsv    tsvector GENERATED ALWAYS AS (to_tsvector('english', body_md)) STORED,
    vector_1024 vector(1024),
    category    ltree,
    metadata    jsonb DEFAULT '{}'::jsonb,
    created_at  timestamptz DEFAULT now(),
    updated_at  timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_wiki_tsv         ON hermes_wiki.documents USING GIN (body_tsv);
CREATE INDEX IF NOT EXISTS idx_wiki_vector_1024 ON hermes_wiki.documents USING HNSW (vector_1024 vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_wiki_category    ON hermes_wiki.documents USING GIST (category);
CREATE INDEX IF NOT EXISTS idx_wiki_slug        ON hermes_wiki.documents (slug);

CREATE TABLE IF NOT EXISTS hermes_wiki.document_links (
    source_id  bigint REFERENCES hermes_wiki.documents(id) ON DELETE CASCADE,
    target_id  bigint REFERENCES hermes_wiki.documents(id) ON DELETE CASCADE,
    context    text,
    created_at timestamptz DEFAULT now(),
    PRIMARY KEY (source_id, target_id)
);
CREATE INDEX IF NOT EXISTS idx_wiki_links_target ON hermes_wiki.document_links (target_id);

CREATE TABLE IF NOT EXISTS hermes_wiki.tags (
    id   serial PRIMARY KEY,
    name text UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS hermes_wiki.document_tags (
    document_id bigint REFERENCES hermes_wiki.documents(id) ON DELETE CASCADE,
    tag_id      int  REFERENCES hermes_wiki.tags(id) ON DELETE CASCADE,
    PRIMARY KEY (document_id, tag_id)
);

-- =====================================================================
-- 3. hermes_journal
-- =====================================================================
CREATE SCHEMA IF NOT EXISTS hermes_journal;

CREATE TABLE IF NOT EXISTS hermes_journal.sessions (
    id         bigserial PRIMARY KEY,
    profile    text NOT NULL,
    started_at timestamptz DEFAULT now(),
    ended_at   timestamptz,
    metadata   jsonb DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_journal_sessions_profile ON hermes_journal.sessions (profile, started_at DESC);

CREATE TABLE IF NOT EXISTS hermes_journal.messages (
    id          bigserial,
    session_id  bigint REFERENCES hermes_journal.sessions(id) ON DELETE CASCADE,
    ts          timestamptz DEFAULT now() NOT NULL,
    role        text NOT NULL CHECK (role IN ('user', 'assistant', 'tool', 'system')),
    content     text NOT NULL,
    tool_calls  jsonb,
    content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    PRIMARY KEY (id, ts)
) PARTITION BY RANGE (ts);
CREATE INDEX IF NOT EXISTS idx_journal_messages_session ON hermes_journal.messages (session_id, ts);
CREATE INDEX IF NOT EXISTS idx_journal_messages_tsv     ON hermes_journal.messages USING GIN (content_tsv);

CREATE TABLE IF NOT EXISTS hermes_journal.messages_default PARTITION OF hermes_journal.messages DEFAULT;

CREATE OR REPLACE FUNCTION hermes_journal.ensure_monthly_partition(p_year int, p_month int)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE
    partition_name text;
    start_date     date;
    end_date       date;
BEGIN
    partition_name := format('hermes_journal.messages_y%sm%s', p_year, lpad(p_month::text, 2, '0'));
    start_date     := make_date(p_year, p_month, 1);
    end_date       := start_date + interval '1 month';
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS %I PARTITION OF hermes_journal.messages FOR VALUES FROM (%L) TO (%L)',
        partition_name, start_date, end_date
    );
END;
$$;

DO $$
DECLARE
    d date;
BEGIN
    FOR d IN
        SELECT (current_date + (n || ' months')::interval)::date
        FROM generate_series(0, 2) AS n
    LOOP
        PERFORM hermes_journal.ensure_monthly_partition(extract(year from d)::int, extract(month from d)::int);
    END LOOP;
END;
$$;

-- =====================================================================
-- 4. hermes_skills
-- =====================================================================
CREATE SCHEMA IF NOT EXISTS hermes_skills;

CREATE TABLE IF NOT EXISTS hermes_skills.skills (
    id          bigserial PRIMARY KEY,
    name        text UNIQUE NOT NULL,
    version     text NOT NULL,
    owner       text,
    description text,
    body_tsv    tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(name, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(description, '')), 'B')
    ) STORED,
    tags        text[] DEFAULT '{}',
    metadata    jsonb DEFAULT '{}'::jsonb,
    created_at  timestamptz DEFAULT now(),
    updated_at  timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_skills_name ON hermes_skills.skills (name);
CREATE INDEX IF NOT EXISTS idx_skills_tsv  ON hermes_skills.skills USING GIN (body_tsv);
CREATE INDEX IF NOT EXISTS idx_skills_tags ON hermes_skills.skills USING GIN (tags);

CREATE TABLE IF NOT EXISTS hermes_skills.skill_links (
    source_id  bigint REFERENCES hermes_skills.skills(id) ON DELETE CASCADE,
    target_id  bigint REFERENCES hermes_skills.skills(id) ON DELETE CASCADE,
    kind       text NOT NULL CHECK (kind IN ('depends_on', 'supersedes', 'related', 'see_also')),
    created_at timestamptz DEFAULT now(),
    PRIMARY KEY (source_id, target_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_skill_links_target ON hermes_skills.skill_links (target_id);

-- =====================================================================
-- 5. hermes_metrics (timescaledb hypertable)
-- =====================================================================
CREATE SCHEMA IF NOT EXISTS hermes_metrics;

CREATE TABLE IF NOT EXISTS hermes_metrics.events (
    ts          timestamptz NOT NULL,
    profile     text NOT NULL,
    metric_name text NOT NULL,
    value       double precision NOT NULL,
    tags        jsonb DEFAULT '{}'::jsonb
);

SELECT create_hypertable('hermes_metrics.events', 'ts',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists => TRUE);

ALTER TABLE hermes_metrics.events SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'profile,metric_name',
    timescaledb.compress_orderby   = 'ts DESC'
);

SELECT add_compression_policy('hermes_metrics.events', INTERVAL '30 days');
SELECT add_retention_policy('hermes_metrics.events', INTERVAL '90 days');

-- =====================================================================
-- 6. public.schema_migrations
-- =====================================================================
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz DEFAULT now(),
    checksum   text
);

-- =====================================================================
-- 7. pg_cron jobs
-- =====================================================================
SELECT cron.schedule(
    'hermes_journal_ensure_partitions',
    '0 0 25 * *',
    $$SELECT hermes_journal.ensure_monthly_partition(
        extract(year  from (current_date + interval '1 month'))::int,
        extract(month from (current_date + interval '1 month'))::int
      );
      SELECT hermes_journal.ensure_monthly_partition(
        extract(year  from (current_date + interval '2 month'))::int,
        extract(month from (current_date + interval '2 month'))::int
      );$$
);
SQL

echo "[01-template-bootstrap] Done."
