"""Model policy — the configured model chains + an auto-failover client wrapper.

A primary model plus ordered backups: when a model 429s / errors / is down (very
common on OpenRouter :free tiers), the next one in the chain is tried. Chains are
env-overridable (comma-separated).

  CURLYOS_MODEL_CHAIN         main reasoning/chat chain (general)
  CURLYOS_CODING_MODEL_CHAIN  code-generation chain (defined; wired by consumers)
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

from shared import metrics

log = logging.getLogger("curlyos.models")

# Main chain: owl-alpha primary, then free backups.
GENERAL_CHAIN_DEFAULT = (
    "openrouter/owl-alpha,"
    "nex-agi/nex-n2-pro:free,"
    "nvidia/nemotron-3-ultra-550b-a55b:free"
)
# Coding chain — defined for consumers to opt into; not wired into the general path.
CODING_CHAIN_DEFAULT = (
    "poolside/laguna-m.1:free,"
    "openai/gpt-oss-120b:free,"
    "qwen/qwen3-coder:free"
)
# Task-tiered chains (see curlyos LLM routing audit):
#   AGENTIC — orchestration + agent runs (Azure Kimi by default)
#   DEEP    — heavy "thinking" cognition: reflection/meta/narrative (Azure gpt-oss-120b)
# Both are env-overridable; each tier's base_url/key come from CURLYOS_<TIER>_* env
# resolved in api_server._make_llm_client. Defaults match the deployed Azure models.
AGENTIC_CHAIN_DEFAULT = "kimi-k2.6"
DEEP_CHAIN_DEFAULT = "gpt-oss-120b"


def _parse(s: str | None) -> list[str]:
    return [m.strip() for m in (s or "").split(",") if m.strip()]


def general_chain() -> list[str]:
    return _parse(os.environ.get("CURLYOS_MODEL_CHAIN", GENERAL_CHAIN_DEFAULT))


def coding_chain() -> list[str]:
    return _parse(os.environ.get("CURLYOS_CODING_MODEL_CHAIN", CODING_CHAIN_DEFAULT))


def agentic_chain() -> list[str]:
    return _parse(os.environ.get("CURLYOS_AGENTIC_CHAIN", AGENTIC_CHAIN_DEFAULT))


def deep_chain() -> list[str]:
    return _parse(os.environ.get("CURLYOS_DEEP_CHAIN", DEEP_CHAIN_DEFAULT))


def primary_model() -> str:
    """The default single model — first of the general chain."""
    chain = general_chain()
    return chain[0] if chain else "openrouter/owl-alpha"


class _FallbackCompletions:
    """Drop-in for `client.chat.completions` that fails over across the chain.

    The `model=` passed by the caller is tried first; the configured chain
    supplies the backups (deduped). Any exception (429, timeout, API error)
    advances to the next model; if all fail, the last exception is raised.
    """

    def __init__(self, raw: Any, chain: list[str], tier: str = "general"):
        self._raw = raw
        self._chain = chain
        self._tier = tier

    async def create(self, *, model: str | None = None, **kwargs: Any) -> Any:
        order: list[str] = []
        for m in ([model] if model else []) + self._chain:
            if m and m not in order:
                order.append(m)
        if not order:
            raise ValueError("no model configured for completion")
        tier = self._tier
        last_exc: Exception | None = None
        t0 = time.time()
        for i, m in enumerate(order):
            try:
                resp = await self._raw.chat.completions.create(model=m, **kwargs)
                # Observability (since-boot, in-process): one call, its latency,
                # the model that actually answered, and whether we had to fail over.
                metrics.incr(f"llm.{tier}.calls")
                metrics.timing(f"llm.{tier}.latency", (time.time() - t0) * 1000)
                metrics.note(f"llm.{tier}.last_model", m)
                if i > 0:
                    metrics.incr(f"llm.{tier}.fallbacks")
                return resp
            except Exception as e:  # noqa: BLE001 — any failure → try the next model
                last_exc = e
                nxt = order[i + 1] if i + 1 < len(order) else None
                log.warning("model %s failed (%s)%s", m, type(e).__name__,
                            f"; falling back to {nxt}" if nxt else "; chain exhausted")
        metrics.incr(f"llm.{tier}.errors")
        metrics.note(f"llm.{tier}.last_error", f"{type(last_exc).__name__}: {last_exc}"[:200])
        raise last_exc  # type: ignore[misc]


class _FallbackChat:
    def __init__(self, raw: Any, chain: list[str], tier: str = "general"):
        self.completions = _FallbackCompletions(raw, chain, tier)


class FallbackClient:
    """Wraps an AsyncOpenAI-style client so `.chat.completions.create` fails over.

    Only `.chat.completions.create` is intercepted; everything else proxies to the
    underlying client. `tier` labels the metrics this client records (fast /
    agentic / deep).
    """

    def __init__(self, raw: Any, chain: list[str] | None = None, tier: str = "general"):
        self._raw = raw
        self.chat = _FallbackChat(raw, chain or general_chain(), tier)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)
