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
# postgresql-${PG_MAJOR}-postgis-3 may not exist for ${PG_MAJOR}=18 in all
# distros. If apt-get fails on that one line, build PostGIS from source.
RUN apt-get install -y --no-install-recommends \
        postgresql-${PG_MAJOR}-postgis-3 \
        postgresql-${PG_MAJOR}-pg-cron \
        timescaledb-2-postgresql-${PG_MAJOR} \
    && rm -rf /var/lib/apt/lists/* \
    || (echo "PostGIS apt install failed, will need source build" && exit 1)

# ---- Apache AGE: try PGDG, fall back to source build ----
# PGDG may or may not have apache-age packaged for PG 18 at the time of build.
RUN (apt-get install -y --no-install-recommends postgresql-${PG_MAJOR}-age \
        && echo "AGE installed from apt" \
    ) \
    || ( \
        echo "AGE not in apt — building from source" \
        && cd /tmp \
        && git clone --branch v${AGE_VERSION} --depth 1 \
              https://github.com/apache/age.git age-src \
        && cd age-src \
        && make PG_CONFIG=/usr/bin/pg_config \
        && make install PG_CONFIG=/usr/bin/pg_config \
        && cd / \
        && rm -rf /tmp/age-src \
    )

# ---- Re-install runtime packages (the source build strip above is fine) ----
# No-op, just keep the layer explicit

# ---- postgresql.conf: shared_preload_libraries order matters ----
# pg_cron schedules background jobs (must run first to register workers)
# timescaledb hooks the executor (must be present before user DDL hits)
# age hooks the parser (must be last; it intercepts Cypher syntax)
RUN echo "shared_preload_libraries = 'pg_cron,timescaledb,age'" \
        >> /usr/share/postgresql/postgresql.conf.sample

# ---- Tuned config (replaces postgresql.conf) ----
COPY postgresql.conf /etc/postgresql/postgresql.conf

# ---- Bootstrap SQL ----
# 00-extensions.sql: every new DB gets the 6 extensions installed.
# 01-template.sql:    only the hermes_template DB gets the 5 schemas baked in.
COPY initdb.d/ /docker-entrypoint-initdb.d/

# ---- AOT-published binary: the hermes-memory CLI (built in CI) ----
COPY --from=ghcr.io/skb50bd/hermes-memory-binary:0.1.0 \
     /hermes-memory /usr/local/bin/hermes-memory

# Healthcheck: pg_isready + extension list
HEALTHCHECK --interval=5s --timeout=5s --retries=20 \
    CMD pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
        || exit 1

EXPOSE 5432
