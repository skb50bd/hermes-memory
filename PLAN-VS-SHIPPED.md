# Plan vs. Shipped: 2026-06-03 implementation

What actually happened when I built the v0.1.0 Docker image. Useful
for future sessions picking this up cold.

## What worked first try

- `pgvector/pgvector:pg18` base image — no surprises
- All 5 extensions installable from PGDG apt (cron, timescaledb, age, pg_trgm, ltree)
- pgvector, pg_trgm, timescaledb, age — install in one shot via `CREATE EXTENSION IF NOT EXISTS`
- `create_hypertable()` for metrics
- HNSW indexes for vector_768/1024/1536 in agent_memory + vector_1024 in hermes_wiki
- FTS indexes on memory content_tsv + wiki body_tsv + journal messages content_tsv
- 2-hop recursive CTE for wiki related docs
- Multi-arch build with buildx (linux/amd64 + linux/arm64 via QEMU)
- `CREATE DATABASE ... TEMPLATE` clone inherits everything byte-perfect

## What I got wrong, and how I fixed it

### 1. PGDG package names

I had the names wrong:
- `postgresql-18-pg-cron` → actually `postgresql-18-cron`
- `timescaledb-2-postgresql-18` → actually `postgresql-18-timescaledb`

Fix: one `apt-get install` per package with a `|| (echo "X failed" && exit 1)` so
the real failing package shows up in the error. The combined `&&` chain
masked the actual failure.

### 2. ltree is an extension in PG 18, not built-in

In PG ≤ 17, ltree shipped as a contrib module. In PG 18, it moved to
`CREATE EXTENSION ltree`. My `category ltree` column failed without
this. Fix: add `CREATE EXTENSION IF NOT EXISTS ltree` to the bootstrap.

### 3. TimescaleDB 2.x is AGPL with paid features

`add_compression_policy` and `add_retention_policy` are paid (require
the "timescale" tier license). The free "apache" tier gives you
hypertables and basic queries. Fix: removed both policies. For
retention, use a plain `DELETE FROM hermes_metrics.events WHERE ts <
...` scheduled by pg_cron (free).

### 4. initdb.d/ scripts run in template1, not in a real DB

`/docker-entrypoint-initdb.d/*.sql` is replayed during the entrypoint's
first-boot init **in the template1 context**. The superuser-only
extensions (timescaledb, age) refuse to install there. Fix:
moved all setup out of initdb.d/ into a one-shot `hermes-init.sh` that
the user runs manually with `docker exec`. Bundled the SQL at
`/usr/local/share/hermes/01-schemas.sql` inside the image.

### 5. The killer issue: pg_cron blocks template clones

`CREATE DATABASE ... TEMPLATE` refuses to run if ANY session is
active in the source DB. pg_cron + TimescaleDB each keep one idle
session pinned to `cron.database_name`. If cron.database_name is
`hermes_template`, every new profile clone is blocked.

Fix: created a separate `hermes_cron` DB. Set `cron.database_name =
'hermes_cron'` in postgresql.conf. Workers idle in hermes_cron
instead. Profile DBs (clones of hermes_template) have no pinned
sessions and clone cleanly. The scheduled jobs still run, but their
DB targets are explicit (per-profile names). For now the actual SQL
is a placeholder; the hermes-memory binary handles per-profile
operations at startup.

### 6. Missing `);` in skill_links CREATE TABLE

When I rewrote the TimescaleDB section I accidentally dropped the
`);` that closed the previous `CREATE TABLE hermes_skills.skill_links`
statement. psql errored with a confusing "LINE 15" reference, blaming
the next `CREATE SCHEMA` for the metrics section. Cost: 3 rebuilds
before I bisected. Fix: added the missing `);` and a
`CREATE INDEX IF NOT EXISTS idx_skill_links_target` line that had
also been dropped.

## What's still TODO

- [ ] Regenerate `migrations/0001-0005_*.sql` from
  `docker/postgres/bin/01-schemas.sql` (single source of truth) so
  the C# MigrationRunner and the docker bootstrap don't drift.
- [ ] Sync `~/.hermes/plans/2026-06-03-hermes-memory-platform/plan.md`
  with these findings (most of the open questions in that plan now
  have answers).
- [ ] The C# binary (hermes-memory CLI) — NativeAOT spike is the
  first blocker, per the plan.
- [ ] The per-profile pg_cron jobs (the hermes_cron DB is set up but
  the actual journal/retention work needs to be dblink or
  binary-driven; placeholder SQL for now).
- [ ] GHCR push (no auth available in this sandbox).

## State of the repo after this session

- 797842b: original scaffold (commit 1)
- next: working Docker image, verified end-to-end (commit 2, uncommitted)
- `hermes-postgres:dev-amd64` — 2.37GB, verified
- `hermes-postgres:dev-arm64` — 2.34GB, verified via QEMU
- `hermes-postgres:dev-multi` — 4.71GB on disk, both archs
- All smoke tests pass: extensions, schemas, indexes, FTS, recursive
  CTE, time_bucket, percentile_cont
