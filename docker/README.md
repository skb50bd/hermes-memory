# Hermes Postgres Image

Custom PostgreSQL image for the Hermes Agent platform. Extends
`pgvector/pgvector:pg18` with the extensions the agent platform needs:

| Extension | Why |
|---|---|
| `vector` (pgvector) | Embeddings for memory, wiki, and skill search |
| `pg_trgm` | Trigram similarity for fuzzy matching |
| `postgis` | Geospatial queries (skill metadata, location-aware agents) |
| `pg_cron` | Scheduled jobs (partition rollover, retention) |
| `timescaledb` | Operational metrics hypertables |
| `age` | Apache AGE graph database (Cypher, opt-in) |

## Build

```bash
docker build -t hermes-postgres:18 -f docker/postgres/Dockerfile docker/postgres/
```

The build installs 4 extensions from apt and falls back to building
Apache AGE from source if no apt package is available for the target
Postgres major version.

## Runtime

Set `POSTGRES_DB=hermes_template` so the template-bootstrap script
fires and installs the 5 schemas. After that:

```bash
docker run -d --name hermes-pg \
    -e POSTGRES_PASSWORD=changeme \
    -e POSTGRES_DB=hermes_template \
    -p 5432:5432 \
    -v pgdata:/var/lib/postgresql/data \
    hermes-postgres:18

# Create a per-agent database
docker exec hermes-pg psql -U postgres -c "CREATE DATABASE hermes_work TEMPLATE hermes_template;"
```

Or use the `hermes-memory` binary to manage profiles:

```bash
hermes-memory profile create work
hermes-memory profile create personal
hermes-memory profile list
```

## Configuration

Tunables live in `postgresql.conf`. Connection limits per profile are
NOT set in the global config — they're applied per-database via
`ALTER DATABASE hermes_<profile> CONNECTION LIMIT 20` at creation time.

## Versions

- PostgreSQL 18
- pgvector (latest, shipped with base image)
- PostGIS 3.5
- pg_cron (latest from PGDG)
- TimescaleDB 2.18+
- Apache AGE 1.5.0+ (apt or source-built)
