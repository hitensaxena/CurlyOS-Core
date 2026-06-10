"""Concrete embedder and reranker implementations.

LocalBgeM3      — sentence-transformers BAAI/bge-m3 (1024-dim)
OpenAIAdapter   — OpenAI embeddings API fallback
FakeEmbedder    — zero vectors for testing (no model download)
HashEmbedder    — deterministic SHA-256 pseudo-vectors (NON-SEMANTIC; replay/CI)
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import threading
from typing import Any

from shared.embeddings import Embedder, Reranker

log = logging.getLogger(__name__)


class FakeEmbedder(Embedder):
    """Zero-vector embedder for tests — no model download required."""

    @property
    def dimension(self) -> int:
        return 1024

    @property
    def model_name(self) -> str:
        return "fake"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self.dimension for _ in texts]


def _hash_embed(text: str, dim: int) -> list[float]:
    """Deterministic, L2-normalised pseudo-embedding (lifted from the build
    repo's validated DeterministicHashEmbedder). Expands SHA-256(text||counter)
    into 32-bit words mapped to [-1, 1), then L2-normalises. Pure function of
    text — replaying a projection reproduces the embedding bit-for-bit."""
    seed = text.encode("utf-8")
    out: list[float] = []
    counter = 0
    while len(out) < dim:
        digest = hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        for j in range(0, len(digest), 4):
            if len(out) >= dim:
                break
            word = int.from_bytes(digest[j:j + 4], "big")
            out.append((word / 2_147_483_648.0) - 1.0)
        counter += 1
    norm = math.sqrt(sum(x * x for x in out)) or 1.0
    return [x / norm for x in out]


class HashEmbedder(Embedder):
    """Deterministic 1024-dim hash embedder — NON-SEMANTIC, never a production
    retrieval backend. Exists so consolidation/replay tests run anywhere with
    byte-identical vectors and no model download (the replay-determinism
    property the spec's exit criteria demand)."""

    @property
    def dimension(self) -> int:
        return 1024

    @property
    def model_name(self) -> str:
        return "deterministic-hash-1024"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [_hash_embed(t, self.dimension) for t in texts]


class LocalBgeM3(Embedder):
    """BAAI/bge-m3 via sentence-transformers. 1024-dim."""

    _load_lock: threading.Lock = threading.Lock()

    def __init__(self, device: str = "cpu"):
        self._device = device
        self._model = None

    @property
    def dimension(self) -> int:
        return 1024

    @property
    def model_name(self) -> str:
        return "BAAI/bge-m3"

    def _load(self):
        with self._load_lock:
            if self._model is None:
                from sentence_transformers import SentenceTransformer
                log.info("Loading %s on %s...", self.model_name, self._device)
                self._model = SentenceTransformer(self.model_name, device=self._device)
                log.info("Model loaded.")

    def _encode(self, texts: list[str]) -> list[list[float]]:
        self._load()
        vectors = self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [v.tolist() for v in vectors]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await asyncio.to_thread(self._encode, texts)


class OpenAIAdapter(Embedder):
    """OpenAI text-embedding-3-large adapter. Returns 1024-dim (truncated)."""

    def __init__(self, model: str = "text-embedding-3-large", api_key: str | None = None):
        self._model_name = model
        self._api_key = api_key
        self._client = None

    @property
    def dimension(self) -> int:
        return 1024

    @property
    def model_name(self) -> str:
        return self._model_name

    def _load(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(api_key=self._api_key)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self._load()
        resp = self._client.embeddings.create(input=texts, model=self._model_name, dimensions=self.dimension)
        return [d.embedding for d in resp.data]


class FakeReranker(Reranker):
    """Identity reranker for tests — returns original order."""

    @property
    def model_name(self) -> str:
        return "fake"

    async def rerank(self, query: str, documents: list[str], top_k: int | None = None) -> list[tuple[int, float]]:
        n = min(top_k or len(documents), len(documents))
        return [(i, 1.0 - i * 0.01) for i in range(n)]
