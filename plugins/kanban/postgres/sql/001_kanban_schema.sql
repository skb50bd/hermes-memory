-- 001_kanban_schema.sql
-- Canonical hermes_kanban schema (matches migrations/0006_kanban.sql).
-- Idempotent. Run against the profile database.

CREATE SCHEMA IF NOT EXISTS hermes_kanban;

-- ── Tenants ─────────────────────────────────────────────────────────
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

-- ── Tasks ───────────────────────────────────────────────────────────
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

-- ── Runs ────────────────────────────────────────────────────────────
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

-- ── Events ──────────────────────────────────────────────────────────
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

-- ── Links ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hermes_kanban.task_links (
    parent_id text NOT NULL REFERENCES hermes_kanban.tasks(id) ON DELETE CASCADE,
    child_id  text NOT NULL REFERENCES hermes_kanban.tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (parent_id, child_id)
);
CREATE INDEX IF NOT EXISTS idx_kanban_links_child ON hermes_kanban.task_links(child_id);

-- ── Comments ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS hermes_kanban.task_comments (
    id         bigserial PRIMARY KEY,
    task_id    text NOT NULL REFERENCES hermes_kanban.tasks(id) ON DELETE CASCADE,
    author     text NOT NULL,
    body       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_kanban_comments_task ON hermes_kanban.task_comments(task_id, created_at);

-- ── Attachments ─────────────────────────────────────────────────────
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

-- ── Notify subs ─────────────────────────────────────────────────────
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

-- Convenience view
CREATE OR REPLACE VIEW hermes_kanban.v_board_tasks AS
SELECT
    t.*,
    ten.slug AS tenant_slug,
    ten.name AS tenant_name
FROM hermes_kanban.tasks t
JOIN hermes_kanban.tenants ten ON ten.id = t.tenant_id
WHERE ten.archived = false;

-- Seed default tenant
INSERT INTO hermes_kanban.tenants (slug, name)
VALUES ('default', 'Default tenant')
ON CONFLICT (slug) DO NOTHING;
