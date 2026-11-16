"""Recall application service.

Owns the retrieval orchestration that used to live in `/api/recall`: cache
lookup, hybrid retrieval, cosine re-scoring, ranking blend, archive-summary
promotion, per-episode diversification and response shaping.

The HTTP route supplies the pool, embedder, Redis client and runtime settings;
it keeps only transport concerns. The service does not close a Redis client it
did not create.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("curlyos.memory.recall")


@dataclass(frozen=True)
class RecallSettings:
    cache_enabled: bool = True
    cache_ttl: int = 120
    fast_followup: bool = False
    max_per_episode: int = 2
    summary_boost: float = 0.03
    chunk_penalty: float = 0.05
    blend_cosine: float = 0.65
    blend_fused: float = 0.25
    blend_recency: float = 0.10
    blend_half_life_days: float = 30.0
    working_set_boost: float = 0.08
    lexical_boost: float = 0.0


def recall_cache_key(
    query: str, scope: str, mode: str, k: int, generation: str, settings: RecallSettings,
) -> str:
    digest = hashlib.sha256(query.encode()).hexdigest()[:16]
    return (
        f"cache:recall:{scope}:{generation}:{mode}:{k}:{settings.max_per_episode}:"
        f"{settings.summary_boost}:{settings.chunk_penalty}:"
        f"{settings.blend_cosine}:{settings.blend_fused}:"
        f"{settings.blend_recency}:{settings.blend_half_life_days}:"
        f"{settings.working_set_boost}:{settings.lexical_boost}:{digest}"
    )


async def recall_query(
    *,
    query: str,
    scope: str,
    mode: str,
    k: int,
    pool: Any,
    embedder: Any,
    redis: Any = None,
    generation: str = "",
    settings: RecallSettings | None = None,
) -> dict:
    """Run one recall and return the HTTP-ready payload."""
    from memory.retrieval import (
        blend_rank_scores, conversation_key, diversify_by_episode, is_archive_summary,
        load_summary_index, lookup_summary, retrieve,
    )
    from shared import metrics
    from shared.embeddings.implementations import CachingEmbedder, FakeReranker
    from shared.types import RetrievalRequest, RetrievedItem

    settings = settings or RecallSettings()
    t_start = time.monotonic()
    metrics.incr("recall.requests")

    cache_key = None
    if redis is not None:
        cache_key = recall_cache_key(query, scope, mode, k, generation, settings)
        try:
            hit = await redis.get(cache_key)
            if hit is not None:
                payload = hit.decode() if isinstance(hit, (bytes, bytearray)) else hit
                cached = json.loads(payload)
                cached["cached"] = True
                cached["timings"] = {"cached": True}
                metrics.incr("recall.cache_hits")
                metrics.timing("recall.latency_cached", (time.monotonic() - t_start) * 1000)
                return cached
        except Exception:
            log.debug("recall cache read failed", exc_info=True)

    metrics.incr("recall.cache_misses")
    try:
        # Per-request memo so the query is embedded once across the dense stage,
        # the re-score pass below, and any agentic follow-up rounds.
        emb = CachingEmbedder(embedder)
        # Large token_budget so retrieve() returns its full candidate pool — its
        # assembler otherwise truncates by budget and can drop dense-strong items
        # that RRF rank-fusion ranked low. We re-rank the pool ourselves below.
        result = await retrieve(
            RetrievalRequest(query=query, scope=scope, mode=mode, token_budget=50000),
            pool=pool, embedder=emb, reranker=FakeReranker(),
            fast_followup=settings.fast_followup,
        )
        cand = result.items
        stage_timings: dict[str, float] = dict(result.timings)
        for _name, _ms in stage_timings.items():
            metrics.timing(f"recall.stage.{_name}", _ms)
        _t_post = time.monotonic()

        sims: dict[str, float] = {}
        blend: dict[str, float] = {}

        def rank_of(it: Any) -> float:
            """Blend of cosine, fused score and recency, with archive demotion."""
            sc = blend.get(it.id, it.score or 0.0)
            if (it.text or "").startswith("# archives/chatgpt/"):
                sc -= settings.chunk_penalty
            return sc

        def shown_score(it: Any) -> float:
            boost = (
                settings.summary_boost
                if is_archive_summary(getattr(it, "text", "") or "")
                else 0.0
            )
            return rank_of(it) + boost

        if cand:
            _t_embed = time.monotonic()
            qv = (await emb.embed([query]))[0]
            qlit = "[" + ",".join(repr(float(x)) for x in qv) + "]"
            stage_timings["query_embed"] = round((time.monotonic() - _t_embed) * 1000, 2)

            _t_score = time.monotonic()
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT id, 1 - (embedding <=> %s::vector) FROM memories "
                        "WHERE id = ANY(%s) AND embedding IS NOT NULL",
                        (qlit, [it.id for it in cand]),
                    )
                    for rid, sim in await cur.fetchall():
                        sims[rid] = float(sim)
            stage_timings["cosine_rescore"] = round((time.monotonic() - _t_score) * 1000, 2)

            _t_blend = time.monotonic()
            blend = blend_rank_scores(
                sims,
                {it.id: float(it.score or 0.0) for it in cand},
                {it.id: it.valid_from for it in cand},
                w_cosine=settings.blend_cosine,
                w_fused=settings.blend_fused,
                w_recency=settings.blend_recency,
                half_life_days=settings.blend_half_life_days,
            )
            stage_timings["blend"] = round((time.monotonic() - _t_blend) * 1000, 2)

            # Terse stored facts embed below long passages that merely discuss
            # the same topic. A small exact-term correction lifts them without
            # overpowering semantic ranking.
            _t_lex = time.monotonic()
            if settings.lexical_boost and cand:
                from shared.text import overlap_score, terms

                query_terms = terms([query], limit=10)
                for it in cand:
                    overlap = overlap_score(query_terms, it.text or "")
                    if overlap:
                        blend[it.id] = min(
                            1.0,
                            blend.get(it.id, 0.0) + settings.lexical_boost * overlap,
                        )
            stage_timings["lexical"] = round((time.monotonic() - _t_lex) * 1000, 2)

            # Working memory is a bounded boost for candidates the query
            # already retrieved; it never injects unrelated pinned rows.
            _t_ws = time.monotonic()
            try:
                from cognition.working_set import working_set_boost

                ws = await working_set_boost(pool, scope)
            except Exception:
                log.debug("working-set boost unavailable", exc_info=True)
                ws = {}
            if ws:
                for it in cand:
                    salience = ws.get(it.id)
                    if salience:
                        blend[it.id] = min(
                            1.0,
                            blend.get(it.id, 0.0) + settings.working_set_boost * salience,
                        )
            stage_timings["working_set"] = round((time.monotonic() - _t_ws) * 1000, 2)
            cand = sorted(cand, key=lambda it: -rank_of(it))

            def key_of(it: Any) -> str:
                return (
                    conversation_key(getattr(it, "text", "") or "")
                    or (getattr(it, "source_episode_id", "") or it.id)
                )

            _t_sum = time.monotonic()
            try:
                summary_index = await load_summary_index(pool, scope)
            except Exception:
                log.debug("summary index unavailable", exc_info=True)
                summary_index = {}
            stage_timings["summary_index"] = round((time.monotonic() - _t_sum) * 1000, 2)

            best_chunk: dict[str, float] = {}
            for it in cand[:12]:
                ck = key_of(it)
                if ck.startswith(("conv:", "doc:", "site:")):
                    best_chunk[ck] = max(best_chunk.get(ck, 0.0), rank_of(it))
            if best_chunk:
                known = {it.id for it in cand}
                for ck, sc in best_chunk.items():
                    summary = lookup_summary(summary_index, ck)
                    if not summary or summary["id"] in known:
                        continue
                    cand.append(RetrievedItem(
                        id=summary["id"], tier=summary["tier"] or "semantic",
                        text=summary["text"], score=sc + settings.summary_boost,
                        valid_from=summary["valid_from"], valid_to=None,
                        source_episode_id=summary["source_episode_id"] or "",
                        signals={"promoted_from_chunk": True},
                        epistemic_status=summary["epistemic_status"] or "canonical",
                    ))

            _t_div = time.monotonic()
            cand = diversify_by_episode(
                cand, rank_of,
                max_per_episode=settings.max_per_episode,
                summary_boost=settings.summary_boost,
                key_of=key_of,
            )
            stage_timings["diversify"] = round((time.monotonic() - _t_div) * 1000, 2)

        items = [
            {"id": it.id, "text": it.text[:200],
             "score": round(shown_score(it), 4),
             "tier": it.tier, "epistemic_status": it.epistemic_status}
            for it in cand[:k]
        ]
        stage_timings["postprocess"] = round((time.monotonic() - _t_post) * 1000, 2)
        stage_timings["total"] = round((time.monotonic() - t_start) * 1000, 2)
        out = {"results": items, "count": len(items), "timings": stage_timings}

        if redis is not None and cache_key is not None:
            try:
                # Cache the payload, not the timings: a cache hit is not a stage run.
                await redis.set(
                    cache_key,
                    json.dumps({"results": items, "count": len(items)}),
                    ex=settings.cache_ttl,
                )
            except Exception:
                pass
        metrics.timing("recall.latency", (time.monotonic() - t_start) * 1000)
        return out
    except Exception as e:
        metrics.incr("recall.errors")
        log.exception("recall failed query=%r", query[:80])
        return {"error": str(e), "results": [], "count": 0}
