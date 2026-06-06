# Plan: hermes-memory v2 — Python-only, hermes-agent-native, single-command install

> **Status:** Draft for review.
> **Goal:** Drop all C# source/build, ship a pure-Python plugin that
> registers its own tools in-process, overrides the built-in `memory`
> tool, fixes issues **#5** and **#8**, and is installable in one
> command. Ships as a PyPI package; lands in the hermes-agent
> plugins ecosystem.
>
> **Decisions (locked 2026-06-06):**
>
> 1. Override model — **plugin owns it** (no hermes-agent patch).
> 2. C# — **delete all C#** (sources, tests, sln, csproj, global.json, props).
> 3. Tool surface — **drop the MCP server, register everything as plugin tools** via `ctx.register_tool()`.
> 4. Memory cap — **32 KB raw, chunked to 512-token windows** on insert.
> 5. Install — **PyPI package + post-install hook** (`pip install hermes-memory[mcp]`).
> 6. Rollback — `hermes-memory uninstall` offers an **export-and-restore** step that can write data back to "default" file/format locations for memory, kanban, wiki, sessions, metrics (i.e. a real data-export path, not just a teardown).
> 7. Embedder default — **bge-m3, 1024-dim, local Ollama** (`10.49.0.52:11434`).
> 8. MCP compat — **hard-dropped**. No `hermes-memory mcp` subcommand. External MCP clients that pointed at the C# stdio server get a `README` migration note: switch to the plugin, data is identical.
> 9. CI — **multi-arch amd64+arm64** Postgres image, buildx on amd64 hosts (unchanged from current).
> 10. Scope — **port all 8 surfaces in this PR** (memory, wiki, journal, skills, metrics, kanban, observability, sessions).
> 11. Profile isolation — **per-profile DBs** cloned from `hermes_template` (unchanged).

---

## 1. What we're delivering

A single PyPI package, `hermes-memory`, with the following structure:

```
hermes-memory/
├── pyproject.toml             # PEP 621; console_scripts entry: hermes-memory
├── README.md                  # Quickstart, what's replaced, rollback
├── CHANGELOG.md               # v2.0.0 — Python-only rewrite
├── LICENSE                    # MIT
├── src/
│   └── hermes_memory/
│       ├── __init__.py
│       ├── plugin.yaml        # name: hermes-postgres-memory, hooks, deps
│       ├── register.py        # register(ctx) — the plugin entry
│       ├── tools/             # All the agent-facing tool implementations
│       │   ├── memory.py      # memory_remember, memory_search, memory_forget, memory_status
│       │   ├── wiki.py        # wiki_create/read/link/backlinks/related/search
│       │   ├── journal.py     # journal_log_session/log_message/search
│       │   ├── skills.py      # skill_index_search/register/link/graph
│       │   ├── metrics.py     # metrics_record/query
│       │   └── kanban.py      # 17 kanban_* tools
│       ├── repos/             # DB access — pure Python, psycopg2/psycopg3
│       │   ├── dsn.py         # DSN normalization
│       │   ├── pool.py        # Thread-safe connection pool
│       │   ├── memory_repo.py
│       │   ├── wiki_repo.py
│       │   ├── journal_repo.py
│       │   ├── skills_repo.py
│       │   ├── metrics_repo.py
│       │   └── kanban_repo.py
│       ├── embeddings/
│       │   ├── registry.py    # Embedder registry, dim dispatch
│       │   ├── http_embedder.py   # Ollama / OpenAI / generic OpenAI-compat
│       │   └── chunker.py     # 512-token windows for issue #5
│       ├── override.py        # Issue #8 — supersede built-in memory tool
│       ├── cli.py             # `hermes-memory install|uninstall|status|doctor|migrate|version|export|import|rollback`
│       ├── install/           # The 12 install steps (port from bash)
│       │   ├── steps.py
│       │   ├── preflight.py
│       │   ├── postgres.py
│       │   ├── extensions.py
│       │   ├── template.py
│       │   ├── profiles.py
│       │   ├── dsn.py
│       │   ├── embedder.py
│       │   ├── register_plugin.py
│       │   ├── register_mcp_replace.py
│       │   ├── smoke.py
│       │   └── summary.py
│       ├── uninstall/
│       │   ├── teardown.py
│       │   └── export.py      # Issue (user-2026-06-06) — export to file/format
│       ├── migrate.py         # SQL migration runner
│       ├── rollback.py        # Local-MEMORY.md restore + config revert
│       └── errors.py          # Routing-rule-aware error messages
├── migrations/                # Copied verbatim from current repo (pure SQL)
│   ├── 0001_agent_memory.sql
│   ├── 0002_wiki.sql
│   ├── 0003_journal.sql
│   ├── 0004_skills.sql
│   ├── 0005_metrics.sql
│   ├── 0006_kanban.sql
│   ├── 0007_wiki_chunks.sql
│   ├── 0008_sessions.sql
│   └── 0009_observability.sql
├── docker/
│   └── postgres/              # Dockerfile + init scripts (unchanged)
├── tests/
│   ├── unit/                  # pytest — repos, chunker, override
│   ├── integration/           # pytest + testcontainers — full schema
│   └── e2e/                   # plugin registration + tool invocation
├── compose/                   # dev compose (unchanged)
├── scripts/
│   └── pre-commit             # ruff + shellcheck + yamllint (no dotnet)
└── .github/workflows/
    └── ci.yml                 # rewritten — pytest + ruff + buildx, no dotnet
```

`src/hermes_memory/` is the in-package directory; the plugin's
`plugin.yaml` and `__init__.py` live at the top of the package so a
plain `pip install` places them where the hermes-agent plugin loader
will find them. (The loader accepts `pip`-installed plugins via the
`hermes_agent.plugins` entry-point group — see
`hermes_cli/plugins.py:4-22`.)

---

## 2. Plugin manifest (`plugin.yaml`)

```yaml
name: hermes-postgres-memory
version: 2.0.0
description: >-
  PostgreSQL + pgvector memory, wiki, journal, skills, metrics,
  kanban, sessions, and observability for Hermes Agent. Pure-Python,
  no MCP overhead, registers all tools in-process.
author: Shakib Haris
license: MIT
pip_dependencies:
  - psycopg[binary]>=3.1
  - httpx>=0.27
requires_env:
  - PG_MEM_DB_CONN_STR
entry_points:
  console_scripts:
    - hermes-memory = hermes_memory.cli:main
hooks:
  - on_session_end
  - pre_tool_call       # see override.py — fixes #8
provides_tools:
  - memory_remember
  - memory_search
  - memory_forget
  - memory_status
  - wiki_create
  - wiki_read
  - wiki_link
  - wiki_backlinks
  - wiki_related
  - wiki_search
  - journal_log_session
  - journal_log_message
  - journal_search
  - skill_index_search
  - skill_register
  - skill_link
  - skill_graph
  - metrics_record
  - metrics_query
  - kanban_create
  - kanban_list
  - kanban_get
  - kanban_claim
  - kanban_heartbeat
  - kanban_complete
  - kanban_fail
  - kanban_comment
  - kanban_history
  - kanban_link
  - kanban_children
  - kanban_parents
  - kanban_tenants
  - kanban_tenant_create
  - kanban_subscribe
  - kanban_unsubscribe
  - kanban_search
overrides_builtin:
  - memory              # fixes #8 — supersedes tools/memory_tool.py
```

`overrides_builtin: [memory]` is the **load-bearing field for issue
#8**. The hermes-agent plugin loader must honor it. Two options:

* **Option A (preferred):** Add a one-line check to
  `tools/registry.register()` in hermes-agent: if the tool name
  matches a registered override, drop the previous registration
  silently. **One PR to hermes-agent, ~10 LOC.**
* **Option B (fallback):** The plugin's `pre_tool_call` hook
  intercepts the `memory` tool calls before they reach the built-in
  `MemoryStore` and re-routes them to `pg_remember` /
  `pg_search` / `pg_forget`. No hermes-agent patch. Slightly
  hackier (we're hijacking a tool we don't own) but zero upstream
  coordination.

We'll go with **Option A** — single small PR to hermes-agent, then
upstreamable. Fall back to B if the PR gets stuck.

---

## 3. Issue #8 fix (memory provider routing) — detailed

**Symptom:** `tools/memory_tool.py` always writes to
`~/.hermes/memories/MEMORY.md` regardless of `memory.provider: postgres`.

**Fix path:**

1. **Plugin side:** `src/hermes_memory/override.py` exports a
   `pg_remember(text, *, source=None, category=None, tags=None) -> int`
   that wraps the underlying repo, plus `pg_search(query, *, top_k=10)`
   and `pg_forget(memory_id)`. These are what the override routes to.

2. **Loader side:** `register(ctx)` calls
   `ctx.register_tool("memory", _memory_tool_impl, override_builtin=True)`.
   The implementation checks `mem_config.get("provider", "")`:
   - empty / `"local"` → behave as the built-in (delegate to
     `MemoryStore`).
   - `"postgres"` → route to `pg_remember` / `pg_search` / `pg_forget`.

3. **System prompt side:** `register(ctx)` also registers
   `on_session_end` and `pre_tool_call` hooks so the agent can refresh
   its system-prompt MEMORY block from postgres on demand. The block
   builder is `src/hermes_memory/budgeter.py` (already exists in the
   current python plugin — port verbatim).

4. **Migration:** On first boot with `provider: postgres`, check for
   existing local `MEMORY.md`. If found, prompt: "Migrate 1,942
   chars of local memory to postgres? [Y/n]". If yes, run a
   one-shot migration (idempotent via `idempotency_key`).

5. **Acceptance:**
   - [ ] `memory` tool's `add`/`replace`/`remove` writes to postgres when configured.
   - [ ] System-prompt MEMORY block reads from postgres.
   - [ ] Local `MemoryStore` is not instantiated when provider=postgres.
   - [ ] One-shot migration command for existing `MEMORY.md` files.
   - [ ] No regression when provider is unset.

---

## 4. Issue #5 fix (32 KB cap + routing rule) — detailed

**Symptom:** `pg_remember` rejects facts above the current per-fact
cap. Agent has no documented rule for memory-vs-wiki.

**Fix path:**

1. **Schema:** No column change needed — `agent_memory.memories.content`
   is already `text`. The current cap is a Python-side check, not a
   column constraint.

2. **Chunker:** New `src/hermes_memory/embeddings/chunker.py`:
   - 512-token windows (≈ 2,000 chars English)
   - 50-token overlap
   - Token counting via a cheap `len(text) // 4` heuristic for
     English, with a clear "approximate" disclaimer

3. **Schema addition:** New table `agent_memory.memory_chunks`:
   ```sql
   CREATE TABLE agent_memory.memory_chunks (
     id            BIGSERIAL PRIMARY KEY,
     memory_id     BIGINT NOT NULL REFERENCES agent_memory.memories(id) ON DELETE CASCADE,
     chunk_index   INT NOT NULL,
     content       TEXT NOT NULL,
     token_count   INT NOT NULL,
     embedding     VECTOR(1024),   -- default dim; per-dim HNSW indexes added
     content_tsv   TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
     created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
   );
   CREATE INDEX memory_chunks_memory_id_idx ON agent_memory.memory_chunks (memory_id);
   -- HNSW per dim, mirroring the per-dim HNSW pattern on agent_memory.memories
   ```
   Migration file: `migrations/0010_memory_chunks.sql`.

4. **Insert path:** `pg_remember(content, ...)`:
   - If `len(content) <= 2048` → store single row, single embedding
     (today's path).
   - Else → store the parent row (full content, no embedding) +
     insert one chunk row per window. Each chunk gets its own
     embedding. Dedup is on `(memory_id, chunk_index)`.

5. **Search path:** `pg_search(query, top_k)` searches `memory_chunks`
   by both FTS and vector, returns the **parent memory** (deduped
   by `memory_id`, ranked by max chunk score).

6. **Cap message:** The "too large" error path in
   `src/hermes_memory/errors.py`:
   ```
   Memory at 38,420 / 32,000 chars. Even with chunking, this is too
   big for the MEMORY tool — it will be stored, but only the first
   1,000 chars will surface in your system-prompt MEMORY block.

   Routing rule:
     • MEMORY  — short, durable facts (< 1 screen). Stored via
                 memory_remember. Surface: system prompt + searches.
     • WIKI    — long-form, structured, multi-paragraph. Stored via
                 wiki_create. Surface: explicit reads, cross-linked.
     • SESSION — never persist; use session_search.

   Did you mean: wiki_create with category="projects.<name>"?
   ```
   The error message is the rule.

7. **Docs:** Update `README.md` "Memory vs Wiki routing" section. Add
   to `src/hermes_memory/AGENTS.md` (a new file). Reference the rule
   in the SOUL.md pointer (skill-level, not SOUL — keeps SOUL under
   15 lines per the 2026-06-04 constraint).

8. **Acceptance:**
   - [ ] `pg_remember` accepts facts up to 32 KB.
   - [ ] Chunks stored in `agent_memory.memory_chunks`.
   - [ ] `pg_search` returns parent memories, deduped.
   - [ ] Error message includes the routing rule.
   - [ ] README + AGENTS.md document the rule.

---

## 5. Override & no-MCP architecture

**Why no MCP:** the hermes-agent plugin loader already supports
`ctx.register_tool()`. All 35+ tools register in-process. The
`mcp_servers.hermes-memory` block in `~/.hermes/config.yaml` is
**removed** on install (the install step detects it and offers to
strip it). The previous C# MCP server is no longer launched, which
saves ~50 MB of memory and one subprocess per session.

**What we lose:** external MCP clients (Claude Code CLI, etc.) can
no longer talk to hermes-memory over stdio. Per the 2026-06-06
decision (hard-drop), we do **not** ship a `hermes-memory mcp`
subcommand. The README documents the migration: "if you used the
C# stdio MCP server from Claude Code / Codex CLI, switch to the
plugin — your data is identical, and all 35+ tools become available
in-process."

**What we gain:** zero IPC, no JSON-RPC overhead, hermes-agent can
discover/filter tools normally, and `hermes update` doesn't have to
recompile a C# binary or restart an MCP subprocess.

---

## 6. Install flow — single command, guided

```bash
# User runs ONE of:
pip install --upgrade hermes-memory[mcp]
# ...or, for the no-PyPI path (kept for air-gapped / Nix):
curl -sSL https://raw.githubusercontent.com/skb50bd/hermes-memory/v2/install.py | python3 -

# Then the post-install hook prompts:
$ hermes-memory install
┌─ hermes-memory v2.0.0 install wizard ────────────────────────────┐
│                                                                  │
│  Preflight:                                                      │
│    ✓ Python 3.11                                                 │
│    ✓ Docker present                                              │
│    ✓ Port 10432 free                                             │
│    ? Existing PG container? [Use existing / Start fresh]         │
│                                                                  │
│  Step 1/8 — Postgres container                                   │
│    Image: ghcr.io/skb50bd/hermes-memory/hermes-postgres:latest   │
│    Name: hermes-postgres                                         │
│    Port: 10432                                                   │
│    Continue? [Y/n]                                               │
│                                                                  │
│  Step 2/8 — Extensions                                           │
│    Will install: vector, pg_trgm, ltree, age, pg_cron,           │
│                  timescaledb                                     │
│    [Press Enter]                                                 │
│                                                                  │
│  Step 3/8 — Template database                                    │
│    Create hermes_template with 5 schemas + 5 extensions           │
│    [Press Enter]                                                 │
│                                                                  │
│  Step 4/8 — Profile database                                     │
│    Profile: hermes_default (matches your current profile)        │
│    Will clone hermes_template → hermes_default                   │
│    [Press Enter]                                                 │
│                                                                  │
│  Step 5/8 — DSN                                                  │
│    postgresql://hermes:***@127.0.0.1:10432/hermes_default        │
│    Write to ~/.hermes/.env: HERMES_PG_CONN_STR=...               │
│    Continue? [Y/n]                                               │
│                                                                  │
│  Step 6/8 — Embedder                                             │
│    Default: Ollama bge-m3 (1024-dim)                             │
│    [Use default / Configure / Skip]                              │
│                                                                  │
│  Step 7/8 — Plugin registration                                  │
│    Install:  ~/.hermes/plugins/hermes-postgres-memory/           │
│    Update:   ~/.hermes/config.yaml (plugins.enabled,             │
│              mcp_servers.hermes-memory removed)                   │
│    Set:      memory.provider: postgres                           │
│    Continue? [Y/n]                                               │
│                                                                  │
│  Step 8/8 — Smoke test                                           │
│    pg_remember("install test")  → OK                            │
│    pg_search("install")        → 1 result                       │
│    memory_status               → 1 memory, 0 chunks             │
│    [Press Enter]                                                 │
│                                                                  │
│  ✓ Install complete. Run `hermes restart` to apply.              │
└──────────────────────────────────────────────────────────────────┘
```

Implementation: `src/hermes_memory/install/steps.py` orchestrates
eight `Step` objects. Each step is independently runnable
(`hermes-memory install --from 5`). State is recorded in
`~/.hermes/state/hermes-memory.json` so `--check` and
`hermes-memory update` are idempotent.

**Non-invasive by construction:**

- Plugin files drop into `~/.hermes/plugins/hermes-postgres-memory/`
  (the loader's **user-plugins** path, NOT the bundled-plugins path
  that gets clobbered by `hermes update`).
- `hermes update` pulls hermes-agent's main branch, which has
  symlinks to `~/repos/hermes-memory/plugins/*` (per current
  convention). The python-only build removes those symlinks — the
  Python code now lives in the **installed package**, not in the
  repo. `hermes update` is therefore a no-op for the memory
  surface. Verified by `git -C ~/.hermes/hermes-agent status` —
  after this work, it should show **zero** dirty files in
  `plugins/memory/`, `plugins/kanban/`, etc.
- `mcp_servers.hermes-memory` block is **removed** on install.
  Re-adding it is a one-liner in `config.yaml`.
- `memory.provider: postgres` is **set** on install. Reverting
  to `""` is a one-liner.

---

## 7. `hermes update` non-invasion — explicit guarantee

The current dirty-state problem is real (saw it: `M agent/agent_init.py`,
`M hermes_logging.py`, `M run_agent.py`, `M tools/browser_tool.py`,
`?? hermes_cli/kanban_db_pg_shim.py`, `?? plugins/kanban/postgres`,
`?? plugins/memory/postgres`, `?? plugins/observability/postgres`,
`?? plugins/session/`). After this work, `hermes update` should leave
the user with:

- `~/.hermes/plugins/hermes-postgres-memory/` (an installed pip
  package, not a symlink — survives `hermes update`).
- `~/.hermes/.env` with `HERMES_PG_CONN_STR=...` (survives update).
- `~/.hermes/config.yaml` with `memory.provider: postgres` and the
  plugin listed in `plugins.enabled` (survives update).
- `~/.hermes/state/hermes-memory.json` recording install state
  (survives update).
- The hermes-agent repo working tree should be **clean** — no
  untracked files, no symlinks, no shim, no agent_init.py patch.
  Verified by `git -C ~/.hermes/hermes-agent status --porcelain`
  returning empty.

To achieve that we need **exactly one** small change to
hermes-agent: the `overrides_builtin` field on `register_tool()`
(see §2). That PR is ~10 LOC.

---

## 8. Rollback — `uninstall` with data-export

Per the user's clarification: `uninstall` should offer a
data-restore step that can write back to default file/format
locations for memory, kanban, wiki, sessions, metrics. So:

```bash
$ hermes-memory uninstall
┌─ hermes-memory uninstall wizard ─────────────────────────────────┐
│                                                                  │
│  Step 1/4 — Stop the plugin                                      │
│    Unregister hermes-postgres-memory from config.yaml            │
│    Remove ~/.hermes/plugins/hermes-postgres-memory/              │
│    Continue? [Y/n]                                               │
│                                                                  │
│  Step 2/4 — Data export (interactive)                            │
│    Memory    → ~/.hermes/memories/MEMORY.md (markdown bullet     │
│                list)            [Yes / Skip]                     │
│    Kanban    → ~/.hermes/kanban/boards/<tenant>/kanban.db        │
│                (SQLite, matches old format) [Yes / Skip]         │
│    Wiki      → ~/.hermes/wiki/<slug>.md         [Yes / Skip]     │
│    Sessions  → ~/.hermes/sessions/YYYY-MM-DD/                   │
│                (jsonl per session)            [Yes / Skip]       │
│    Metrics   → ~/.hermes/metrics/events.jsonl  [Yes / Skip]      │
│                                                                  │
│  Step 3/4 — Config revert                                        │
│    memory.provider: "" (was "postgres")                          │
│    mcp_servers.hermes-memory block: removed (was present)        │
│    Continue? [Y/n]                                               │
│                                                                  │
│  Step 4/4 — Container & DB                                       │
│    [Stop & remove container / Leave running / Wipe DBs]          │
│                                                                  │
│  ✓ Uninstall complete. Run `hermes restart` to apply.            │
│    To re-enable: `hermes-memory install`                         │
└──────────────────────────────────────────────────────────────────┘
```

The export step is the new code in
`src/hermes_memory/uninstall/export.py`. Reuses the export machinery
from issue #7 (JSON / XML with/without vectors) once that lands, but
adds the **per-surface "default location"** mapping above so users
get file paths, not just a JSON dump.

If the user already did `--no-pip` and the plugin is symlinked into
hermes-agent, `uninstall` also cleans the symlinks. The current
working-tree dirty state gets resolved during uninstall.

---

## 9. TDD workflow

Per dev-standards and the existing `scripts/pre-commit` (which we
update to drop `dotnet` and add `ruff`).

### Test matrix

| Surface | Unit | Integration (Testcontainers PG) | E2E (plugin loader) |
|---|---|---|---|
| Repos (memory, wiki, journal, skills, metrics, kanban) | ✓ | ✓ | — |
| Chunker (issue #5) | ✓ | ✓ (real PG) | — |
| Override (issue #8) | ✓ | — | ✓ (real plugin loader) |
| Embedder registry | ✓ | — | — |
| Install wizard | — | ✓ (idempotency) | — |
| Uninstall + export | — | ✓ (round-trip) | — |
| `migrate` | — | ✓ (head-to-head) | — |
| `hermes-memory doctor` (issue #6, even though not in scope) | — | ✓ | — |

CI gates: `ruff check` → `pytest tests/unit` →
`pytest tests/integration --testcontainers` → buildx postgres image
→ smoke.

Heavy load tests (kanban contention, pgvector recall) stay **local,
on a schedule**, not in CI — per the baseline rule.

---

## 10. CI / .github/workflows/ci.yml — rewrite

Drop the dotnet jobs entirely. New structure:

```yaml
jobs:
  changes:        # path-filter
  ruff:           # rtk ruff check (no .cs files exist)
  unit:           # pytest tests/unit
  integration:    # pytest tests/integration with testcontainers
  build-image:    # buildx multi-arch amd64+arm64
  smoke-image:    # start container, run migrate, run smoke
  publish:        # tag-driven: pip build + PyPI upload + image push
```

The `Hermes.Memory.Cli/bin/Release/net10.0/hermes-memory` path
disappears from CI entirely. No more dotnet install, no more NativeAOT
build, no more multi-arch C# binary.

---

## 11. Submission to hermes-agent repo

We want this to land in `hermes-agent/plugins/memory/hermes-postgres/`
eventually. To make that reviewable:

1. Repo shape: `src/hermes_memory/` is the importable package.
2. The plugin manifest (`plugin.yaml`) lives at the package root, not
   in a separate `plugins/` dir, so the same file works whether
   hermes-agent vendors it as a subdirectory or installs it as a pip
   package.
3. The single PR to hermes-agent is the `overrides_builtin` hook
   support in `tools/registry.py`. Tiny, low-risk, well-justified by
   issue #8.

We'll land:
- The hermes-memory v2.0.0 release.
- The hermes-agent PR for `overrides_builtin` (cross-link them).
- A `plugins/memory/hermes-postgres/` reference under
  `optional-plugins/` in hermes-agent that just documents
  `pip install hermes-memory[mcp]` (one PR).

---

## 12. Open questions — all answered 2026-06-06

| # | Question | Answer |
|---|---|---|
| 1 | Embedding model default | bge-m3, 1024-dim, local Ollama |
| 2 | Keep multi-arch CI | Yes |
| 3 | MCP compat subcommand | Hard-drop, no `mcp` subcommand |
| 4 | Port observability + sessions in this PR | Yes, port all 8 |
| 5 | Profile DB isolation | Per-profile (current behavior) |

**No further questions. Starting implementation in this order:**

1. New `migrations/0010_memory_chunks.sql` (issue #5)
2. `src/hermes_memory/embeddings/chunker.py`
3. `src/hermes_memory/repos/memory_repo.py` — add chunked insert/search
4. `src/hermes_memory/tools/memory.py` — error routing rule baked in
5. `src/hermes_memory/override.py` — supersede built-in `memory` tool
6. `src/hermes_memory/repos/{wiki,journal,skills,metrics,kanban,observability,sessions}_repo.py`
7. `src/hermes_memory/tools/{wiki,journal,skills,metrics,kanban,observability,sessions}.py`
8. `src/hermes_memory/install/` — 8-step wizard ported from `install.sh`
9. `src/hermes_memory/uninstall/export.py` — per-surface default-location exporter
10. `src/hermes_memory/cli.py` — `install|uninstall|status|doctor|migrate|version|export|import|rollback`
11. `src/hermes_memory/plugin.yaml` + `register.py`
12. `tests/unit/` + `tests/integration/` (testcontainers)
13. `pyproject.toml`
14. Delete C# tree + rewrite `.github/workflows/ci.yml`
15. Small PR to hermes-agent: `register_tool(..., override_builtin=False)` in `tools/registry.py`
16. Submit

---

## Appendix A: files to delete vs keep

**Delete (was C#):**
```
src/                                          (entire tree)
tests/Hermes.Memory.Integration/              (entire tree)
tests/Hermes.Memory.Tests/                    (if exists)
Hermes.Memory.sln
Directory.Build.props
Directory.Packages.props
global.json
```

**Keep (unchanged or lightly modified):**
```
migrations/                                   (9 SQL files + new 0010)
docker/                                       (postgres image)
compose/                                      (dev compose)
README.md                                     (rewrite)
install.sh                                    (delete — replaced by Python)
scripts/pre-commit                            (rewrite — drop dotnet)
.gitignore
LICENSE
```

**New (this work):**
```
src/hermes_memory/                            (the package)
tests/unit/
tests/integration/
pyproject.toml
.github/workflows/ci.yml                      (rewrite)
```

---

## Appendix B: hermes-agent PR — `overrides_builtin` support

`tools/registry.py` currently does:

```python
def register(name, func, ...):
    _TOOLS[name] = func
```

Change to:

```python
def register(name, func, *, override_builtin: bool = False, ...):
    if name in _TOOLS and not override_builtin:
        raise ToolConflict(name)
    _TOOLS[name] = func
```

Plugin's `register(ctx)` calls
`ctx.register_tool("memory", _pg_memory_impl, override_builtin=True)`.
The existing built-in registration in `model_tools.py` doesn't pass
`override_builtin`, so a conflict is raised — exactly what we want
during the hermes-agent update, the plugin's `register()` runs
**after** the built-ins, and the override wins.

That's the whole PR. ~10 LOC.
