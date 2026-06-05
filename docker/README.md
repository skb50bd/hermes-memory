# Hermes Postgres Image

Custom PostgreSQL image for the Hermes Agent platform. Extends
`pgvector/pgvector:pg18` with the extensions the agent platform needs:

| Extension | Lives in | Why |
|---|---|---|
| `vector` (pgvector) | `hermes_template` + every profile | Embeddings for memory, wiki, skill search |
| `pg_trgm` | same | Trigram similarity for fuzzy matching |
| `ltree` | same | Hierarchical categories (`projects.sportsverse`) |
| `timescaledb` | same | Operational metrics hypertables |
| `age` | same | Apache AGE graph database (Cypher, opt-in) |
| `pg_cron` | **`hermes_cron` only** | Scheduled jobs (lives in its own DB so it doesn't block profile clones) |

## Why pg_cron is separate

`CREATE DATABASE ... TEMPLATE hermes_template` refuses to run if any
session is active in `hermes_template`. pg_cron + TimescaleDB each
hold one idle session pinned to `cron.database_name`. If we put
pg_cron in `hermes_template`, every new profile clone needs to
terminate those backends first. So we don't: pg_cron lives in
`hermes_cron` (a DB that nothing clones), and the workers idle there
safely.

The 5 user schemas (`agent_memory`, `hermes_wiki`, `hermes_journal`,
`hermes_skills`, `hermes_metrics`) all live in `hermes_template` and
every profile DB.

## Build

```bash
# Single-arch
docker buildx build --platform linux/amd64 \
    -t hermes-postgres:dev -f docker/postgres/Dockerfile docker/postgres/

# Multi-arch (the GHCR push target)
docker buildx build --platform linux/amd64,linux/arm64 \
    -t hermes-postgres:dev -f docker/postgres/Dockerfile docker/postgres/
```

All extensions are installed from the PGDG/Timescale apt repos. The
build pulls from `pgvector/pgvector:pg18` (PG 18 + pgvector
pre-installed) and layers the others on top.

## Runtime

The container starts with a clean cluster. The first-boot setup is
a one-shot step that the user runs:

```bash
docker run -d --name hermes-pg \
    -e POSTGRES_PASSWORD=*** \
    -e POSTGRES_USER=postgres \
    -p 5432:5432 \
    -v pgdata:/var/lib/postgresql/data \
    hermes-postgres:dev

# One-shot installer: creates hermes_template (5 schemas, 5 extensions)
# and hermes_cron (pg_cron + 2 scheduled jobs).
docker exec hermes-pg /usr/local/bin/hermes-init.sh

# Create a per-agent database (byte-perfect clone of hermes_template)
docker exec hermes-pg psql -U postgres -c "CREATE DATABASE hermes_work TEMPLATE hermes_template CONNECTION LIMIT 20"
```

Or, when the `hermes-memory` binary is built (not in v0.1.0 yet):

```bash
hermes-memory profile create work
hermes-memory profile create personal
hermes-memory profile list
```

## Why a separate init step

The official Postgres Docker image's `/docker-entrypoint-initdb.d/`
replays scripts in the **template1** context, where most extensions
(`timescaledb`, `age`) refuse to install because they
require superuser CREATE privileges only available in a real DB. So
the schema SQL is **not** in `initdb.d/` — it's bundled at
`/usr/local/share/hermes/01-schemas.sql` and applied by
`hermes-init.sh` after the cluster is up.

## File layout

```
docker/postgres/
├── Dockerfile
└── bin/
    ├── hermes-init.sh     # one-shot: creates hermes_template + hermes_cron
    ├── hermes-cron.sh     # one-shot: schedules the 2 pg_cron jobs
    └── 01-schemas.sql     # the 5 schemas (loaded by hermes-init.sh)
```

## Verified

- amd64 + arm64 builds (QEMU-emulated on amd64 host)
- 5 extensions installable via `hermes-init.sh`
- 5 schemas + all indexes (4 HNSW, multiple GIN, ltree GIST, FTS)
- `CREATE DATABASE ... TEMPLATE hermes_template` succeeds
- 2-hop recursive CTE over wiki document_links
- `time_bucket()` + `percentile_cont()` work on the metrics hypertable
- All 5 user schemas insert/FTS/aggregate successfully
