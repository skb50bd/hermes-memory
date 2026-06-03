# Plan: Kanban — SQLite → Postgres Migration

**Date:** 2026-06-03
**Author:** Pixu (assistant) for Shakib Haris
**Status:** Approved — building v0.2.0
**Supersedes:** N/A (new work)

---

## 1. What's in the existing SQLite system

Five per-board databases under `~/.hermes/kanban/boards/`:

| Board | Tasks | Profile |
|---|---|---|
| hermes | 1 | Hermes Agent itself |
| sv | 2 | SportsVerse |
| chokidar | 1 (archived) | chokidar monitor |
| infra | 0 | infra |
| sportsverse | 0 | sportsverse |
| st-platform | 0 | st-platform |

Total: 4 active tasks across 6 boards. ~9,758 LOC of Python in
`hermes_cli/kanban_*.py` + `plugins/kanban/`. Schema has 8 tables:

- `tasks` — the work itself, with claim/heartbeat/release fields
- `task_runs` — execution history per attempt
- `task_events` — fine-grained event log
- `task_links` — parent/child task relationships
- `task_comments` — discussion threads
- `task_attachments` — file attachments
- `kanban_notify_subs` — platform subscriptions for status notifications
- (no `tenants` table; `tenant` is a free text column on `tasks`)

Plus a per-board `board.json` (slug, name, description, icon, workdir)
and per-task `workspaces/<task_id>/` (the actual worktree).

## 2. Locked decisions

| Q | Decision |
|---|---|
| Where does the kanban live? | New `hermes_kanban` schema in `hermes_template` (cloned per profile) |
| How do we migrate the SQLite boards? | One-shot `hermes-memory migrate-kanban` command, then archive SQLite |
| What does the agent interface look like? | New MCP tools (`kanban_create`, `kanban_list`, `kanban_claim`, `kanban_complete`, `kanban_comment`, `kanban_history`, `kanban_subscribe`, etc.). Python plugin becomes a back-compat shim. |
| Concurrency model? | `SELECT ... FOR UPDATE SKIP LOCKED` in a CTE for atomic claim |
| Tenants? | First-class `tenants` table. `tasks.tenant` is a real FK. Boards become views. |

## 3. New schema (`hermes_kanban`)

### 3.1 Tenants (replaces free-form `tenant` text)

```sql
CREATE TABLE hermes_kanban.tenants (
    id          bigserial PRIMARY KEY,
    slug        text UNIQUE NOT NULL,           -- e.g. 'sv', 'hermes', 'infra'
    name        text NOT NULL,                  -- 'SportsVerse'
    description text,
    icon        text,                           -- emoji or url
    color       text,
    default_workdir text,
    created_at  timestamptz DEFAULT now(),
    archived    boolean NOT NULL DEFAULT false
);
```

### 3.2 Tasks (the work)

```sql
CREATE TABLE hermes_kanban.tasks (
    id                   text PRIMARY KEY,            -- 't_<12hex>' like today
    tenant_id            bigint REFERENCES hermes_kanban.tenants(id) ON DELETE RESTRICT,
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
    current_run_id       bigint,                      -- FK to task_runs (denormalised)
    workflow_template_id text,
    current_step_key     text,
    skills               jsonb NOT NULL DEFAULT '[]'::jsonb,
    model_override       text,
    max_retries          integer,
    session_id           text,
    goal_mode            integer NOT NULL DEFAULT 0,
    goal_max_turns       integer
);
```

### 3.3 Task runs (dispatch attempts)

```sql
CREATE TABLE hermes_kanban.task_runs (
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
```

### 3.4 The killer pattern: claim with SKIP LOCKED

```sql
-- Dispatcher: pick the next ready task and atomically claim it.
WITH next AS (
    SELECT id FROM hermes_kanban.tasks
    WHERE status = 'ready'
      AND (assignee IS NULL OR assignee = $1)
    ORDER BY priority DESC, created_at
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE hermes_kanban.tasks t
SET status = 'running',
    started_at = now(),
    worker_pid = $2
FROM next
WHERE t.id = next.id
RETURNING t.*;
```

If the CTE returns 0 rows, another worker grabbed it. The dispatcher
moves on. No `claim_lock`/`claim_expires` math, no race conditions.

### 3.5 Other tables

```sql
-- Events (fine-grained history, one row per state change)
CREATE TABLE hermes_kanban.task_events (
    id         bigserial PRIMARY KEY,
    task_id    text NOT NULL REFERENCES hermes_kanban.tasks(id) ON DELETE CASCADE,
    run_id     bigint REFERENCES hermes_kanban.task_runs(id) ON DELETE SET NULL,
    kind       text NOT NULL,
    payload    jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Parent/child links
CREATE TABLE hermes_kanban.task_links (
    parent_id text NOT NULL REFERENCES hermes_kanban.tasks(id) ON DELETE CASCADE,
    child_id  text NOT NULL REFERENCES hermes_kanban.tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (parent_id, child_id)
);

-- Comments
CREATE TABLE hermes_kanban.task_comments (
    id         bigserial PRIMARY KEY,
    task_id    text NOT NULL REFERENCES hermes_kanban.tasks(id) ON DELETE CASCADE,
    author     text NOT NULL,
    body       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Attachments (file pointers, actual blobs stay on disk)
CREATE TABLE hermes_kanban.task_attachments (
    id           bigserial PRIMARY KEY,
    task_id      text NOT NULL REFERENCES hermes_kanban.tasks(id) ON DELETE CASCADE,
    filename     text NOT NULL,
    stored_path  text NOT NULL,
    content_type text,
    size         bigint NOT NULL DEFAULT 0,
    uploaded_by  text,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- Notify subscriptions (which Discord channels want updates for which tasks)
CREATE TABLE hermes_kanban.notify_subs (
    task_id           text NOT NULL REFERENCES hermes_kanban.tasks(id) ON DELETE CASCADE,
    platform          text NOT NULL,
    chat_id           text NOT NULL,
    thread_id         text NOT NULL DEFAULT '',
    user_id           text,
    notifier_profile  text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    last_event_id     bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, platform, chat_id, thread_id)
);
```

### 3.6 Indexes

```sql
CREATE INDEX idx_tasks_tenant_status   ON hermes_kanban.tasks(tenant_id, status);
CREATE INDEX idx_tasks_status          ON hermes_kanban.tasks(status);
CREATE INDEX idx_tasks_assignee        ON hermes_kanban.tasks(assignee, status);
CREATE INDEX idx_tasks_idempotency     ON hermes_kanban.tasks(idempotency_key);
CREATE INDEX idx_tasks_session_id      ON hermes_kanban.tasks(session_id);
CREATE INDEX idx_tasks_priority        ON hermes_kanban.tasks(priority DESC, created_at);

CREATE INDEX idx_runs_task    ON hermes_kanban.task_runs(task_id, started_at DESC);
CREATE INDEX idx_runs_status  ON hermes_kanban.task_runs(status);

CREATE INDEX idx_events_task  ON hermes_kanban.task_events(task_id, created_at);
CREATE INDEX idx_events_run   ON hermes_kanban.task_events(run_id, id);

CREATE INDEX idx_links_child  ON hermes_kanban.task_links(child_id);

CREATE INDEX idx_comments_task ON hermes_kanban.task_comments(task_id, created_at);
CREATE INDEX idx_attachments_task ON hermes_kanban.task_attachments(task_id, created_at);
```

### 3.7 Convenience views

```sql
-- A "board" is a view over a tenant's tasks.
CREATE OR REPLACE VIEW hermes_kanban.v_board_tasks AS
SELECT t.*, ten.slug AS tenant_slug, ten.name AS tenant_name
FROM hermes_kanban.tasks t
JOIN hermes_kanban.tenants ten ON ten.id = t.tenant_id
WHERE ten.archived = false;
```

## 4. MCP tool surface

| Tool | Args | Returns |
|---|---|---|
| `kanban_create` | title, body, tenant_slug, assignee?, priority?, skills? | task id |
| `kanban_list` | tenant_slug?, status?, assignee?, limit? | task rows |
| `kanban_get` | task_id | full task + last 20 events |
| `kanban_update` | task_id, body? | updated row |
| `kanban_claim` | assignee, max_runtime_seconds? | claimed task or `{"claimed":false}` |
| `kanban_heartbeat` | task_id, worker_pid | ack |
| `kanban_complete` | task_id, summary, result? | updated row |
| `kanban_fail` | task_id, error, status? | updated row (increments consecutive_failures) |
| `kanban_link` | parent_id, child_id | ok |
| `kanban_unlink` | parent_id, child_id | ok |
| `kanban_comment` | task_id, body, author | comment id |
| `kanban_history` | task_id, limit? | events + runs |
| `kanban_subscribe` | task_id, platform, chat_id, thread_id? | ok |
| `kanban_unsubscribe` | task_id, platform, chat_id, thread_id? | ok |
| `kanban_tenants` | include_archived? | tenant list |
| `kanban_tenant_create` | slug, name, description?, icon? | tenant id |
| `kanban_search` | query, tenant_slug? | FTS results |

That's 17 tools. Lots, but each is small.

## 5. Migration command

`hermes-memory migrate-kanban --conn <dsn> [--sqlite-dir <path>]`:

1. Discovers every `*.db` under `~/.hermes/kanban/boards/` (override with `--sqlite-dir`).
2. For each board:
   a. Reads `board.json` → upserts into `hermes_kanban.tenants` (slug = board slug).
   b. Reads all tasks → upserts into `hermes_kanban.tasks` (using `id` as natural key).
   c. Reads all task_runs → inserts into `hermes_kanban.task_runs` (with id offset to avoid collisions if you migrate twice).
   d. Reads task_events, task_links, task_comments, task_attachments, notify_subs.
3. Prints a summary: per-board row counts.
4. Does **not** delete SQLite files. User runs `mv ~/.hermes/kanban ~/.hermes/kanban.sqlite-archive-$(date +%s)` manually.

Idempotent: re-running the command on the same DB skips already-imported rows (by `id`).

## 6. Backward compat shim

The existing Python plugin in `hermes_cli/kanban_*.py` becomes a thin shim
that calls the new MCP tools. The plugin's CLI subcommand surface
(`hermes kanban list`, `hermes kanban create`, etc.) stays identical so
existing agent instructions and scripts keep working.

**Big TODO for a future PR**: rewrite the dispatcher to use
`SELECT ... FOR UPDATE SKIP LOCKED`. The existing claim-lock dance
works in Postgres too, but the new shape is cleaner and race-free.

## 7. Top 5 risks

1. **Dispatcher migration** — the current Python dispatcher has 7624 LOC in `kanban_db.py`. The new claim model (`SKIP LOCKED` + atomic state transition) means rewriting the dispatch loop. Out of scope for v0.2.0; the existing claim_lock model still works in Postgres.
2. **Workspace directories** — each task has a `workspaces/<task_id>/` git worktree. Migration moves the metadata, not the on-disk worktree. The migration command must not touch `~/.hermes/kanban/boards/<slug>/workspaces/`.
3. **Id collisions across boards** — `tasks.id` is `t_<12hex>`. Two boards could in theory collide (vanishingly unlikely with 12 hex). The migration will detect collisions and refuse to import the second one.
4. **FTS for task search** — SQLite has FTS5; Postgres has `tsvector`. Migration command will rebuild FTS indexes on the imported data.
5. **Attachment blob paths** — the existing `task_attachments.stored_path` is filesystem-local. Migration records the paths but doesn't move the files. User is responsible for keeping the storage reachable.

## 8. Definition of done

- [ ] `hermes_kanban` schema in `docker/postgres/bin/01-schemas.sql` and `migrations/0006_kanban.sql`
- [ ] `KanbanRepository` in C# Core with all 8 tables + SKIP LOCKED claim
- [ ] 17 MCP tools registered in `KanbanTools`
- [ ] `migrate-kanban` subcommand imports all 5 SQLite boards
- [ ] amd64 build + smoke test (insert, claim, complete, comment, history)
- [ ] arm64 build + smoke test
- [ ] Docs + memory updated
- [ ] `migrate-kanban` actually imports the real 4 active tasks from `hermes`, `sv`, `chokidar` (proves end-to-end)
