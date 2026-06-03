---
name: hermes-memory
description: Use the hermes-memory platform — Postgres-backed memory, wiki, journal, skills, and metrics. Load when the agent needs to remember something, search prior context, create or link wiki documents, log conversation events, register a skill, or query operational metrics.
---

# hermes-memory

The `hermes-memory` binary is the agent's gateway to a Postgres-backed
platform with five surfaces, all behind a single stdio MCP server:

| Surface | When to use |
|---|---|
| **Memory** | "Remember that..." / "What did I tell you about X?" / atomic facts the agent should recall across sessions |
| **Wiki** | Long-form documents with wikilinks and categories. The agent's knowledge base, browsable |
| **Journal** | Conversation logs. FTS-searchable. Use for "what did we discuss last Tuesday?" |
| **Skills** | Catalog index of installed skills. Search + link graph (depends_on, supersedes, related) |
| **Metrics** | Operational telemetry (latencies, cache hit rates, tool call counts). Aggregations, not raw reads |
| **Kanban** | Multi-tenant task dispatcher. SQLite's old home; now in Postgres. Race-free claim via `SKIP LOCKED`. |

## 1. Before you start

Confirm the binary is reachable and the connection is live:

```bash
hermes-memory preflight
hermes-memory version
```

Both should return non-error output. If `preflight` reports a failure,
**stop and read the failing check name** — it's the answer.

## 2. Memory (the most common surface)

```python
# MCP tool call (handled by the agent runtime)
memory_remember(content="User prefers Postgres over MySQL for new projects", tags=["preferences","databases"], source="user")
memory_search(query="database preferences", top_k=5)
memory_forget(id=42)            # soft-delete
memory_status()                  # counts + embedder cache stats
```

**Empty search result is not an error** — try a rephrasing, broaden the
query, or lower the `hybrid_text_weight` toward 0 (pure vector) for
semantic recall when the FTS pre-filter has no token overlap.

## 3. Wiki

```python
wiki_create(slug="platform-overview", title="Platform Overview", body_md="...", category="projects", tags=["overview","platform"])
wiki_read(slug="platform-overview")
wiki_link(source_slug="platform-overview", target_slug="agent-architecture", context="see the agent layer")
wiki_backlinks(slug="agent-architecture")     # who links to me?
wiki_related(slug="platform-overview", max_hops=2)
wiki_search(query="how does the agent layer work", top_k=5)
```

Slugs are URLs (lowercase, hyphens, no spaces). The `body_md` is
embedded via the same BGE-M3 model as memory — semantic + FTS.

## 4. Journal

```python
journal_log_session(profile="work")
journal_log_message(session_id=1234, role="user", content="how do I...?")
journal_log_message(session_id=1234, role="assistant", content="...", tool_calls='[{"name":"pg_search",...}]')
journal_search(query="deployment question", top_k=20)
```

`role` ∈ `{user, assistant, tool, system}`. `tool_calls` is a JSON
string (MCP doesn't have native JSON args for arrays, so we serialize).

## 5. Skills

```python
skill_register(name="hermes-memory", version="0.1.0", owner="skb50bd", description="...", tags=["platform","memory"])
skill_link(source_skill="hermes-memory", target_skill="dev-framework", kind="related")
skill_index_search(query="memory", top_k=10)
skill_graph(root_skill="hermes-memory", max_hops=2)
```

`kind` ∈ `{depends_on, supersedes, related, see_also}`. The graph query
walks N hops, deduplicates, and returns edges grouped by kind.

## 5b. Kanban

Multi-tenant task dispatcher. The first-class replacement for the
old `~/.hermes/kanban/boards/*/kanban.db` SQLite files. Tenants
replace the free-form `tasks.tenant` text column. Tasks are claimed
race-free via `SELECT ... FOR UPDATE SKIP LOCKED`.

```python
# Manage tenants (boards)
kanban_tenant_create(slug="sv", name="SportsVerse", icon="🪽")
kanban_tenants(include_archived=False)

# Create + list tasks
kanban_create(id="t_abc123", tenant_slug="sv", title="Fix the bug", body="...", priority=10, status="ready")
kanban_list(tenant_slug="sv", status="ready", limit=50)
kanban_get(id="t_abc123")

# Dispatcher (worker process): race-free claim
claimed = kanban_claim(assignee="worker_1", max_runtime_seconds=3600)
# claimed is null if another worker grabbed the task
kanban_heartbeat(id="t_abc123")
kanban_complete(id="t_abc123", summary="fixed in PR #42", result="https://github.com/...")
kanban_fail(id="t_abc123", error="timeout", status="blocked")

# Comments, history, links
kanban_comment(id="t_abc123", body="investigating")
kanban_history(id="t_abc123", limit=50)
kanban_link(parent_id="t_parent", child_id="t_abc123")
kanban_children(parent_id="t_parent")

# Notify subscriptions (Discord/Telegram channels watching a task)
kanban_subscribe(id="t_abc123", platform="discord", chat_id="...", thread_id="...")
```

Status enum: `ready`, `running`, `blocked`, `done`, `crashed`, `timed_out`, `failed`, `archived`, `cancelled`.
Claim returns `{"claimed": true, task: {...}}` on success, `{"claimed": false}` on miss.

## 6. Metrics

```python
metrics_record(profile="work", metric_name="mcp.tool.duration_ms", value=42.5, tags='{"tool":"memory_search"}')
metrics_query(profile="work", metric_name="mcp.tool.duration_ms", from="2026-06-03T00:00:00Z", to="2026-06-03T23:59:59Z", bucket="5 minutes")
```

Returns aggregated rows: `bucket, profile, metric_name, n, avg, min, max, p50, p95, p99`.

## 7. Common pitfalls

- **Empty memory_search result.** The FTS pre-filter requires token overlap. Rephrase, or use a pure-vector path (lower `hybrid_text_weight` to 0).
- **Dim mismatch in embeddings.** The plugin's `vector_<dim>` column must match the live embedder's dim. If you switch dims, the new column is empty until you backfill.
- **Profile DB doesn't exist.** Run `hermes-memory profile list` to see what's there, then `hermes-memory profile create <name>` to clone from `hermes_template`.
- **MCP stdio hung.** The agent runtime spawns the binary per session. A hung session usually means the binary is waiting for a tool call that never came — kill the process and restart the agent.
- **`hermes-memory preflight` reports a missing extension.** The image failed to install one of the 6. Check `docker logs <pg-container>` for the apt/source-build failure.

## 8. Operational commands (not MCP)

The binary has CLI subcommands for human/admin use:

```bash
hermes-memory preflight                                 # 16-check diagnostic
hermes-memory migrate --conn <dsn> --to head             # apply pending migrations
hermes-memory profile create <name>                      # clone hermes_template
hermes-memory profile list                              # show all profile DBs
hermes-memory profile drop <name>                        # nuke a profile DB
hermes-memory embed --text "..."                         # standalone embedder test
hermes-memory version                                    # build info
```

## 9. Architecture reminder

- **One server, one database per agent profile.** Each profile's `.env`
  has `POSTGRES_DATABASE=hermes_<profile_name>`. The server is shared;
  the schemas are uniform because all profile DBs are byte-perfect
  clones of `hermes_template`.
- **5 schemas, not 5 databases.** Memory, wiki, journal, skills, and
  metrics all live in the same database. The journal is a regular
  partitioned table; metrics is a timescaledb hypertable. They share
  the same connection pool.
- **Single binary, stdio MCP, no HTTP.** The agent spawns the binary
  as a subprocess. No port to expose, no auth surface, no service
  to deploy. Restart the agent to restart memory.
