# Hermes Postgres Image — multi-stage build for size efficiency.
#
# Stage 1 (builder): install curl + gnupg to fetch PGDG and TimescaleDB
#                    apt keys. That's it — no compilation, no source
#                    builds, no build-essential.
#
# Stage 2 (runtime): clean pgvector base. Install just the three packaged
#                    extension .debs. This is much smaller than the
#                    previous single-stage image because the 1.1GB
#                    build-essential + postgresql-server-dev-18 + flex
#                    + bison + git + pkg-config stack is gone — it was
#                    only needed for an Apache AGE source-build fallback
#                    that has been obsolete since PGDG began shipping
#                    prebuilt postgresql-18-age packages (>= 1.5.0).
#
# Result: ~60% smaller image (2.37GB → ~930MB) and growingly slimmer as
# we drop extensions that aren't actually used.

# ---- Build args ----
ARG PG_MAJOR=18
ARG TIMESCALEDB_VERSION=2.18.2

# =============================================================================
# Stage 1: builder — fetch PGDG and TimescaleDB apt keys + sources
# =============================================================================
FROM pgvector/pgvector:pg${PG_MAJOR} AS builder
ARG PG_MAJOR
ENV DEBIAN_FRONTEND=noninteractive

# Install just curl + gnupg + ca-certs to fetch the apt keyrings.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg \
    && rm -rf /var/lib/apt/lists/*

# PGDG keyring + sources
RUN install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
       https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
       > /etc/apt/sources.list.d/pgdg.list

# TimescaleDB keyring + sources
RUN curl -fsSL https://packagecloud.io/timescale/timescaledb/gpgkey \
        | gpg --dearmor -o /etc/apt/trusted.gpg.d/timescaledb.gpg \
    && echo "deb https://packagecloud.io/timescale/timescaledb/debian/ bookworm main" \
       > /etc/apt/sources.list.d/timescaledb.list

# =============================================================================
# Stage 2: runtime — clean pgvector base + extensions installed via apt
# =============================================================================
FROM pgvector/pgvector:pg${PG_MAJOR} AS runtime
ARG PG_MAJOR

ENV DEBIAN_FRONTEND=noninteractive

# Bring forward the apt sources and keyrings from the builder
COPY --from=builder /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
                    /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
COPY --from=builder /etc/apt/sources.list.d/pgdg.list          /etc/apt/sources.list.d/
COPY --from=builder /etc/apt/trusted.gpg.d/timescaledb.gpg     /etc/apt/trusted.gpg.d/
COPY --from=builder /etc/apt/sources.list.d/timescaledb.list   /etc/apt/sources.list.d/

# Two-step: install ca-certificates first (so apt can verify HTTPS repos
# during the next `apt-get update`), then install the extensions. The
# three extension .debs each pull their declared runtime dependencies
# via apt. We use --no-install-recommends to skip doc packages.
#   - postgresql-18-cron:      pg_cron background job scheduler
#   - postgresql-18-timescaledb: TimescaleDB hypertables
#   - postgresql-18-age:       Apache AGE graph extension
#
# pgvector:vector is already in the base image. pg_trgm, ltree, plpgsql
# are stock postgres extensions and need no extra package.
#
# Cleanup: drop ca-certificates after the install (it was only needed
# for the apt-get update step) and purge the apt cache. The base image
# still has OpenSSL libraries; we just lose the Mozilla CA bundle which
# is only used by apt and curl.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        postgresql-${PG_MAJOR}-cron \
        postgresql-${PG_MAJOR}-timescaledb \
        postgresql-${PG_MAJOR}-age \
    && apt-get purge -y ca-certificates \
    && apt-get autoremove -y \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Hermes scripts + schema files
COPY --chown=root:root docker/postgres/bin/hermes-init.sh /usr/local/bin/hermes-init.sh
COPY --chown=root:root docker/postgres/bin/hermes-cron.sh /usr/local/bin/hermes-cron.sh
COPY --chown=root:root docker/postgres/bin/01-schemas.sql  /usr/local/share/hermes/01-schemas.sql
COPY --chown=root:root migrations                         /usr/migrations
RUN chmod 0755 /usr/local/bin/hermes-init.sh /usr/local/bin/hermes-cron.sh \
    && chmod 0644 /usr/local/share/hermes/01-schemas.sql

# postgresql.conf tunables (order matters: shared_preload_libraries must list
# pg_cron, timescaledb, age in that order — pg_cron registers workers first,
# timescaledb hooks the executor, age hooks the parser last).
RUN { \
        echo "shared_preload_libraries = 'pg_cron,timescaledb,age'"; \
        echo "cron.database_name = 'hermes_cron'"; \
    } >> /usr/share/postgresql/postgresql.conf.sample

# Hermes-specific tunables
RUN { \
        echo ""; \
        echo "# ---- Hermes tunables ----"; \
        echo "max_connections = 200"; \
        echo "shared_buffers = 512MB"; \
        echo "effective_cache_size = 1GB"; \
        echo "work_mem = 8MB"; \
        echo "maintenance_work_mem = 128MB"; \
        echo "wal_compression = on"; \
        echo "logging_collector = on"; \
        echo "log_destination = 'stderr'"; \
        echo "log_min_duration_statement = 250ms"; \
        echo "log_line_prefix = '%m [%p] %q%u@%d from %h '"; \
        echo "lc_messages = 'C.UTF-8'"; \
        echo "lc_monetary = 'C.UTF-8'"; \
        echo "lc_numeric = 'C.UTF-8'"; \
        echo "lc_time = 'C.UTF-8'"; \
        echo "timescaledb.telemetry_level = off"; \
    } >> /usr/share/postgresql/postgresql.conf.sample

# Healthcheck
HEALTHCHECK --interval=5s --timeout=5s --retries=20 \
    CMD pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
        || exit 1

EXPOSE 5432
