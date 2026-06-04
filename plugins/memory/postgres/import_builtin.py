"""Import Hermes built-in memory files (MEMORY.md / USER.md) into PostgreSQL.

One-shot migration. Reads the markdown files, splits by delimiter,
and inserts each entry as a memory row with appropriate categorization.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from plugins.memory.postgres import _PostgresClient, get_pg_mem_db_conn_str
from plugins.memory.postgres.embedder import get_embedder, SUPPORTED_DIMS

logger = logging.getLogger(__name__)

# Hermes built-in memory delimiter
ENTRY_DELIMITER = "\n---\n"


def _read_file(path: Path) -> List[str]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    entries = [e.strip() for e in text.split(ENTRY_DELIMITER)]
    return [e for e in entries if e]


def _categorize_entry(text: str, target: str) -> str:
    """Heuristic categorization based on content patterns."""
    text_lower = text.lower()
    if target == "user":
        if any(k in text_lower for k in ("prefers", "likes", "dislikes", "favorite", "hates")):
            return "user.preference"
        if any(k in text_lower for k in ("name", "role", "job", "works at", "timezone")):
            return "user.profile"
        return "user.profile"
    # target == "memory"
    if any(k in text_lower for k in ("os:", "linux", "macos", "windows", "ubuntu", "debian")):
        return "environment"
    if any(k in text_lower for k in ("project", "repo", "repository", "uses ", "stack")):
        return "project.convention"
    if any(k in text_lower for k in ("test", "pytest", "jest", "cargo test", "dotnet test")):
        return "workflow"
    if any(k in text_lower for k in ("skill", "convention", "pattern", "always", "never")):
        return "project.convention"
    return "fact"


def import_builtin_memory(
    memory_dir: Optional[Path] = None,
    dry_run: bool = False,
) -> dict:
    """Import MEMORY.md and USER.md into PostgreSQL memory store.

    Returns dict with counts and any errors.
    """
    if memory_dir is None:
        memory_dir = Path.home() / ".hermes" / "memory"

    memory_entries = _read_file(memory_dir / "MEMORY.md")
    user_entries = _read_file(memory_dir / "USER.md")

    client = _PostgresClient()
    default_dim = client.default_dim
    embedder = get_embedder(default_dim)

    imported = 0
    skipped = 0
    errors = []

    def _import_batch(entries: List[str], target: str) -> None:
        nonlocal imported, skipped, errors
        for entry in entries:
            # Skip blocked/placeholder entries
            if entry.startswith("[BLOCKED:"):
                skipped += 1
                continue
            # Skip very short entries
            if len(entry) < 10:
                skipped += 1
                continue
            category = _categorize_entry(entry, target)
            tags = ["imported", "builtin", target]
            try:
                if not dry_run:
                    embedding = embedder.embed(entry)
                    client.add_memory(
                        content=entry,
                        category=category,
                        tags=tags,
                        metadata={
                            "imported_at": datetime.now(timezone.utc).isoformat(),
                            "source": f"builtin_{target}",
                            "original_target": target,
                        },
                    )
                imported += 1
            except Exception as e:
                errors.append(str(e))
                logger.warning("Failed to import memory entry: %s", e)

    _import_batch(memory_entries, "memory")
    _import_batch(user_entries, "user")

    return {
        "imported": imported,
        "skipped": skipped,
        "errors": errors,
        "memory_entries": len(memory_entries),
        "user_entries": len(user_entries),
    }


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Import built-in memory into PostgreSQL")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be imported")
    parser.add_argument("--memory-dir", type=Path, help="Path to memory directory")
    args = parser.parse_args()

    result = import_builtin_memory(
        memory_dir=args.memory_dir,
        dry_run=args.dry_run,
    )
    print(f"Memory entries: {result['memory_entries']}")
    print(f"User entries: {result['user_entries']}")
    print(f"Imported: {result['imported']}")
    print(f"Skipped: {result['skipped']}")
    if result['errors']:
        print(f"Errors: {len(result['errors'])}")
        for e in result['errors'][:5]:
            print(f"  - {e}")


if __name__ == "__main__":
    main()
