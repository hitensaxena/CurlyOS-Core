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


def _detect_device() -> str:
    """Pick the torch device for bge-m3.

    Default is CPU. We deliberately do NOT auto-select Apple MPS: measured on
    this box, bge-m3 on MPS recompiles its kernels per input shape, so the
    sporadic varied-length queries the recall path issues cost 200-800ms each
    (vs a stable ~50ms on CPU's Accelerate/AMX path) — ~5x slower overall. MPS
    only wins on large, repeated, same-shape batches. A real CUDA GPU has no
    such pathology, so it's auto-selected. Set CURLYOS_EMBED_DEVICE=mps|cpu|cuda
    to force a device (e.g. mps for a dedicated bulk re-embed job).
    """
    import os
    forced = os.environ.get("CURLYOS_EMBED_DEVICE", "").strip().lower()
    if forced:
        return forced
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


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

    def __init__(self, device: str = "auto"):
        self._device = _detect_device() if device == "auto" else device
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


class CachingEmbedder(Embedder):
    """Wraps a real embedder with an exact-text memo cache.

    A single /api/recall embeds the query several times — once in the dense
    first-stage, again to re-score the candidate pool, and once per agentic
    follow-up round. On a CPU/MPS box each embed is ~50-150ms, so wrapping the
    shared embedder per request collapses those to one real model call.
    Unbounded by design: scope it to one request (short-lived) or to a small
    set of repeated texts, not to a long-running process.
    """

    def __init__(self, inner: Embedder):
        self._inner = inner
        self._cache: dict[str, list[float]] = {}

    @property
    def dimension(self) -> int:
        return self._inner.dimension

    @property
    def model_name(self) -> str:
        return self._inner.model_name

    async def embed(self, texts: list[str]) -> list[list[float]]:
        missing = [t for t in texts if t not in self._cache]
        if missing:
            # de-dup the misses before the (expensive) real call
            uniq = list(dict.fromkeys(missing))
            vecs = await self._inner.embed(uniq)
            for t, v in zip(uniq, vecs):
                self._cache[t] = v
        return [self._cache[t] for t in texts]


class HttpEmbedder(Embedder):
    """Calls an out-of-process embedding sidecar over HTTP.

    Used to run bge-m3 on the Apple Neural Engine (Core ML) in a separate
    process — measured ~18ms/query + ~840MB RSS there vs ~200ms + ~1.3GB
    in-process on CPU, with cos 0.99999 to the production embeddings. Keeps
    this API process small (the 1024-dim contract is unchanged). Stdlib-only
    (urllib) so it adds no dependency to the core venv.

    Sidecar contract: POST {url}/embed {"texts": [...]} -> {"vectors": [[...]]}.
    """

    def __init__(self, url: str, model_name: str = "bge-m3-coreml-ane", timeout: float = 30.0):
        self._url = url.rstrip("/") + "/embed"
        self._model_name = model_name
        self._timeout = timeout

    @property
    def dimension(self) -> int:
        return 1024

    @property
    def model_name(self) -> str:
        return self._model_name

    def _post(self, texts: list[str]) -> list[list[float]]:
        import json
        import urllib.request

        body = json.dumps({"texts": texts}).encode("utf-8")
        req = urllib.request.Request(
            self._url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return payload["vectors"]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return await asyncio.to_thread(self._post, texts)


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
