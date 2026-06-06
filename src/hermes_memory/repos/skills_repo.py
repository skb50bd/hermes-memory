"""Skills repository — hermes_skills.skills + skill_links.

Public surface (from SkillsTools.cs):
  - register(name, version, *, description, owner, tags) -> bool
  - search(query, *, top_k) -> list[Skill]
  - link(source, target, kind) -> bool
  - graph(root, *, max_hops) -> dict[str, list[str]]
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


LinkKind = Literal["depends_on", "supersedes", "related", "see_also"]


@dataclass
class Skill:
    name: str
    version: str
    description: str
    owner: str | None
    tags: tuple[str, ...]


class SkillsRepo:
    def register(
        self,
        name: str,
        version: str,
        *,
        description: str = "",
        owner: str | None = None,
        tags: list[str] | None = None,
    ) -> bool:
        if not name:
            raise ValueError("name must be non-empty")
        if not version:
            raise ValueError("version must be non-empty")
        return self._insert_skill(
            name, version, description, owner, list(tags or [])
        )

    def search(self, query: str, *, top_k: int = 20) -> list[Skill]:
        if not query.strip():
            return []
        return self._search(query, top_k=top_k)

    def link(self, source: str, target: str, kind: LinkKind) -> bool:
        if kind not in ("depends_on", "supersedes", "related", "see_also"):
            raise ValueError(f"invalid link kind: {kind}")
        if source == target:
            return False
        return self._insert_link(source, target, kind)

    def graph(self, root: str, *, max_hops: int = 2) -> dict[str, list[str]]:
        if max_hops < 1:
            raise ValueError("max_hops must be >= 1")
        return self._graph(root, max_hops)

    def _insert_skill(self, name, version, description, owner, tags) -> bool:
        raise NotImplementedError

    def _search(self, query, *, top_k) -> list[Skill]:
        raise NotImplementedError

    def _insert_link(self, source, target, kind) -> bool:
        raise NotImplementedError

    def _graph(self, root, max_hops) -> dict[str, list[str]]:
        raise NotImplementedError
