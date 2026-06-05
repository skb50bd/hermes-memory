-- 000_schema.sql
--
-- DEPRECATED. The canonical schema lives in `migrations/0001_agent_memory.sql`
-- and is applied by `install/steps/_step_run.py` (bash installer) or by
-- `Hermes.Memory.Core.Db.MigrationRunner` (C# installer). This file is
-- kept ONLY for backwards compatibility with the legacy
-- `psql -f sql/000_schema.sql` bootstrap path — and even then, it
-- delegates to the canonical migration so we don't keep two divergent
-- schemas in the repo.
--
-- If you reached this file via:
--   psql -f plugins/memory/postgres/sql/000_schema.sql
-- ... and got this comment, you should switch to:
--   psql -f migrations/0001_agent_memory.sql
-- Or, better, use the installer:
--   install.sh
--   ./src/Hermes.Memory.Cli/bin/Release/net10.0/hermes-memory install
--
-- History:
--   - Original file lived in `public` schema with `is_active` boolean,
--     `category_id` FK, etc. (legacy "flat" layout)
--   - Migrated to namespaced `agent_memory.*` layout with `deleted_at`,
--     ltree category, etc. in v1.7 (commit that introduced the C# port)
--   - This file was never updated and became stale. It pointed at
--     `public.agent_memory` and `agent_memory_settings` (no namespace),
--     which silently failed against the live DB.
--   - Fix (commit pending): replace contents with a pointer to
--     `migrations/0001_agent_memory.sql`.

\echo '!! 000_schema.sql is DEPRECATED. Use migrations/0001_agent_memory.sql instead.'
\echo '!! Forwarding to migrations/0001_agent_memory.sql ...'
\i ../../../migrations/0001_agent_memory.sql
