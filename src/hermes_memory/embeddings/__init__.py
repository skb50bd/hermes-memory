"""Embedder registry — pluggable embedder dispatch by dimension.

The default is bge-m3 (1024-dim) via local Ollama. Override per
installation via env vars:

  HERMES_EMBED_PROVIDER=openai|ollama_local|http
  HERMES_EMBED_BASE_URL=https://api.openai.com/v1
  HERMES_EMBED_MODEL=text-embedding-3-small
  HERMES_EMBED_API_KEY=...
  HERMES_EMBED_DIM=1536
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)


SUPPORTED_DIMS = (768, 1024, 1536)
DEFAULT_DIM = 1024


class EmbeddingError(Exception):
    """Raised when the embedder fails to produce a vector."""


class Embedder:
    """Base interface. Subclass for each provider."""

    def embed(self, text: str) -> list[float]:
        raise NotImplementedError

    @property
    def dim(self) -> int:
        raise NotImplementedError

    @property
    def model(self) -> str:
        raise NotImplementedError


class HttpEmbedder(Embedder):
    """OpenAI-compatible HTTP embedder (Ollama, OpenAI, vLLM, etc.)."""

    def __init__(
        self,
        base_url: str,
        model: str,
        dim: int,
        api_key: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        if dim not in SUPPORTED_DIMS:
            raise ValueError(
                f"unsupported dim {dim}; choose one of {SUPPORTED_DIMS}"
            )
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._dim = dim
        self._api_key = api_key
        self._client = httpx.Client(timeout=timeout)

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model(self) -> str:
        return self._model

    def embed(self, text: str) -> list[float]:
        if not text or not text.strip():
            # Empty text → zero vector (deterministic, no API call)
            return [0.0] * self._dim
        # OpenAI-compatible POST /v1/embeddings
        url = f"{self._base_url}/embeddings"
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            resp = self._client.post(
                url,
                headers=headers,
                json={"model": self._model, "input": text},
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise EmbeddingError(f"embedder POST failed: {e}") from e
        data = resp.json()
        try:
            vec = data["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError) as e:
            raise EmbeddingError(
                f"embedder response missing 'data[0].embedding': {e}"
            ) from e
        if len(vec) != self._dim:
            # The embedder returned a different dim than expected;
            # truncate or pad to keep the column constraint happy.
            vec = vec[: self._dim] if len(vec) > self._dim else vec + [0.0] * (self._dim - len(vec))
        return vec


class EmbedderRegistry:
    """Dispatch queries to the right embedder based on dim.

    The default v2 install ships bge-m3 at 1024-dim via Ollama
    (per the locked decision). Other dims (768, 1536) are supported
    but require explicit configuration.
    """

    def __init__(self, embedders: dict[int, Embedder]) -> None:
        self._embedders = embedders
        if DEFAULT_DIM not in embedders:
            raise ValueError(
                f"default dim {DEFAULT_DIM} must be in the registry"
            )

    @property
    def default_dim(self) -> int:
        return DEFAULT_DIM

    @classmethod
    def from_env(cls) -> EmbedderRegistry:
        provider = os.environ.get("HERMES_EMBED_PROVIDER", "ollama_local").strip()
        if provider == "ollama_local":
            base_url = os.environ.get(
                "HERMES_EMBED_BASE_URL", "http://10.49.0.52:11434/v1"
            )
            dim = int(os.environ.get("HERMES_EMBED_DIM", "1024"))
            # 768-dim uses nomic-embed-text-v2-moe, 1024 uses bge-m3
            model = os.environ.get(
                "HERMES_EMBED_MODEL",
                "bge-m3" if dim == 1024 else "nomic-embed-text-v2-moe",
            )
        elif provider == "openai":
            base_url = os.environ.get(
                "HERMES_EMBED_BASE_URL", "https://api.openai.com/v1"
            )
            dim = int(os.environ.get("HERMES_EMBED_DIM", "1536"))
            model = os.environ.get(
                "HERMES_EMBED_MODEL", "text-embedding-3-small"
            )
        elif provider == "http":
            base_url = os.environ["HERMES_EMBED_BASE_URL"]
            dim = int(os.environ["HERMES_EMBED_DIM"])
            model = os.environ["HERMES_EMBED_MODEL"]
        else:
            raise ValueError(
                f"unknown HERMES_EMBED_PROVIDER: {provider!r} "
                f"(expected ollama_local, openai, or http)"
            )
        api_key = os.environ.get("HERMES_EMBED_API_KEY")
        embedder = HttpEmbedder(
            base_url=base_url, model=model, dim=dim, api_key=api_key
        )
        return cls({dim: embedder})

    def embed(self, text: str, *, dim: int | None = None) -> list[float]:
        target = dim or DEFAULT_DIM
        emb = self._embedders.get(target)
        if emb is None:
            raise EmbeddingError(f"no embedder for dim={target}")
        return emb.embed(text)

    def get(self, dim: int) -> Embedder | None:
        return self._embedders.get(dim)
