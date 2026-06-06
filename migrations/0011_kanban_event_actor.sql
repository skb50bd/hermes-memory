-- 0011_kanban_event_actor.sql
-- Drift fix: prod `hermes_kanban.task_events` was created before the
-- `actor` column was added to the migration. The C# and Python repos
-- both need it, so this is an idempotent fix-up.

ALTER TABLE hermes_kanban.task_events
    ADD COLUMN IF NOT EXISTS actor text;
