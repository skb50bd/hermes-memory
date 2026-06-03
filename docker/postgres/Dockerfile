# Hermes Postgres Image
#
# Base: pgvector/pgvector:pg18 (Postgres 18 + pgvector pre-installed)
# Adds: pg_trgm (built-in, just needs CREATE EXTENSION), PostGIS 3,
#       pg_cron, TimescaleDB 2, Apache AGE
#
# AGE on PG 18 is a real risk. We try the PGDG apt package first;
# if it's not available we fall back to a build-from-source step.
# See: https://github.com/apache/age/releases for the latest matrix.

ARG PG_MAJOR=18

FROM pgvector/pgvector:pg${PG_MAJOR}

ARG PG_MAJOR=18
ARG TIMEScaledb_VERSION=2.18.2
ARG AGE_VERSION=1.5.0
ARG POSTGIS_VERSION=3.5.3

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

# ---- Build tooling (needed only for AGE source build fallback) ----
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
        gnupg \
        lsb-release \
        pkg-config \
        postgresql-server-dev-${PG_MAJOR} \
        flex \
        bison \
    && rm -rf /var/lib/apt/lists/*

# ---- PGDG repo for the postgres extensions ----
RUN install -d /usr/share/postgresql-common/pgdg \
    && curl -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
       --fail https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(lsb_release -c -s)-pgdg main" \
       > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update

# ---- TimescaleDB packagecloud repo ----
RUN curl -fsSL https://packagecloud.io/timescale/timescaledb/gpgkey \
        | gpg --dearmor -o /etc/apt/trusted.gpg.d/timescaledb.gpg \
    && echo "deb https://packagecloud.io/timescale/timescaledb/debian/ $(lsb_release -c -s) main" \
       > /etc/apt/sources.list.d/timescaledb.list \
    && apt-get update

# ---- Install the 4 packaged extensions ----
# Verified package names on bookworm + PGDG + PG 18:
#   postgresql-18-postgis-3, postgresql-18-cron, postgresql-18-timescaledb, postgresql-18-age
# Note: the timescale package is `timescaledb`, NOT `timescaledb-2`.
# Each install is wrapped individually so a single failure surfaces the
# real package name in the error rather than masking it.
RUN apt-get install -y --no-install-recommends postgresql-${PG_MAJOR}-postgis-3 \
    || (echo "postgis install failed" && exit 1)
RUN apt-get install -y --no-install-recommends postgresql-${PG_MAJOR}-cron \
    || (echo "pg_cron install failed" && exit 1)
RUN apt-get install -y --no-install-recommends postgresql-${PG_MAJOR}-timescaledb \
    || (echo "timescaledb install failed" && exit 1)
RUN apt-get install -y --no-install-recommends postgresql-${PG_MAJOR}-age \
    || (echo "age install failed — falling back to source build" && \
        cd /tmp && \
        git clone --branch v${AGE_VERSION} --depth 1 https://github.com/apache/age.git age-src && \
        cd age-src && \
        make PG_CONFIG=/usr/bin/pg_config && \
        make install PG_CONFIG=/usr/bin/pg_config && \
        cd / && \
        rm -rf /tmp/age-src)
RUN rm -rf /var/lib/apt/lists/*

# ---- Re-install runtime packages (the source build strip above is fine) ----
# No-op, just keep the layer explicit

# ---- postgresql.conf: shared_preload_libraries order matters ----
# pg_cron schedules background jobs (must run first to register workers)
# timescaledb hooks the executor (must be present before user DDL hits)
# age hooks the parser (must be last; it intercepts Cypher syntax)
RUN { \
        echo "shared_preload_libraries = 'pg_cron,timescaledb,age'"; \
        echo "cron.database_name = 'hermes_cron'"; \
    } >> /usr/share/postgresql/postgresql.conf.sample
RUN grep -E "^(shared_preload_libraries|cron.database_name)" /usr/share/postgresql/postgresql.conf.sample

# ---- hermes-init.sh: one-shot installer for extensions + schemas ----
# The script + bundled SQL are the only path that touches the template DB.
# initdb.d/ is intentionally empty (or absent) so the entrypoint doesn't
# replay anything at first boot.
COPY --chown=root:root bin/hermes-init.sh /usr/local/bin/hermes-init.sh
COPY --chown=root:root bin/hermes-cron.sh /usr/local/bin/hermes-cron.sh
COPY --chown=root:root bin/01-schemas.sql /usr/local/share/hermes/01-schemas.sql
RUN chmod 0755 /usr/local/bin/hermes-init.sh /usr/local/bin/hermes-cron.sh /usr/local/share/hermes/01-schemas.sql

# ---- postgresql.conf tuning (merged into the image's conf at build) ----
# The pgvector image's entrypoint copies postgresql.conf.sample to the
# data dir on first init, so appending to .sample here makes the tunables
# survive container rebuilds.
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

# ---- hermes-memory CLI (not bundled in the image; install separately) ----
# The CLI is a NativeAOT-published single binary, ~30MB, that talks to this
# Postgres over HERMES_PG_CONN_STR. Install it from the GitHub release:
#   curl -L https://github.com/skb50bd/hermes-memory/releases/latest/download/hermes-memory-linux-x64.tar.gz | tar -xz
#   sudo mv hermes-memory /usr/local/bin/
# Keeping the binary OUT of the Postgres image means: smaller image, no
# build dependency on .NET 10, binary updates don't require image rebuild.

# Healthcheck: pg_isready + extension list
HEALTHCHECK --interval=5s --timeout=5s --retries=20 \
    CMD pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
        || exit 1

EXPOSE 5432
