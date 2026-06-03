-- 01-schemas.sql
-- Idempotent. Creates the 5 schemas that the hermes-memory platform uses.
-- Run once against `hermes_template` AFTER 00-extensions.sql:
--   psql -U postgres -d hermes_template -f 01-schemas.sql
-- The result is a self-contained template DB. Every profile DB created
-- from it via `hermes-memory profile create <name>` is a byte-perfect
-- clone and needs no further setup.

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
RETURNS void LANGUAGE plpgsql AS $$
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
DECLARE d date;
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
-- TimescaleDB 2.x (since mid-2024) is AGPL-licensed. The "apache" tier
-- includes hypertables and basic queries. Compression, retention policies,
-- and continuous aggregates are PAID features ("timescale" tier).
-- So: keep the hypertable (free) and let application code handle
-- retention/aggregation. No add_compression_policy / add_retention_policy.
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

-- Drop the metrics table after 1 year via a pg_cron job (free).
-- App code is responsible for querying with time_bucket() and aggregations.

-- =====================================================================
-- 6. hermes_kanban (the 8-table task dispatcher)
-- =====================================================================
-- Multi-tenant kanban. tenants is the new first-class replacement for
-- the old free-form tasks.tenant text column. A "board" is a view over
-- a tenant's tasks.
CREATE SCHEMA IF NOT EXISTS hermes_kanban;

CREATE TABLE IF NOT EXISTS hermes_kanban.tenants (
    id              bigserial PRIMARY KEY,
    slug            text UNIQUE NOT NULL,
    name            text NOT NULL,
    description     text,
    icon            text,
    color           text,
    default_workdir text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    archived        boolean NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS hermes_kanban.tasks (
    id                   text PRIMARY KEY,
    tenant_id            bigint NOT NULL REFERENCES hermes_kanban.tenants(id) ON DELETE RESTRICT,
    title                text NOT NULL,
    body                 text,
    assignee             text,
    status               text NOT NULL
        CHECK (status IN ('ready','running','blocked','done','crashed','timed_out','failed','archived','cancelled')),
    priority             integer NOT NULL DEFAULT 0,
    created_by           text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    started_at           timestamptz,
    completed_at         timestamptz,
    workspace_kind       text NOT NULL DEFAULT 'scratch',
    workspace_path       text,
    branch_name          text,
    result               text,
    idempotency_key      text,
    consecutive_failures integer NOT NULL DEFAULT 0,
    worker_pid           integer,
    last_failure_error   text,
    max_runtime_seconds  integer,
    last_heartbeat_at    timestamptz,
    current_run_id       bigint,
    workflow_template_id text,
    current_step_key     text,
    skills               jsonb NOT NULL DEFAULT '[]'::jsonb,
    model_override       text,
    max_retries          integer,
    session_id           text,
    goal_mode            integer NOT NULL DEFAULT 0,
    goal_max_turns       integer,
    body_tsv             tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(title,'') || ' ' || coalesce(body,''))) STORED
);

CREATE INDEX IF NOT EXISTS idx_kanban_tasks_tenant_status ON hermes_kanban.tasks(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_kanban_tasks_status        ON hermes_kanban.tasks(status);
CREATE INDEX IF NOT EXISTS idx_kanban_tasks_assignee      ON hermes_kanban.tasks(assignee, status);
CREATE INDEX IF NOT EXISTS idx_kanban_tasks_idempotency   ON hermes_kanban.tasks(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_kanban_tasks_session       ON hermes_kanban.tasks(session_id);
CREATE INDEX IF NOT EXISTS idx_kanban_tasks_priority       ON hermes_kanban.tasks(priority DESC, created_at);
CREATE INDEX IF NOT EXISTS idx_kanban_tasks_tsv           ON hermes_kanban.tasks USING GIN (body_tsv);

CREATE TABLE IF NOT EXISTS hermes_kanban.task_runs (
    id                  bigserial PRIMARY KEY,
    task_id             text NOT NULL REFERENCES hermes_kanban.tasks(id) ON DELETE CASCADE,
    profile             text,
    step_key            text,
    status              text NOT NULL
        CHECK (status IN ('running','done','blocked','crashed','timed_out','failed','released')),
    claim_lock          text,
    claim_expires       timestamptz,
    worker_pid          integer,
    max_runtime_seconds integer,
    last_heartbeat_at   timestamptz,
    started_at          timestamptz NOT NULL DEFAULT now(),
    ended_at            timestamptz,
    outcome             text,
    summary             text,
    metadata            jsonb,
    error               text
);
CREATE INDEX IF NOT EXISTS idx_kanban_runs_task    ON hermes_kanban.task_runs(task_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_kanban_runs_status  ON hermes_kanban.task_runs(status);

CREATE TABLE IF NOT EXISTS hermes_kanban.task_events (
    id         bigserial PRIMARY KEY,
    task_id    text NOT NULL REFERENCES hermes_kanban.tasks(id) ON DELETE CASCADE,
    run_id     bigint REFERENCES hermes_kanban.task_runs(id) ON DELETE SET NULL,
    kind       text NOT NULL,
    payload    jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_kanban_events_task ON hermes_kanban.task_events(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_kanban_events_run  ON hermes_kanban.task_events(run_id, id);

CREATE TABLE IF NOT EXISTS hermes_kanban.task_links (
    parent_id text NOT NULL REFERENCES hermes_kanban.tasks(id) ON DELETE CASCADE,
    child_id  text NOT NULL REFERENCES hermes_kanban.tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (parent_id, child_id)
);
CREATE INDEX IF NOT EXISTS idx_kanban_links_child ON hermes_kanban.task_links(child_id);

CREATE TABLE IF NOT EXISTS hermes_kanban.task_comments (
    id         bigserial PRIMARY KEY,
    task_id    text NOT NULL REFERENCES hermes_kanban.tasks(id) ON DELETE CASCADE,
    author     text NOT NULL,
    body       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_kanban_comments_task ON hermes_kanban.task_comments(task_id, created_at);

CREATE TABLE IF NOT EXISTS hermes_kanban.task_attachments (
    id           bigserial PRIMARY KEY,
    task_id      text NOT NULL REFERENCES hermes_kanban.tasks(id) ON DELETE CASCADE,
    filename     text NOT NULL,
    stored_path  text NOT NULL,
    content_type text,
    size         bigint NOT NULL DEFAULT 0,
    uploaded_by  text,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_kanban_attachments_task ON hermes_kanban.task_attachments(task_id, created_at);

CREATE TABLE IF NOT EXISTS hermes_kanban.notify_subs (
    task_id          text NOT NULL REFERENCES hermes_kanban.tasks(id) ON DELETE CASCADE,
    platform         text NOT NULL,
    chat_id          text NOT NULL,
    thread_id        text NOT NULL DEFAULT '',
    user_id          text,
    notifier_profile text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    last_event_id    bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, platform, chat_id, thread_id)
);
CREATE INDEX IF NOT EXISTS idx_kanban_notify_task ON hermes_kanban.notify_subs(task_id);

-- Convenience view: a "board" is just a tenant's tasks.
CREATE OR REPLACE VIEW hermes_kanban.v_board_tasks AS
SELECT
    t.*,
    ten.slug AS tenant_slug,
    ten.name AS tenant_name
FROM hermes_kanban.tasks t
JOIN hermes_kanban.tenants ten ON ten.id = t.tenant_id
WHERE ten.archived = false;

-- =====================================================================
-- 7. public.schema_migrations
-- =====================================================================
CREATE TABLE IF NOT EXISTS public.schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz DEFAULT now(),
    checksum   text
);

-- =====================================================================
-- 8. pg_cron jobs
-- =====================================================================
-- pg_cron is configured (via postgresql.conf) to read jobs from
-- `hermes_template`. BUT — `hermes_template` must be clonable, and
-- `CREATE DATABASE ... TEMPLATE` refuses if the source has active
-- connections. pg_cron + TimescaleDB each keep one idle session pinned
-- to cron.database_name, which blocks the clone.
--
-- Workaround: don't keep ANY user-database pin in hermes_template. Move
-- the cron jobs to a dedicated `hermes_cron` database that's NOT cloned.
-- Profile DBs are clones of `hermes_template` (clean, no active sessions).
-- `hermes_cron` is created once by the bootstrap, never cloned.
-- ---------------------------------------------------------------------
-- We DON'T schedule the jobs here. They get scheduled by hermes-cron.sh
-- in a follow-up step that connects to `hermes_cron` (the one DB the
-- workers can safely idle in).
