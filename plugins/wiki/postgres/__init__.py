"""PostgreSQL wiki plugin for Hermes Agent.

Large-document notebook with:
- Chunking + per-chunk embeddings
- Document versioning
- Auto-link suggestions
- Hybrid search (doc-level + chunk-level)
- Source provenance
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

import psycopg2
import psycopg2.pool
from psycopg2.extensions import make_dsn

logger = logging.getLogger(__name__)

_POOL = None
_POOL_LOCK = threading.Lock()

# Chunking config
_DEFAULT_CHUNK_SIZE = 2000  # chars
_DEFAULT_CHUNK_OVERLAP = 200


def _get_pool():
    global _POOL
    if _POOL is not None:
        return _POOL
    with _POOL_LOCK:
        if _POOL is None:
            dsn = os.environ.get("PG_MEM_DB_CONN_STR", "").strip()
            if not dsn:
                raise RuntimeError("PG_MEM_DB_CONN_STR not set")
            _POOL = psycopg2.pool.ThreadedConnectionPool(0, 2, dsn)
        return _POOL


@contextmanager
def _cursor(*, commit: bool = False) -> Iterator[Any]:
    pool = _get_pool()
    conn = pool.getconn()
    cur = None
    try:
        conn.autocommit = not commit
        cur = conn.cursor()
        yield cur
        if commit:
            conn.commit()
    except Exception:
        if commit:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        pool.putconn(conn, close=False)


# ── Chunking ────────────────────────────────────────────────────────────

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


def _extract_headings(text: str) -> List[Tuple[int, str, int]]:
    """Return list of (level, heading, char_offset)."""
    return [
        (len(m.group(1)), m.group(2).strip(), m.start())
        for m in _HEADING_RE.finditer(text)
    ]


def _chunk_document(
    text: str,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
    overlap: int = _DEFAULT_CHUNK_OVERLAP,
) -> List[Dict]:
    """Split document into overlapping chunks with heading context."""
    headings = _extract_headings(text)
    chunks = []
    pos = 0
    ordinal = 0

    while pos < len(text):
        end = min(pos + chunk_size, len(text))
        # Extend to next newline if possible
        if end < len(text):
            nl = text.find("\n", end)
            if nl != -1:
                end = nl + 1

        chunk_text = text[pos:end].strip()
        if not chunk_text:
            pos = end
            continue

        # Find current heading path
        heading_path = []
        for level, heading, hpos in headings:
            if hpos <= pos:
                # Trim to current level
                heading_path = heading_path[:level - 1]
                heading_path.append(heading)
            else:
                break

        chunks.append({
            "ordinal": ordinal,
            "heading_path": " > ".join(heading_path) if heading_path else None,
            "anchor": _slugify(heading_path[-1]) if heading_path else None,
            "char_start": pos,
            "char_end": end,
            "content": chunk_text,
        })
        ordinal += 1
        pos = end - overlap if end < len(text) else end

    return chunks


def _slugify(text: str) -> str:
    return re.sub(r"[^\w\s-]", "", text).strip().lower().replace(" ", "-")


# ── Wiki operations ─────────────────────────────────────────────────────

def ingest_document(
    slug: str,
    title: str,
    body_md: str,
    *,
    category: Optional[str] = None,
    tags: Optional[List[str]] = None,
    source_uri: Optional[str] = None,
    source_mime: Optional[str] = None,
    created_by: Optional[str] = None,
    embedder=None,
) -> Dict[str, Any]:
    """Ingest a document: create doc, version, chunks, embeddings."""
    chunks = _chunk_document(body_md)

    with _cursor(commit=True) as cur:
        # Upsert document
        checksum = hashlib.sha256(body_md.encode()).hexdigest()[:16]
        cur.execute(
            """
            INSERT INTO hermes_wiki.documents
            (slug, title, body_md, category, tags, metadata, source_uri, source_mime, source_checksum, imported_at, updated_at)
            VALUES (%s, %s, %s, %s::ltree, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (slug) DO UPDATE SET
                title = EXCLUDED.title,
                body_md = EXCLUDED.body_md,
                category = EXCLUDED.category,
                tags = EXCLUDED.tags,
                metadata = EXCLUDED.metadata,
                source_uri = EXCLUDED.source_uri,
                source_mime = EXCLUDED.source_mime,
                source_checksum = EXCLUDED.source_checksum,
                imported_at = EXCLUDED.imported_at,
                updated_at = EXCLUDED.updated_at
            RETURNING id
            """,
            (slug, title, body_md, category, tags or [], json.dumps({}),
             source_uri, source_mime, checksum, datetime.now(timezone.utc), datetime.now(timezone.utc)),
        )
        doc_id = cur.fetchone()[0]

        # Create version
        cur.execute(
            """
            INSERT INTO hermes_wiki.document_versions
            (document_id, version, body_md, created_by, created_at)
            VALUES (%s, COALESCE((SELECT MAX(version) FROM hermes_wiki.document_versions WHERE document_id = %s), 0) + 1, %s, %s, %s)
            RETURNING id, version
            """,
            (doc_id, doc_id, body_md, created_by, datetime.now(timezone.utc)),
        )
        version_id, version_num = cur.fetchone()

        # Delete old chunks for this doc
        cur.execute(
            "DELETE FROM hermes_wiki.document_chunks WHERE document_id = %s",
            (doc_id,),
        )

        # Insert chunks with embeddings
        if embedder:
            for chunk in chunks:
                embedding = embedder.embed(chunk["content"])
                dim = len(embedding)
                col = f"vector_{dim}"
                cur.execute(
                    f"""
                    INSERT INTO hermes_wiki.document_chunks
                    (document_id, version_id, ordinal, heading_path, anchor, char_start, char_end, content, {col}, token_count)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s)
                    """,
                    (doc_id, version_id, chunk["ordinal"], chunk["heading_path"],
                     chunk["anchor"], chunk["char_start"], chunk["char_end"],
                     chunk["content"], embedding, len(chunk["content"].split())),
                )
        else:
            for chunk in chunks:
                cur.execute(
                    """
                    INSERT INTO hermes_wiki.document_chunks
                    (document_id, version_id, ordinal, heading_path, anchor, char_start, char_end, content, token_count)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (doc_id, version_id, chunk["ordinal"], chunk["heading_path"],
                     chunk["anchor"], chunk["char_start"], chunk["char_end"],
                     chunk["content"], len(chunk["content"].split())),
                )

    return {
        "document_id": doc_id,
        "version_id": version_id,
        "version": version_num,
        "chunks": len(chunks),
    }


def search_chunks(query: str, top_k: int = 10) -> List[Dict]:
    """Search document chunks by FTS."""
    with _cursor() as cur:
        cur.execute(
            """
            SELECT
                c.id, c.document_id, c.ordinal, c.heading_path, c.content,
                d.slug, d.title,
                ts_rank(c.content_tsv, plainto_tsquery('english', %s)) AS rank
            FROM hermes_wiki.document_chunks c
            JOIN hermes_wiki.documents d ON d.id = c.document_id
            WHERE c.content_tsv @@ plainto_tsquery('english', %s)
            ORDER BY rank DESC
            LIMIT %s
            """,
            (query, query, top_k),
        )
        rows = cur.fetchall()
    return [
        {
            "chunk_id": r[0],
            "document_id": r[1],
            "ordinal": r[2],
            "heading_path": r[3],
            "content": r[4],
            "slug": r[5],
            "title": r[6],
            "rank": float(r[7]) if r[7] else 0.0,
        }
        for r in rows
    ]


_STOP_WORDS = frozenset({
    "this", "that", "with", "have", "from", "they", "will", "would", "there",
    "their", "what", "said", "each", "which", "does", "could", "should",
    "about", "above", "after", "again", "against", "all", "and", "any",
    "are", "because", "been", "before", "being", "below", "between",
    "both", "but", "can", "did", "down", "during", "for", "had", "has",
    "her", "here", "hers", "him", "his", "how", "into", "its", "may",
    "more", "most", "not", "now", "off", "once", "only", "other", "our",
    "out", "over", "own", "same", "she", "some", "such", "than", "them",
    "then", "these", "those", "too", "under", "until", "very", "was",
    "were", "when", "where", "who", "whom", "why", "your",
})


def _extract_search_terms(text: str, max_terms: int = 5) -> str:
    """Extract key search terms from text for FTS query.

    Removes stop words, keeps nouns/verbs, builds an OR query.
    """
    words = re.findall(r"\b[a-zA-Z]{4,}\b", text.lower())
    filtered = [w for w in words if w not in _STOP_WORDS]
    seen: set = set()
    terms: List[str] = []
    for w in filtered:
        if w not in seen:
            seen.add(w)
            terms.append(w)
        if len(terms) >= max_terms:
            break
    return " | ".join(terms) if terms else text[:50]


def suggest_links(doc_id: int, top_k: int = 5, min_confidence: float = 0.6, embedder=None) -> List[Dict]:
    """Suggest links to other documents based on chunk similarity.
    
    Uses vector similarity when embedder is provided, falls back to FTS otherwise.
    FTS confidence is scaled to be comparable with vector similarity (0-1 range).
    """
    with _cursor() as cur:
        # Get doc chunks
        cur.execute(
            "SELECT id, content FROM hermes_wiki.document_chunks WHERE document_id = %s",
            (doc_id,),
        )
        chunks = cur.fetchall()

        suggestions = []
        for chunk_id, content in chunks:
            if embedder:
                # Vector similarity search
                embedding = embedder.embed(content)
                dim = len(embedding)
                col = f"vector_{dim}"
                cur.execute(
                    f"""
                    SELECT DISTINCT d.id, d.slug, d.title,
                        1 - (c.{col} <=> %s::vector) AS similarity
                    FROM hermes_wiki.document_chunks c
                    JOIN hermes_wiki.documents d ON d.id = c.document_id
                    WHERE c.document_id != %s
                      AND c.{col} IS NOT NULL
                    ORDER BY similarity DESC
                    LIMIT %s
                    """,
                    (embedding, doc_id, top_k),
                )
                for target_id, target_slug, target_title, similarity in cur.fetchall():
                    suggestions.append({
                        "source_doc_id": doc_id,
                        "target_doc_id": target_id,
                        "target_slug": target_slug,
                        "target_title": target_title,
                        "kind": "related",
                        "confidence": float(similarity) if similarity else 0.0,
                        "context": content[:200],
                    })
            else:
                # FTS fallback — use extracted key terms instead of full content
                query = _extract_search_terms(content)
                cur.execute(
                    """
                    SELECT DISTINCT d.id, d.slug, d.title,
                        ts_rank(c.content_tsv, to_tsquery('english', %s)) AS rank
                    FROM hermes_wiki.document_chunks c
                    JOIN hermes_wiki.documents d ON d.id = c.document_id
                    WHERE c.document_id != %s
                      AND c.content_tsv @@ to_tsquery('english', %s)
                    ORDER BY rank DESC
                    LIMIT %s
                    """,
                    (query, doc_id, query, top_k),
                )
                for target_id, target_slug, target_title, rank in cur.fetchall():
                    # Scale FTS rank to 0-1 range (typical raw ranks are 0.0-0.1)
                    scaled_conf = min(float(rank) * 10.0, 1.0) if rank else 0.5
                    suggestions.append({
                        "source_doc_id": doc_id,
                        "target_doc_id": target_id,
                        "target_slug": target_slug,
                        "target_title": target_title,
                        "kind": "related",
                        "confidence": scaled_conf,
                        "context": content[:200],
                    })

    # Deduplicate by target, keep highest confidence
    seen: Dict[int, Dict] = {}
    for s in suggestions:
        tid = s["target_doc_id"]
        if tid not in seen or s["confidence"] > seen[tid]["confidence"]:
            seen[tid] = s

    deduped = [s for s in seen.values() if s["confidence"] >= min_confidence]
    deduped.sort(key=lambda x: x["confidence"], reverse=True)
    return deduped[:top_k]


def accept_link(source_doc_id: int, target_doc_id: int, kind: str = "related") -> bool:
    """Accept a suggested link or create an explicit one."""
    with _cursor(commit=True) as cur:
        # Insert into document_links
        cur.execute(
            """
            INSERT INTO hermes_wiki.document_links (source_id, target_id, context)
            VALUES (%s, %s, %s)
            ON CONFLICT (source_id, target_id) DO NOTHING
            """,
            (source_doc_id, target_doc_id, kind),
        )
        # Update candidate status
        cur.execute(
            """
            UPDATE hermes_wiki.link_candidates
            SET status = 'accepted'
            WHERE source_doc_id = %s AND target_doc_id = %s AND kind = %s
            """,
            (source_doc_id, target_doc_id, kind),
        )
        return True


# ── Tool schemas ────────────────────────────────────────────────────────

WIKI_INGEST_SCHEMA = {
    "name": "wiki_ingest",
    "description": "Ingest a large document into the wiki with chunking and embeddings.",
    "parameters": {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "Unique document slug."},
            "title": {"type": "string", "description": "Document title."},
            "body": {"type": "string", "description": "Markdown body text."},
            "category": {"type": "string", "description": "Category path, e.g. 'docs.architecture'"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["slug", "title", "body"],
    },
}

WIKI_SEARCH_SCHEMA = {
    "name": "wiki_search",
    "description": "Search wiki document chunks.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "top_k": {"type": "integer", "description": "Max results (default: 10)"},
        },
        "required": ["query"],
    },
}

WIKI_LINK_SCHEMA = {
    "name": "wiki_link",
    "description": "Create a link between two wiki documents.",
    "parameters": {
        "type": "object",
        "properties": {
            "source_slug": {"type": "string"},
            "target_slug": {"type": "string"},
            "kind": {"type": "string", "default": "related"},
        },
        "required": ["source_slug", "target_slug"],
    },
}


def register(ctx) -> None:
    """Register wiki tools."""
    # Tool registration would go here when Hermes supports non-memory plugins
    pass
