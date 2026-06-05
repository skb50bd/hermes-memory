"""CLI subcommands for the postgres memory provider (canonical schema).

Discovery convention: this file is auto-loaded by Hermes Agent's
plugin CLI discovery. The `register_cli(subparser)` function is the
entry point. Subcommands appear under `hermes postgres <sub>`.

Schema
------
Uses the canonical layout:
  - agent_memory.memories      — content + vector_768/1024/1536 + FTS
  - agent_memory.settings      — key/value (default_dim, etc.)
  - agent_memory.models        — per-dim provider/model/base_url/api_key_env

Subcommands
-----------
- status                — show plugin status, registry, per-dim fill counts
- model-list            — list the per-dim model registry
- model-set             — switch default dim and/or override model
- backfill              — run scripts/backfill_embeddings.py
- find-empty            — list memory IDs whose target column is null/all-zero
- find-missing          — find rows missing any/all vector columns
- embed-text            — embed arbitrary text, print the vector
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import List, Optional

import psycopg2


# ── Connection ──────────────────────────────────────────────────────────


def _conn():
    """Build a psycopg2 connection from required PG_MEM_DB_CONN_STR."""
    from psycopg2.extensions import make_dsn
    from plugins.memory.postgres import get_pg_mem_db_conn_str
    return psycopg2.connect(
        make_dsn(
            dsn=get_pg_mem_db_conn_str(),
            connect_timeout=5,
            application_name="hermes-memory-cli",
        )
    )


def _memory_table_exists(cur) -> bool:
    cur.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'agent_memory' AND table_name = 'memories')"
    )
    return cur.fetchone()[0]


# ── Subcommand handlers ─────────────────────────────────────────────────


def cmd_status(args, parser) -> int:
    """Print provider status as JSON."""
    from plugins.memory.postgres import (
        _PostgresClient, get_embedder, SUPPORTED_DIMS,
    )
    try:
        client = _PostgresClient()
        with client._cursor() as cur:
            cur.execute("SELECT version()")
            version = cur.fetchone()[0]
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            v = cur.fetchone()
            cur.execute(
                "SELECT COUNT(*) FROM agent_memory.memories WHERE deleted_at IS NULL"
            )
            total = cur.fetchone()[0]
        per_dim = client.count_by_dim()
        embedders = {}
        for d in SUPPORTED_DIMS:
            try:
                e = get_embedder(d)
                embedders[str(d)] = {
                    "provider": e.provider,
                    "model": e.model,
                    "stats": e.stats(),
                }
            except Exception as exc:
                embedders[str(d)] = {"error": str(exc)}
        print(json.dumps({
            "status": "connected",
            "postgres_version": version,
            "pgvector_version": v[0] if v else "not installed",
            "total_memories": total,
            "default_dim": client.default_dim,
            "per_dim_embedded": per_dim,
            "embedders": embedders,
        }, indent=2))
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def cmd_model_list(args, parser) -> int:
    """List the per-dim model registry."""
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT dim, provider, model, base_url, api_key_env "
                "FROM agent_memory.models ORDER BY dim"
            )
            rows = cur.fetchall()
        if not rows:
            print("agent_memory.models is empty; run sql/0001_agent_memory.sql first.",
                  file=sys.stderr)
            return 2
        print(f"{'dim':<6} {'provider':<14} {'model':<32} {'base_url':<40} {'api_key_env':<20}")
        print("-" * 116)
        for r in rows:
            dim, provider, model, base_url, api_key_env = r
            print(f"{dim:<6} {provider:<14} {model:<32} "
                  f"{(base_url or ''):<40} {(api_key_env or ''):<20}")
        with conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM agent_memory.settings WHERE key = 'default_dim'"
            )
            row = cur.fetchone()
        if row:
            print(f"\ndefault_dim: {row[0]}")
        return 0
    finally:
        conn.close()


def cmd_model_set(args, parser) -> int:
    """Switch the default dim and/or override the model for that dim.

    Examples:
        hermes postgres model-set --dim 768
        hermes postgres model-set --dim 1024 --provider ollama_local --model bge-m3
        hermes postgres model-set --dim 1536 --provider openai --model text-embedding-3-small
    """
    if args.dim not in (768, 1024, 1536):
        print(f"Invalid --dim: {args.dim}. Use 768, 1024, or 1536.", file=sys.stderr)
        return 2
    conn = _conn()
    try:
        with conn.cursor() as cur:
            # Update the model registry row if --provider or --model given
            if args.provider or args.model or args.base_url or args.api_key_env is not None:
                cur.execute(
                    "UPDATE agent_memory.models SET "
                    "  provider = COALESCE(%s, provider), "
                    "  model = COALESCE(%s, model), "
                    "  base_url = COALESCE(%s, base_url), "
                    "  api_key_env = COALESCE(%s, api_key_env), "
                    "  updated_at = now() "
                    "WHERE dim = %s RETURNING provider, model, base_url, api_key_env",
                    (args.provider, args.model, args.base_url,
                     args.api_key_env, args.dim),
                )
                row = cur.fetchone()
                if row:
                    new_provider, new_model, new_base_url, new_api_key_env = row
                else:
                    print(f"No registry row for dim {args.dim}; create one via SQL first.",
                          file=sys.stderr)
                    return 2
            else:
                cur.execute(
                    "SELECT provider, model, base_url, api_key_env "
                    "FROM agent_memory.models WHERE dim = %s",
                    (args.dim,),
                )
                row = cur.fetchone()
                if not row:
                    print(f"No model registered for dim {args.dim} and no overrides given.",
                          file=sys.stderr)
                    return 2
                new_provider, new_model, new_base_url, new_api_key_env = row
            # Update default_dim
            cur.execute(
                "UPDATE agent_memory.settings SET value = %s::jsonb, updated_at = now() "
                "WHERE key = 'default_dim' RETURNING value",
                (str(args.dim),),
            )
        conn.commit()
        # Drop the per-dim embedder singleton so the next call rebuilds from SQL
        from plugins.memory.postgres.embedder import reset_embedder
        reset_embedder(args.dim)
        print(f"✓ default_dim set to {args.dim}")
        print(f"  model: provider={new_provider!r}, model={new_model!r}")
        print(f"  base_url={new_base_url!r} api_key_env={new_api_key_env!r}")
        print()
        print("Next steps:")
        print(f"  1. New writes go to vector_{args.dim} automatically.")
        print(f"  2. Run `hermes postgres backfill --dim {args.dim}` to populate")
        print(f"     the new dim for existing rows.")
        return 0
    finally:
        conn.close()


def cmd_backfill(args, parser) -> int:
    """Delegate to scripts/backfill_embeddings.py."""
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.normpath(os.path.join(here, "..", "scripts", "backfill_embeddings.py"))
    cmd = [sys.executable, script]
    if args.dry_run:
        cmd.append("--dry-run")
    if args.batch:
        cmd += ["--batch", str(args.batch)]
    if args.limit:
        cmd += ["--limit", str(args.limit)]
    if args.dim:
        cmd += ["--dim", str(args.dim)]
    print(f"running: {' '.join(cmd)}", file=sys.stderr)
    return subprocess.call(cmd)


def cmd_find_empty(args, parser) -> int:
    """List memory IDs whose target column is null or all-zero.

    With no --dim, lists per-dim counts of empty rows.
    With --dim, lists the actual IDs (id, content preview, source).
    """
    conn = _conn()
    try:
        with conn.cursor() as cur:
            if not _memory_table_exists(cur):
                print("agent_memory.memories does not exist.", file=sys.stderr)
                return 2
            if args.dim:
                col = f"vector_{args.dim}"
                if args.dim not in (768, 1024, 1536):
                    print(f"Invalid --dim: {args.dim}.", file=sys.stderr)
                    return 2
                cur.execute(
                    f"SELECT id, category, source, substring(content, 1, 80) "
                    f"FROM agent_memory.memories "
                    f"WHERE deleted_at IS NULL "
                    f"  AND ({col} IS NULL OR {col} = array_fill(0, ARRAY[%s])::vector) "
                    f"ORDER BY id LIMIT %s",
                    (args.dim, args.limit or 1000),
                )
                rows = cur.fetchall()
                print(f"empty rows in {col} ({len(rows)} of total):")
                for r in rows:
                    print(f"  id={r[0]:<6} cat={r[1] or '-':<20} "
                          f"src={r[2] or '-':<30} content={r[3]!r}")
            else:
                out = {}
                for d in (768, 1024, 1536):
                    cur.execute(
                        f"SELECT COUNT(*) FROM agent_memory.memories "
                        f"WHERE deleted_at IS NULL "
                        f"  AND (vector_{d} IS NULL OR vector_{d} = "
                        f"      array_fill(0, ARRAY[%s])::vector)",
                        (d,),
                    )
                    out[d] = (cur.fetchone() or [0])[0]
                print(json.dumps({"empty_per_dim": out}, indent=2))
        return 0
    finally:
        conn.close()


def cmd_find_missing(args, parser) -> int:
    """Find rows missing any/all vector columns.

    --all means: list rows that have ZERO non-zero vector columns.
    --dim N means: list rows that are missing specifically vector_N.
    """
    conn = _conn()
    try:
        with conn.cursor() as cur:
            if not _memory_table_exists(cur):
                print("agent_memory.memories does not exist.", file=sys.stderr)
                return 2
            if args.dim:
                col = f"vector_{args.dim}"
                if args.dim not in (768, 1024, 1536):
                    print(f"Invalid --dim: {args.dim}.", file=sys.stderr)
                    return 2
                cur.execute(
                    f"SELECT id, category, source, substring(content, 1, 80) "
                    f"FROM agent_memory.memories "
                    f"WHERE deleted_at IS NULL "
                    f"  AND ({col} IS NULL OR {col} = array_fill(0, ARRAY[%s])::vector) "
                    f"ORDER BY id LIMIT %s",
                    (args.dim, args.limit or 1000),
                )
                rows = cur.fetchall()
                print(f"rows missing {col} ({len(rows)}):")
                for r in rows:
                    print(f"  id={r[0]:<6} cat={r[1] or '-':<20} "
                          f"src={r[2] or '-':<30} content={r[3]!r}")
            elif args.all:
                cur.execute(
                    """
                    SELECT id, category, source, substring(content, 1, 80)
                    FROM agent_memory.memories
                    WHERE deleted_at IS NULL
                      AND (vector_768  IS NULL OR vector_768  = array_fill(0, ARRAY[768])::vector)
                      AND (vector_1024 IS NULL OR vector_1024 = array_fill(0, ARRAY[1024])::vector)
                      AND (vector_1536 IS NULL OR vector_1536 = array_fill(0, ARRAY[1536])::vector)
                    ORDER BY id LIMIT %s
                    """,
                    (args.limit or 1000,),
                )
                rows = cur.fetchall()
                print(f"rows with ZERO non-zero vector columns ({len(rows)}):")
                for r in rows:
                    print(f"  id={r[0]:<6} cat={r[1] or '-':<20} "
                          f"src={r[2] or '-':<30} content={r[3]!r}")
            else:
                cur.execute(
                    """
                    SELECT
                      COUNT(*) FILTER (WHERE vector_768  IS NULL OR vector_768  = array_fill(0, ARRAY[768])::vector)  AS miss_768,
                      COUNT(*) FILTER (WHERE vector_1024 IS NULL OR vector_1024 = array_fill(0, ARRAY[1024])::vector) AS miss_1024,
                      COUNT(*) FILTER (WHERE vector_1536 IS NULL OR vector_1536 = array_fill(0, ARRAY[1536])::vector) AS miss_1536,
                      COUNT(*) FILTER (
                        WHERE (vector_768  IS NULL OR vector_768  = array_fill(0, ARRAY[768])::vector)
                          AND (vector_1024 IS NULL OR vector_1024 = array_fill(0, ARRAY[1024])::vector)
                          AND (vector_1536 IS NULL OR vector_1536 = array_fill(0, ARRAY[1536])::vector)
                      ) AS miss_all,
                      COUNT(*) AS total
                    FROM agent_memory.memories WHERE deleted_at IS NULL
                    """
                )
                r = cur.fetchone()
                if r is None:
                    print("{}", file=sys.stderr)
                    return 1
                print(json.dumps({
                    "total_active": r[4],
                    "missing_768":  r[0],
                    "missing_1024": r[1],
                    "missing_1536": r[2],
                    "missing_all":  r[3],
                }, indent=2))
        return 0
    finally:
        conn.close()


def cmd_embed_text(args, parser) -> int:
    """Embed arbitrary text using the configured embedder for --dim.

    Prints the vector as JSON to stdout. Useful for sanity-checking
    the embedder and for ad-hoc semantic lookups.
    """
    from plugins.memory.postgres import get_embedder
    text = args.text
    if not text or not text.strip():
        print("Empty text; nothing to embed.", file=sys.stderr)
        return 2
    dim = args.dim if args.dim else 1024
    if dim not in (768, 1024, 1536):
        print(f"Invalid --dim: {dim}.", file=sys.stderr)
        return 2
    try:
        e = get_embedder(dim)
    except Exception as exc:
        print(f"Failed to load embedder for dim {dim}: {exc}", file=sys.stderr)
        return 1
    vec = e.embed(text)
    out = {
        "dim": e.dim,
        "provider": e.provider,
        "model": e.model,
        "text_preview": text[:80],
        "vector_len": len(vec),
        "vector": vec,
        "stats": e.stats(),
    }
    if args.no_vector:
        out.pop("vector")
    print(json.dumps(out, indent=2))
    return 0


# ── Argparse wiring ─────────────────────────────────────────────────────


def register_cli(subparser) -> None:
    """Entry point for the plugin CLI discovery."""
    p = subparser.add_parser(
        "postgres",
        help="PostgreSQL + pgvector memory plugin (canonical schema)",
    )
    subs = p.add_subparsers(dest="postgres_command")

    s_status = subs.add_parser("status", help="Show provider status, registry, and per-dim fill counts")
    s_status.set_defaults(func=cmd_status)

    s_ml = subs.add_parser("model-list", help="List per-dim model configs")
    s_ml.set_defaults(func=cmd_model_list)

    s_ms = subs.add_parser(
        "model-set",
        help="Switch the default dim and/or override the model for that dim",
    )
    s_ms.add_argument("--dim", type=int, required=True, choices=[768, 1024, 1536],
                      help="New default dim")
    s_ms.add_argument("--provider", help="Override the embedder provider for this dim")
    s_ms.add_argument("--model", help="Override the model name for this dim")
    s_ms.add_argument("--base-url", help="Override the embedder base URL for this dim")
    s_ms.add_argument("--api-key-env",
                      help="Env var name holding the API key (e.g. OPENAI_API_KEY). "
                           "Pass empty string to clear.")
    s_ms.set_defaults(func=cmd_model_set, api_key_env=None)

    s_bf = subs.add_parser("backfill", help="Run the backfill script (populate empty vector columns)")
    s_bf.add_argument("--dry-run", action="store_true",
                      help="Count rows that would be embedded; no writes.")
    s_bf.add_argument("--batch", type=int, help="Rows per embed batch (default: 32).")
    s_bf.add_argument("--limit", type=int, help="Stop after N rows per dim (0 = no limit).")
    s_bf.add_argument("--dim", type=int, choices=[768, 1024, 1536],
                      help="Backfill a specific dim only (default: all dims).")
    s_bf.set_defaults(func=cmd_backfill)

    s_fe = subs.add_parser(
        "find-empty",
        help="List memories with null/zero vector in a given dim (or summary per dim)",
    )
    s_fe.add_argument("--dim", type=int, choices=[768, 1024, 1536],
                      help="Inspect a specific dim. Without this, prints a per-dim summary.")
    s_fe.add_argument("--limit", type=int, default=1000, help="Max rows to list (default: 1000).")
    s_fe.set_defaults(func=cmd_find_empty)

    s_fm = subs.add_parser(
        "find-missing",
        help="Find rows missing any/all vector columns",
    )
    s_fm.add_argument("--dim", type=int, choices=[768, 1024, 1536],
                      help="List rows missing this specific dim.")
    s_fm.add_argument("--all", action="store_true",
                      help="List rows that have ZERO non-zero vector columns.")
    s_fm.add_argument("--limit", type=int, default=1000, help="Max rows to list (default: 1000).")
    s_fm.set_defaults(func=cmd_find_missing)

    s_et = subs.add_parser(
        "embed-text",
        help="Embed arbitrary text with the configured embedder for a given dim",
    )
    s_et.add_argument("text", help="Text to embed")
    s_et.add_argument("--dim", type=int, choices=[768, 1024, 1536],
                      help="Embed at this dim (default: 1024).")
    s_et.add_argument("--no-vector", action="store_true",
                      help="Print metadata only, omit the raw vector.")
    s_et.set_defaults(func=cmd_embed_text)

    p.set_defaults(func=lambda args, parser: p.print_help() or 1)
