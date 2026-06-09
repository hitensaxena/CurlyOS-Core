"""Embedding provider contract.

Shared across memory, knowledge, identity, and retrieval engines.
Default: bge-m3 1024-dim (pinned in HitenOS architecture).
"""
from __future__ import annotations

from typing import Any


class Embedder:
    """Abstract embedder — produces 1024-dim vectors from text.

    Implementations: LocalBgeM3, OpenAIAdapter, SentenceTransformerAdapter.
    """

    @property
    def dimension(self) -> int:
        return 1024

    @property
    def model_name(self) -> str:
        raise NotImplementedError

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts. Returns list of float vectors."""
        raise NotImplementedError

    async def embed_single(self, text: str) -> list[float]:
        """Convenience: embed one text."""
        results = await self.embed([text])
        return results[0]


class Reranker:
    """Cross-encoder reranker. Default: bge-reranker-v2-m3."""

    @property
    def model_name(self) -> str:
        raise NotImplementedError

    async def rerank(
        self,
        query: str,
        documents: list[str],
        top_k: int | None = None,
    ) -> list[tuple[int, float]]:
        """Rerank documents against query.

        Returns list of (original_index, score) sorted by score descending.
        """
        raise NotImplementedError
