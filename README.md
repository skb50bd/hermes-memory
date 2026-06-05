# hermes-memory

Postgres-backed memory, wiki, journal, skills catalog, **kanban**,
and operational metrics for the Hermes Agent platform. Single-binary,
stdio MCP, NativeAOT.

> 🚧 **v0.3.0 — kanban plugin ships.** The old
> `~/.hermes/kanban/boards/*/kanban.db` SQLite files are now superseded
> by a `hermes_kanban` schema in the same Postgres. Race-free dispatch
> via `SELECT ... FOR UPDATE SKIP LOCKED`. **The new Python plugin is
> the kanban provider; the old SQLite plugin is replaced by a thin
> shim at `hermes_cli/kanban_db.py`.**

## What you get

| Surface | Storage | MCP tools |
|---|---|---|
| **Memory** | `agent_memory.memories` (per-dim vector + FTS) | `memory_remember`, `memory_search`, `memory_forget`, `memory_status` |
| **Wiki** | `hermes_wiki.documents` + `document_links` (recursive CTE for related/backlinks) | `wiki_create`, `wiki_read`, `wiki_link`, `wiki_backlinks`, `wiki_related`, `wiki_search` |
| **Journal** | `hermes_journal.messages` (regular partitioned table) | `journal_log_session`, `journal_log_message`, `journal_search` |
| **Skills** | `hermes_skills.skills` + `skill_links` | `skill_index_search`, `skill_register`, `skill_link`, `skill_graph` |
| **Metrics** | `hermes_metrics.events` (timescaledb hypertable) | `metrics_record`, `metrics_query` |
| **Kanban** | `hermes_kanban.tasks` + 9 related tables (tenants, task_runs, task_events, task_links, task_comments, task_attachments, tags, task_tags, notify_subs) | 17 tools: `kanban_create`, `kanban_list`, `kanban_get`, `kanban_claim`, `kanban_heartbeat`, `kanban_complete`, `kanban_fail`, `kanban_comment`, `kanban_history`, `kanban_link`, `kanban_children`, `kanban_parents`, `kanban_tenants`, `kanban_tenant_create`, `kanban_subscribe`, `kanban_unsubscribe`, `kanban_search` |

The Python plugin at `~/repos/hermes-memory/plugins/kanban/postgres/`
ships the runtime now. The C# binary is the long-term path but doesn't
ship yet. The thin shim at `hermes_cli/kanban_db.py` makes the swap
invisible to the dispatcher and dashboard.

## Architecture

```
┌─────────────────────┐  HERMES_PG_CONN_STR  ┌──────────────────────────┐
│  hermes-memory      │ ────────────────────▶│  Postgres 18 + 5 exts    │
│  (NativeAOT binary) │                      │  ┌──────────────────┐    │
│                     │                      │  │ hermes_template  │    │
│  --mcp   (stdio)    │                      │  └────────┬─────────┘    │
│  preflight          │                      │           │ clone        │
│  migrate            │                      │  ┌────────▼─────────┐    │
│  profile create     │                      │  │ hermes_work      │    │
│  embed              │                      │  │ hermes_personal  │    │
│  version            │                      │  │ hermes_default   │    │
└─────────────────────┘                      │  └──────────────────┘    │
                                             └──────────────────────────┘
```

**One server, one database per agent profile.** Each agent's `.env`
has `POSTGRES_DATABASE=hermes_<profile>`. The schemas are uniform
because every profile DB is a byte-perfect clone of `hermes_template`.

**5 schemas per database, not 5 databases.** Memory, wiki, journal,
skills, and metrics all share a single Postgres cluster. The journal
is a regular partitioned table; metrics is a timescaledb hypertable.
They share the connection pool and the embedder cache.

## Quickstart

```bash
# 1. Pull the image (or build locally — see docker/README.md)
docker pull ghcr.io/skb50bd/hermes-postgres:18

# 2. Start Postgres (clean cluster, no automatic init)
docker run -d --name hermes-pg \
    -e POSTGRES_PASSWORD=*** \
    -e POSTGRES_USER=postgres \
    -p 5432:5432 \
    -v pgdata:/var/lib/postgresql/data \
    ghcr.io/skb50bd/hermes-postgres:18

# 3. Initialize: creates hermes_template (5 schemas, 5 extensions) +
#    hermes_cron (pg_cron with 2 jobs).
docker exec hermes-pg /usr/local/bin/hermes-init.sh

# 4. Create a per-agent database (byte-perfect clone of hermes_template)
docker exec hermes-pg psql -U postgres -c "CREATE DATABASE hermes_work TEMPLATE hermes_template CONNECTION LIMIT 20"

# 5. Connect and use
export HERMES_PG_CONN_STR='postgresql://postgres:***@localhost:5432/hermes_work'
psql -c "SELECT count(*) FROM pg_extension;"      # 6 (5 + plpgsql)
psql -c "SELECT schemaname, count(*) FROM pg_tables
         WHERE schemaname LIKE 'hermes_%' OR schemaname='agent_memory'
         GROUP BY schemaname;"
```

## Architecture

```
┌─────────────────────┐  HERMES_PG_CONN_STR  ┌──────────────────────────┐
│  hermes-memory      │ ────────────────────▶│  Postgres 18 + 5 exts    │
│  (NativeAOT binary) │                      │  ┌──────────────────┐    │
│                     │                      │  │ hermes_template  │    │
│  --mcp   (stdio)    │                      │  │ (5 schemas,      │    │
│  preflight          │                      │  │  5 extensions)   │    │
│  migrate            │                      │  └────────┬─────────┘    │
│  profile create     │                      │           │ clone        │
│  embed              │                      │  ┌────────▼─────────┐    │
│  version            │                      │  │ hermes_work      │    │
└─────────────────────┘                      │  │ hermes_personal  │    │
                                             │  │ hermes_default   │    │
                                             │  └──────────────────┘    │
                                             │  ┌──────────────────┐    │
                                             │  │ hermes_cron      │    │
                                             │  │ (pg_cron + jobs) │    │
                                             │  └──────────────────┘    │
                                             └──────────────────────────┘
```
```

## Embedders

The default embedder is **Kimi BGE-M3** (free, 1024-dim, MTEB
top-tier). Set `KIMI_API_KEY` in the env. Alternatives:

| Dim | Provider | Model | API key env |
|---|---|---|---|
| 768 | `ollama_local` | `nomic-embed-text` | (none) |
| 1024 | `kimi` (default) | `bge_m3_embed` | `KIMI_API_KEY` |
| 1536 | `kimi` | `bge_m3_embed` | `KIMI_API_KEY` |

Per-dim overrides via `HERMES_EMBED_PROVIDER_<dim>`,
`HERMES_EMBED_MODEL_<dim>`, `HERMES_EMBED_API_KEY_<dim>`,
`HERMES_EMBED_CACHE_DIR_<dim>`. The cache root defaults to
`~/.cache/hermes/embeddings/<dim>/`.

**Fail-open policy**: provider errors substitute a zero vector and
return success. Zero vectors from the fail-open path are **never
cached** (poison prevention). The `noop` provider deliberately
returns zeros and DOES cache.

## Roadmap to v1.0

- [x] Repository scaffold (this commit)
- [x] 5 schemas baked into `hermes_template`
- [x] C# NativeAOT binary with stdio MCP server
- [x] Embedder cache + per-dim registry
- [x] Back-compat shim for the old `pg_*` Python plugin
- [ ] Npgsql + AOT type-registration spike (week 1)
- [ ] All 20 MCP tools exercised by integration tests
- [ ] CI green: docker + unit + integration
- [ ] First tagged release (v0.2.0)
- [ ] Real Cypher queries via Apache AGE (v0.3.0+)

## License

MIT — see `LICENSE`.
