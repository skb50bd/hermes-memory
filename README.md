# hermes-memory

Postgres-backed memory, wiki, journal, skills catalog, and operational
metrics for the Hermes Agent platform. Single-binary, stdio MCP,
NativeAOT.

> 🚧 **v0.1.0 scaffold.** This is a greenfield repo. The previous
> `hermes-postgres-memory` Python plugin's features are now exposed
> as MCP tools in this binary. See `skills/hermes-memory/SKILL.md`
> for the agent's reference.

## What you get

| Surface | Storage | MCP tools |
|---|---|---|
| **Memory** | `agent_memory.memories` (per-dim vector + FTS) | `memory_remember`, `memory_search`, `memory_forget`, `memory_status` |
| **Wiki** | `hermes_wiki.documents` + `document_links` (recursive CTE for related/backlinks) | `wiki_create`, `wiki_read`, `wiki_link`, `wiki_backlinks`, `wiki_related`, `wiki_search` |
| **Journal** | `hermes_journal.messages` (regular partitioned table) | `journal_log_session`, `journal_log_message`, `journal_search` |
| **Skills** | `hermes_skills.skills` + `skill_links` | `skill_index_search`, `skill_register`, `skill_link`, `skill_graph` |
| **Metrics** | `hermes_metrics.events` (timescaledb hypertable) | `metrics_record`, `metrics_query` |

20 MCP tools over stdio. Single 30MB statically-linked C# binary. No
HTTP, no ports to expose, no service to deploy.

## Architecture

```
┌─────────────────────┐  HERMES_PG_CONN_STR  ┌──────────────────────────┐
│  hermes-memory      │ ────────────────────▶│  Postgres 18 + 6 exts    │
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
# 1. Pull the image (or build locally)
docker pull ghcr.io/skb50bd/hermes-postgres:18

# 2. Start Postgres (creates hermes_template with all 5 schemas)
docker run -d --name hermes-pg \
    -e POSTGRES_PASSWORD=*** \
    -e POSTGRES_DB=hermes_template \
    -p 5432:5432 \
    ghcr.io/skb50bd/hermes-postgres:18

# 3. Create a per-agent database
hermes-memory profile create work
hermes-memory profile create personal
hermes-memory profile list

# 4. Preflight
export HERMES_PG_CONN_STR='postgresql://postgres:***@localhost:5432/hermes_work'
hermes-memory preflight
# 16/16 passed

# 5. Run as MCP server (the agent spawns this)
hermes-memory --mcp
```

## Build (from source)

```bash
git clone https://github.com/skb50bd/hermes-memory.git
cd hermes-memory

# Build the Postgres image
docker build -t ghcr.io/skb50bd/hermes-postgres:dev -f docker/postgres/Dockerfile docker/postgres/

# Build the AOT binary
dotnet publish src/Hermes.Memory.Cli/Hermes.Memory.Cli.csproj \
    -c Release -r linux-x64 \
    -p:PublishAot=true \
    -p:PublishSingleFile=true \
    -o ./out/linux-x64
./out/linux-x64/hermes-memory version
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
