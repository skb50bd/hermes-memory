# Changelog

All notable changes to `hermes-memory` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] - 2026-06-06

This is a major rewrite. The previous release (`1.x`) was a hybrid
C# + Python plugin using System.CommandLine for the CLI and a
`pgvector`-backed Postgres container for the data layer. The 2.0.0
release is **pure Python**, ships as a single `pip install`-able
package, and plugs into the hermes-agent plugin loader in-process
(no MCP stdio server, no C# toolchain).

### Breaking changes

- **C# tree removed.** The `src/Hermes.Memory.Core/`,
  `src/Hermes.Memory.Cli/`, `tests/Hermes.Memory.Integration/`,
  `tests/Hermes.Memory.Tests/` directories are gone. The C#
  `hermes-memory-cli` binary is no longer built or shipped.
- **No more MCP server.** 2.0.0 is an in-process plugin; it uses
  hermes-agent's `register_tool(..., override=True)` to supersede
  the built-in `memory` tool directly. This fixes issue #8
  (the built-in memory tool ignoring `provider: postgres`).
- **No more `mcp_servers:` block in `~/.hermes/config.yaml`.** The
  install step that removed the old `mcp_servers:` block now runs
  unconditionally and is idempotent.

### Added

- **Issue #5 fix — 32 KB cap removed.** Long memories are now
  chunked into 512-token windows with 50-token overlap before
  embedding. Each chunk is stored in the new
  `agent_memory.memory_chunks` table with its own HNSW index per
  embedding dimension. Search returns the parent memory, not the
  individual chunk, and a memory is forgotten by deleting all its
  chunks atomically.
- **Issue #8 fix — provider-aware memory tool.** The override
  layer in `src/hermes_memory/override.py` supersedes the built-in
  `memory` tool and routes by `provider` (default, postgres,
  hermes, etc.) so a config with `memory.provider: postgres`
  actually uses Postgres.
- **8 surfaces as in-process plugins.** memory, wiki, journal,
  skills, metrics, kanban, observability, sessions — all registered
  via `register.py` with `override=True` where the built-in tool
  exists. 35+ tools total.
- **Per-profile databases preserved.** Each `~/.hermes/profiles/<name>/`
  has its own `hermes_pg_<name>` database cloned from
  `hermes_template` on first use. Profile isolation works the same
  way it did in 1.x.
- **Interactive install wizard** with 8 named steps, idempotent
  re-runs, single-step dispatch (`hermes-memory install --step 3`),
  and a `--yes` flag for scripted installs.
- **`hermes-memory doctor [--heal] [--json]`.** Diagnoses install
  state and self-heals the most common problem: redacted DSNs in
  `~/.hermes/.env`. Returns a structured report with OK / WARN /
  FAIL severity counts.
- **Defensive in-process healer** for `HERMES_PG_CONN_STR` and
  `PG_MEM_DB_CONN_STR`. If the password slot contains the
  redaction marker, the loader substitutes the real password
  from `~/.hermes/state/hermes-pg-*.password` automatically.
- **Rollback to default file locations.** `hermes-memory uninstall`
  exports memory → `MEMORY.md`, kanban → SQLite, wiki → per-slug
  `.md` files, sessions → jsonl, metrics → jsonl. Switching back
  to the built-in stack is a one-line `hermes-memory uninstall`.
- **Pure-Python CI.** `.github/workflows/ci.yml` is rewritten to
  drop the .NET toolchain. Multi-arch buildx for the postgres
  image, pytest matrix for unit + integration, no more NuGet
  restore.

### Changed

- **Embedder default: bge-m3** (1024-dim, local Ollama). The
  `EmbedderRegistry` dispatches by embedding dimension to the
  correct HNSW index.
- **Migrations are now `migrations/NNNN_*.sql`** with a single
  `0010_memory_chunks.sql` and `0011_kanban_event_actor.sql`
  drift fix.
- **README rewritten** to lead with the Python-only quickstart.

### Fixed

- The 32 KB Postgres-indexed-row limit (issue #5) is gone.
- The memory tool ignoring `provider: postgres` (issue #8) is gone.
- The 2026-06-05 live-migration bug where the auto-redaction
  layer could mangle the DSN password in `~/.hermes/.env` is
  handled by the new heal path (defense-in-depth — see
  `hermes-redacted-agent` skill, quirk 11a).

### Removed

- All C# source, csproj, sln, NuGet config.
- The `mcp_servers:` block from `plugin.yaml`.
- The v1 Python plugin's `pg_memory_store/` directory.
- `install.sh` (replaced by `hermes-memory install` subcommand).
- `compose/` (replaced by the install wizard's POSTGRES step).

## [1.x]

Pre-rewrite hybrid C# + Python. See the `v1` git tag for the
last C#-based release.
