"""Wiki repository — hermes_wiki.documents + document_links.

Public surface (from the C# WikiTools.cs):
  - create(slug, title, body_md, *, category, tags, metadata) -> int
  - read(slug) -> Document | None
  - link(source_slug, target_slug, *, context) -> bool
  - backlinks(target_slug) -> list[Document]
  - related(slug, *, max_hops) -> list[Document]
  - search(query, *, top_k) -> list[Document]
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass
class Document:
    id: int
    slug: str
    title: str
    body_md: str
    category: str | None
    metadata: dict[str, Any]
    tags: tuple[str, ...] = ()


class WikiRepo:
    """Base class. Subclasses implement the storage hooks."""

    def create(
        self,
        slug: str,
        title: str,
        body_md: str,
        *,
        category: str | None = None,
        tags: Sequence[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        if not slug or not slug.strip():
            raise ValueError("slug must be non-empty")
        if not title:
            raise ValueError("title must be non-empty")
        return self._insert_document(
            slug,
            title,
            body_md,
            category=category,
            tags=list(tags or []),
            metadata=metadata or {},
        )

    def read(self, slug: str) -> Document | None:
        return self._fetch_document(slug)

    def link(self, source_slug: str, target_slug: str, *, context: str | None = None) -> bool:
        if source_slug == target_slug:
            return False
        return self._insert_link(source_slug, target_slug, context)

    def backlinks(self, target_slug: str) -> list[Document]:
        return self._fetch_backlinks(target_slug)

    def related(self, slug: str, *, max_hops: int = 2) -> list[Document]:
        if max_hops < 1:
            raise ValueError("max_hops must be >= 1")
        return self._fetch_related(slug, max_hops)

    def search(self, query: str, *, top_k: int = 10) -> list[Document]:
        if not query.strip():
            return []
        return self._search(query, top_k=top_k)

    # -- hooks
    def _insert_document(self, slug, title, body_md, *, category, tags, metadata) -> int:
        raise NotImplementedError

    def _fetch_document(self, slug) -> Document | None:
        raise NotImplementedError

    def _insert_link(self, source_slug, target_slug, context) -> bool:
        raise NotImplementedError

    def _fetch_backlinks(self, target_slug) -> list[Document]:
        raise NotImplementedError

    def _fetch_related(self, slug, max_hops) -> list[Document]:
        raise NotImplementedError

    def _search(self, query, *, top_k) -> list[Document]:
        raise NotImplementedError
