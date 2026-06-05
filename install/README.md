# hermes-memory interactive install

A guided, idempotent installer for the hermes-memory stack. Brings up a
self-hosted Postgres in Docker, runs migrations, wires DSN, picks an
embedder, registers the C# MCP server, and prints a clear summary.

## Quick start

```bash
# From the repo root, via the top-level shim
./install.sh                  # full interactive install
./install.sh --check          # idempotent: only run missing steps
./install.sh --update         # alias for --check
./install.sh --status         # print current state JSON
./install.sh --step 5         # run a single step
./install.sh --from 5         # resume from step 5
./install.sh --uninstall      # reverse everything
./install.sh --yes            # non-interactive: take defaults
```

## From the C# binary

The C# binary is the canonical entry point. It shells out to the same
`install.sh` shim, so behavior is identical:

```bash
hermes-memory install [--check|--update|--uninstall] [--from N] [--step N] [--yes]
hermes-memory uninstall [--yes]
hermes-memory --help            # see all subcommands
```

## What it does

11 steps, in order:

| #  | Step            | What it does                                                                |
|----|-----------------|-----------------------------------------------------------------------------|
| 0  | preflight       | Detect docker, compose v2, hermes CLI, repo, ports, internet                |
| 1  | postgres        | Start the self-hosted Postgres container (or detect already-up)             |
| 2  | extensions      | Verify vector / postgis / timescaledb / age / pg_cron / pg_trgm installed   |
| 3  | template        | Create `hermes_template` from the schema migrations                         |
| 4  | profiles        | Create per-agent profile DBs (e.g. `hermes_default`) cloned from template   |
| 5  | dsn             | Resolve DSN (password from env, compose, or prompt) and write to test dir   |
| 6  | embedder        | Pick embedder provider per dim (ollama_local / kimi / openai)               |
| 7  | binary          | Locate or build the `hermes-memory` C# binary                               |
| 8  | mcp             | Register the MCP server in `~/.hermes/config.yaml`                          |
| 9  | introduce       | Print the tool list the agent can now see (Python plugin + C# MCP)         |
| 10 | smoke           | End-to-end probe: pg_search returns a seed memory                           |
| 11 | summary         | Print the final install card with next steps                               |

## Where state lives

`~/.hermes/state/hermes-memory.json` — JSON, auto-saved after every step.
Read it with `./install.sh --status`.

## Architecture

```
┌──────────────────────────────────────────┐
│  C# binary (hermes-memory)               │  ← canonical entry
│  - install / uninstall subcommands       │
│  - process.Subprocess → bash install.sh  │
└─────────────┬────────────────────────────┘
              │
              ▼
┌──────────────────────────────────────────┐
│  install.sh  (top-level shim)            │  ← 30 lines
│  - arg parsing, --check/--uninstall      │
│  - iterates install/steps/[0-9]*.sh      │
└─────────────┬────────────────────────────┘
              │
              ▼
┌──────────────────────────────────────────┐
│  install/steps/_dispatch.sh             │  ← finds NN_*.sh for HERMES_STEP
│  install/steps/NN_*.sh                   │  ← 11 shims, each `exec python3 _step_run.py`
└─────────────┬────────────────────────────┘
              │
              ▼
┌──────────────────────────────────────────┐
│  install/steps/_step_run.py             │  ← the orchestrator, ~880 lines
│  - State, UI, detect, pg, compose,       │
│    dsn, embedder, mcp, report libraries  │
│  - 11 step functions, registered via     │
│    @register(0..11) decorator            │
└──────────────────────────────────────────┘
```

## Why Python orchestrator + bash shims?

The redaction system mangles bash quoted strings containing literal
passwords (`changeme`, `HERMES_PG_PASSWORD` etc.) — the substituted
output corrupts syntax. Python is unaffected, so all logic lives in
Python and bash is just a 200-byte dispatcher per step. Same wizard
behavior, no brittle quoting.

## Idempotency

Re-running `./install.sh` (or `./install.sh --check`) is safe. The
state file is the source of truth; each step is responsible for
checking "am I already done?" before mutating anything. Steps 1, 2, 3,
4, 7, 8 all no-op cleanly when their target already exists.
