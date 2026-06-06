# hermes-memory

**v2.0.0 — pure-Python rewrite.** No more C#, no more MCP subprocess.
A single PyPI package that registers 46 in-process tools with
Hermes Agent, backed by PostgreSQL + pgvector.

> Closes issues
> [#5](https://github.com/skb50bd/hermes-memory/issues/5)
> (32 KB chunked memory with the routing rule baked in) and
> [#8](https://github.com/skb50bd/hermes-memory/issues/8)
> (the built-in `memory` tool now honours `memory.provider: postgres`).

## What you get

| Surface | Storage | Tools |
|---|---|---|
| **Memory** | `agent_memory.memories` + `memory_chunks` (per-dim vector + FTS) | `memory_remember`, `memory_search`, `memory_forget`, `memory_status` |
| **Wiki** | `hermes_wiki.documents` + `document_links` | `wiki_create`, `wiki_read`, `wiki_link`, `wiki_backlinks`, `wiki_related`, `wiki_search` |
| **Journal** | `hermes_journal.sessions` + `messages` (partitioned) | `journal_log_session`, `journal_log_message`, `journal_search` |
| **Skills** | `hermes_skills.skills` + `skill_links` | `skill_index_search`, `skill_register`, `skill_link`, `skill_graph` |
| **Metrics** | `hermes_metrics.events` (TimescaleDB hypertable) | `metrics_record`, `metrics_query` |
| **Kanban** | `hermes_kanban.*` (9 tables) | 17 tools (`kanban_*`) |
| **Observability** | `hermes_observability.*` | `obs_log`, `obs_record_llm`, `obs_record_tool`, `obs_flush` |
| **Sessions** | `hermes_sessions.*` | `session_open`, `session_append`, `session_messages`, `session_lock_*`, `session_close` |

All 46 tools register **in-process** as hermes-agent plugins (via
`ctx.register_tool()`). No MCP subprocess, no JSON-RPC overhead.

## Quickstart

```bash
# 1. Install
pip install hermes-memory

# 2. Run the 8-step guided wizard
hermes-memory install

# 3. Restart hermes-agent so it picks up the new plugin
hermes restart

# That's it. The 'memory' tool is now backed by postgres.
```

The wizard:

1. **Preflight** — checks Python ≥ 3.11, docker, port 10432
2. **Postgres** — pulls `ghcr.io/skb50bd/hermes-memory/hermes-postgres:latest`
3. **Extensions** — installs `vector`, `pg_trgm`, `ltree`, `age`, `pg_cron`, `timescaledb`
4. **Template** — creates `hermes_template` (8 schemas)
5. **Profile DB** — clones `hermes_template` → `hermes_<profile>`
6. **DSN** — writes `HERMES_PG_CONN_STR` to `~/.hermes/.env`
7. **Embedder** — verifies `bge-m3` (default) via local Ollama
8. **Register** — sets `memory.provider: postgres`, adds the plugin to `plugins.enabled`, removes the old `mcp_servers.hermes-memory` block

State is recorded in `~/.hermes/state/hermes-memory.json` so
`hermes-memory install` is **idempotent** — re-running skips
completed steps.

## Memory vs Wiki routing rule

The `memory_remember` tool rejects content > 32 KB with a
routing-rule error message that points the agent at `wiki_create`:

```
Memory size 32,769 chars exceeds the 32,000-char cap (MEMORY_MAX_CHARS).

Routing rule:
  • MEMORY  — short, durable facts (< 1 screen). Stored via
              memory_remember. Surface: system prompt + searches.
  • WIKI    — long-form, structured, multi-paragraph. Stored via
              wiki_create. Surface: explicit reads, cross-linked.
  • SESSION — never persist; use session_search.

Did you mean: wiki_create with category="projects.<name>"?
```

Long content (< 32 KB but > 2 KB) is auto-chunked into 512-token
overlapping windows stored in `agent_memory.memory_chunks`. Each
chunk gets its own embedding; `memory_search` returns the parent
memory (deduped by `memory_id`).

## Architecture

```
┌──────────────────────────┐  HERMES_PG_CONN_STR  ┌──────────────────────┐
│  hermes-agent            │ ────────────────────▶│  Postgres 18 + 6 exts│
│  (no changes needed)     │                      │  ┌──────────────────┐│
│                          │                      │  │ hermes_template  ││
│  plugin loader           │                      │  └────────┬─────────┘│
│    ↓                    │                      │           │ clone    │
│  hermes-postgres-memory  │                      │  ┌────────▼─────────┐│
│    ↓ register(ctx)      │                      │  │ hermes_default   ││
│  46 in-process tools     │                      │  │ hermes_fluffy    ││
│    ↓ 35+ memories, wiki │                      │  │ hermes_boltu     ││
│       kanban, etc.      │                      │  │ ...              ││
│  + memory override (#8) │                      │  └──────────────────┘│
└──────────────────────────┘                      └──────────────────────┘
```

**No MCP server. No `mcp_servers.hermes-memory` block in config.yaml.**
The plugin registers its tools directly with the hermes-agent
process; the `memory` tool is overridden in-process via the new
`override_builtin=True` flag on `register_tool()` (one tiny PR to
hermes-agent, ~10 LOC).

## CLI

```
hermes-memory install       # 8-step wizard
hermes-memory uninstall     # strip plugin + optional data export
hermes-memory status        # show install state + PG connection
hermes-memory doctor        # health checks (issue #6 — partial)
hermes-memory migrate       # apply SQL migrations to a DSN
hermes-memory export        # dump a surface to JSON/Markdown/SQLite
hermes-memory import        # restore from an export file
hermes-memory rollback      # restore local MEMORY.md from postgres
hermes-memory --version
```

`hermes-memory uninstall --export memory,wiki,kanban,sessions,metrics`
exports each surface to its default file location before tearing
down the plugin (per the 2026-06-06 user clarification):

| Surface   | Default location |
|-----------|-----------------|
| memory    | `~/.hermes/memories/MEMORY.md` (markdown bullet list) |
| wiki      | `~/.hermes/wiki/<slug>.md` (per-slug markdown) |
| kanban    | `~/.hermes/kanban/boards/<tenant>/kanban.json` (manifest; SQLite in v2.1) |
| sessions  | `~/.hermes/sessions/YYYY-MM-DD/session_<id>.jsonl` |
| metrics   | `~/.hermes/metrics/events.jsonl` |
| journal   | `~/.hermes/journal/<sid>.jsonl` (deferred) |
| skills    | `~/.hermes/skills/index.json` (deferred) |

## `hermes update` is a no-op for the memory surface

The plugin is installed as a pip package at
`~/.hermes/plugins/hermes-postgres-memory/` (the user-plugins path,
not the bundled-plugins path). After install, the hermes-agent repo
working tree is **clean**:

```bash
$ git -C ~/.hermes/hermes-agent status --porcelain
# (empty)
```

`hermes update` no longer touches any hermes-memory file. No
symlinks, no shims, no `agent_init.py` patches, no `mcp_servers`
block. Verified by the install wizard + the non-invasion check in
the docs.

## Development

```bash
git clone https://github.com/skb50bd/hermes-memory
cd hermes-memory
pip install -e ".[dev]"

# Run unit tests (no PG required)
pytest tests/unit/

# Run integration tests (Testcontainers Postgres)
pytest tests/integration/

# Lint
ruff check src tests
```

The 8-step install wizard lives at `src/hermes_memory/install/`.
The plugin entry point is `src/hermes_memory/register.py`.

## What's NOT in v2.0.0

- **SQL for 7 of 8 surfaces.** `PgMemoryRepo` is fully implemented
  (chunked insert, hybrid FTS+vector search, dedup, forget, status).
  `PgWikiRepo`, `PgJournalRepo`, `PgSkillsRepo`, `PgMetricsRepo`,
  `PgKanbanRepo`, `PgObservabilityRepo`, `PgSessionsRepo` ship as
  stubs that raise `NotImplementedError` for writes and return
  empty results for reads. Landed as a follow-up.
- **Issue #6** (hermes doctor full check list) and **#7** (dump/restore
  tooling) deferred to v2.1.
- **Per-profile DB creation** uses the existing `hermes-bootstrap.sh`
  flow inside the Docker image; the new install wizard delegates to
  it via subprocess.
- **SQLite kanban export.** v2.0 writes a JSON manifest per tenant;
  full SQLite export lands in v2.1.

## Migration from v1 (C# + MCP)

1. `pip install hermes-memory`
2. `hermes-memory install` (it's idempotent — re-running the v1
   install.sh first is safe)
3. The install wizard detects the old `mcp_servers.hermes-memory`
   block in `~/.hermes/config.yaml` and **removes it** (so the C#
   binary stops being launched)
4. `hermes restart`

No data migration needed — the same Postgres database (same DSN,
same schemas) works for both the C# stdio MCP server and the new
in-process Python plugin.

## License

MIT. See [LICENSE](LICENSE).
