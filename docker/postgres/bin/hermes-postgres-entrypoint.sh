#!/bin/bash
#
# /usr/local/bin/hermes-postgres-entrypoint.sh
# Hermes wrapper around the upstream pgvector docker-entrypoint.sh.
#
# Why this exists:
#   The upstream entrypoint only runs /docker-entrypoint-initdb.d/ scripts
#   on FIRST `initdb`. For a pre-initialized volume (e.g. live
#   `hermes-postgres-data` brought up from the old postgis image), those
#   scripts never run, and the `hermes_template` / `hermes_cron` DBs
#   never get created.
#
# What this does:
#   1. Symlinks /usr/local/bin/hermes-init.sh into
#      /docker-entrypoint-initdb.d/99-hermes.sh so the upstream runs it
#      on FIRST init (alongside the rest of init.d/).
#   2. Hands off to the upstream entrypoint with `exec`. The upstream
#      FOREGROUNDS postgres (which is what docker expects as PID 1).
#   3. For PRE-INITIALIZED volumes, my entrypoint needs to run the init
#      scripts AFTER the real postgres is up. The upstream flow is:
#         - initdb (only on empty volume)
#         - docker_setup_db (creates POSTGRES_DB)
#         - docker_process_init_files /docker-entrypoint-initdb.d/*  ← only on empty volume
#         - exec postgres
#      So on a pre-initialized volume, the init.d scripts NEVER run.
#      We need a separate path. But `exec` replaces the shell, so we
#      can't add a post-start hook from here.
#
#   The solution: symlink the init scripts to /docker-entrypoint-initdb.d/
#   for the first-init case, AND install a one-shot sidecar that the
#   agent can invoke manually if needed. For automated recovery, the
#   hermes-memory install/bootstrap scripts call /usr/local/bin/hermes-init.sh
#   directly after a `docker run` succeeds. The hermes-memory C# binary's
#   preflight check is the canonical signal that init needs to run; if
#   preflight fails, the install orchestrator runs the init manually.
#
# In short:
#   - Fresh volume: upstream init.d does the work.
#   - Pre-initialized volume: orchestrator (hermes-memory install) runs
#     the init manually after `docker run`. The wrapper doesn't try to
#     detect this — it's the installer's job.

set -e

# Symlink the init scripts into /docker-entrypoint-initdb.d/ so the
# upstream runs them on first init. The 99- prefix ensures they run
# AFTER any other init.d scripts (the upstream's own docker_setup_db
# runs first via docker_temp_server_start).
ln -sf /usr/local/bin/hermes-init.sh /docker-entrypoint-initdb.d/99-hermes-init.sh
ln -sf /usr/local/bin/hermes-cron.sh /docker-entrypoint-initdb.d/99-hermes-cron.sh

# Hand off to the upstream entrypoint. `exec` replaces the shell so
# docker's PID-1 supervision sees the real postgres process.
exec /usr/local/bin/docker-entrypoint.sh "$@"
