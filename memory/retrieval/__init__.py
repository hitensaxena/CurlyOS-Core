"""Memory retrieval — hybrid semantic + keyword + graph → cross-encoder rerank → context assembler.

Five-stage pipeline:
  1. Hybrid first-stage (parallel): pgvector HNSW (dense) + BM25 ParadeDB (sparse) + entity match
  2. Reciprocal Rank Fusion + recency boost
  3. Postgres recursive CTE k-hop graph expansion + cross-encoder rerank (bge-reranker-v2-m3)
  4. Agentic iterative loop (max 3 rounds)
  5. Context assembler — token-budgeted, tier-allocated, lost-in-the-middle mitigation

Modes: fast (default), deep (larger ef_search, k=2 hops, more rounds), divergent (MMR, recency-inverted, speculative graph)

See: ~/hitenos-architecture/02c-retrieval.md
"""
from __future__ import annotations

import asyncio
import logging
import math
import hashlib
from datetime import datetime, timezone
from typing import Any

from shared.types import (
    RetrievalRequest, RetrievalResult, RetrievedItem, RankWeights,
)

log = logging.getLogger("curlyos.retrieval")

_RRF_K = 60  # RRF constant

# ── Bi-temporal predicate ───────────────────────────────────────────────────


def _bitemporal_where(as_of: datetime | None = None) -> tuple[str, list]:
    """Generate the bi-temporal WHERE clause + params."""
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    return (
        "valid_from <= %s AND (valid_to IS NULL OR valid_to > %s)",
        [as_of, as_of],
    )


def _epistemic_filter_for_mode(mode: str) -> frozenset[str]:
    """Return the set of epistemic statuses to include for a given retrieval mode.

    `belief` (Hiten's held worldview/values) grounds "who he is", so it surfaces
    alongside `canonical` in normal recall — previously it was in NO mode and
    never recalled. `divergent` additionally pulls speculative tiers.
    """
    if mode == "divergent":
        return frozenset({"canonical", "belief", "hypothesis", "conjecture", "possible_world"})
    return frozenset({"canonical", "belief"})


# ── Stage 1: Hybrid first-stage ────────────────────────────────────────────


async def _dense_recall(
    pool: Any, embedder: Any, query: str, scope: str,
    k: int = 20, ef_search: int = 64, as_of: datetime | None = None,
    epistemic_filter: frozenset[str] = frozenset({"canonical"}),
) -> list[dict]:
    """pgvector HNSW dense recall on memories.embedding."""
    vec = await embedder.embed_single(query)
    bt_where, bt_params = _bitemporal_where(as_of)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            # Set ef_search for this query
            ef_search = max(10, min(int(ef_search), 400))
            await cur.execute(f"SET LOCAL hnsw.ef_search = {ef_search}")
            await cur.execute(
                f"SELECT id, statement, kind, valid_from, valid_to, source_episode_id, "
                f"epistemic_status, 1 - (embedding <=> %s::vector) AS score "
                f"FROM memories "
                f"WHERE scope = %s AND {bt_where} "
                f"AND epistemic_status = ANY(%s) "
                f"ORDER BY embedding <=> %s::vector LIMIT %s",
                [str(vec), scope, *bt_params,
                 list(epistemic_filter), str(vec), k],
            )
            rows = await cur.fetchall()
    return [
        {"id": r[0], "text": r[1], "tier": r[2],
         "valid_from": r[3], "valid_to": r[4],
         "source_episode_id": r[5], "epistemic_status": r[6],
         "score": float(r[7]), "signals": {"dense": float(r[7])}}
        for r in rows
    ]


async def _sparse_recall(
    pool: Any, query: str, scope: str,
    k: int = 20, as_of: datetime | None = None,
    epistemic_filter: frozenset[str] = frozenset({"canonical"}),
) -> list[dict]:
    """BM25 sparse recall via Postgres full-text search (tsvector + plainto_tsquery).
    Falls back to websearch_to_tsquery if plainto_tsquery returns no results."""
    bt_where, bt_params = _bitemporal_where(as_of)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            # Try plainto_tsquery first (less aggressive stemming)
            await cur.execute(
                f"SELECT id, statement, kind, valid_from, valid_to, source_episode_id, "
                f"epistemic_status, ts_rank(to_tsvector('english', statement), "
                f"plainto_tsquery('english', %s)) AS score "
                f"FROM memories "
                f"WHERE scope = %s AND {bt_where} "
                f"AND epistemic_status = ANY(%s) "
                f"AND to_tsvector('english', statement) @@ plainto_tsquery('english', %s) "
                f"ORDER BY score DESC LIMIT %s",
                [query, scope, *bt_params, list(epistemic_filter), query, k],
            )
            rows = await cur.fetchall()
            # Fallback to websearch_to_tsquery if no results
            if not rows:
                await cur.execute(
                    f"SELECT id, statement, kind, valid_from, valid_to, source_episode_id, "
                    f"epistemic_status, ts_rank(to_tsvector('english', statement), "
                    f"websearch_to_tsquery('english', %s)) AS score "
                    f"FROM memories "
                    f"WHERE scope = %s AND {bt_where} "
                    f"AND epistemic_status = ANY(%s) "
                    f"AND to_tsvector('english', statement) @@ websearch_to_tsquery('english', %s) "
                    f"ORDER BY score DESC LIMIT %s",
                    [query, scope, *bt_params, list(epistemic_filter), query, k],
                )
                rows = await cur.fetchall()
    return [
        {"id": r[0], "text": r[1], "tier": r[2],
         "valid_from": r[3], "valid_to": r[4],
         "source_episode_id": r[5], "epistemic_status": r[6],
         "score": float(r[7]), "signals": {"bm25": float(r[7])}}
        for r in rows if r[7] and float(r[7]) > 0
    ]


async def _entity_match(
    pool: Any, query: str, scope: str,
    k: int = 10, as_of: datetime | None = None,
) -> list[dict]:
    """Exact identity_facts lookup for deterministic self-model queries."""
    bt_where, bt_params = _bitemporal_where(as_of)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT id, predicate, object, confidence, valid_from, valid_to, "
                f"source_episode_id, epistemic_status "
                f"FROM identity_facts "
                f"WHERE scope = %s AND {bt_where} "
                f"AND (predicate ILIKE %s OR object ILIKE %s) "
                f"ORDER BY confidence DESC LIMIT %s",
                [scope, *bt_params, f"%{query}%", f"%{query}%", k],
            )
            rows = await cur.fetchall()
    return [
        {"id": r[0], "text": f"{r[1]} = {r[2]}", "tier": "identity",
         "valid_from": r[4], "valid_to": r[5],
         "source_episode_id": r[6], "epistemic_status": r[7],
         "score": float(r[3]), "signals": {"entity": float(r[3])}}
        for r in rows
    ]


# ── Stage 2: RRF fusion ────────────────────────────────────────────────────


def _rrf_fuse(
    candidates: list[dict],
    dense_ranked: list[dict],
    sparse_ranked: list[dict],
    entity_ranked: list[dict],
    recency_weight: float = 0.3,
    divergent: bool = False,
) -> list[dict]:
    """Reciprocal Rank Fusion combining dense + sparse + entity results.

    RRF score = Σ(1/(k + rank)) with k=60 for each signal channel.
    Recency boost: multiply by exp(-0.01 * age_in_days).
    For divergent mode, recency is inverted (older items boosted).
    """
    now = datetime.now(timezone.utc)
    rrf_k = _RRF_K

    def _build_rank_map(ranked: list[dict]) -> dict[str, int]:
        return {item["id"]: idx + 1 for idx, item in enumerate(ranked)}

    dense_ranks = _build_rank_map(dense_ranked)
    sparse_ranks = _build_rank_map(sparse_ranked)
    entity_ranks = _build_rank_map(entity_ranked)

    # Collect all unique candidate ids
    all_ids: set[str] = set()
    for c in candidates:
        all_ids.add(c["id"])

    # Group by id to merge signals
    by_id: dict[str, dict] = {}
    for c in candidates:
        cid = c["id"]
        if cid in by_id:
            by_id[cid]["signals"].update(c["signals"])
            by_id[cid]["score"] = max(by_id[cid]["score"], c["score"])
        else:
            by_id[cid] = {**c}

    # Compute RRF score for each candidate
    for cid, item in by_id.items():
        rrf_score = 0.0
        if cid in dense_ranks:
            rrf_score += 1.0 / (rrf_k + dense_ranks[cid])
        if cid in sparse_ranks:
            rrf_score += 1.0 / (rrf_k + sparse_ranks[cid])
        if cid in entity_ranks:
            rrf_score += 1.0 / (rrf_k + entity_ranks[cid])

        # Recency boost
        age_days = (now - item.get("valid_from", now)).total_seconds() / 86400.0
        recency_factor = math.exp(-0.01 * age_days)
        if divergent:
            recency_factor = 1.0 - recency_factor + 0.01  # small floor to avoid zero

        item["fused_score"] = rrf_score * (1.0 + recency_weight * recency_factor)
        if math.isnan(item["fused_score"]) or math.isinf(item["fused_score"]):
            item["fused_score"] = 0.0

    return sorted(by_id.values(), key=lambda x: -x["fused_score"])


# ── Stage 3: Graph expansion ───────────────────────────────────────────────


async def _graph_expand(
    pool: Any,
    seed_ids: list[str],
    scope: str,
    k_hops: int = 1,
    as_of: datetime | None = None,
    epistemic_filter: frozenset[str] = frozenset({"canonical"}),
) -> list[dict]:
    """Postgres recursive CTE k-hop graph expansion on knowledge_edges.

    Resolves seed memory IDs → source_episode_ids → entity IDs, then
    traverses knowledge_edges (src_entity_id/dst_entity_id) up to k_hops,
    and returns memories linked to reached entities.

    Filters edges and entities by bi-temporal validity.
    """
    if not seed_ids:
        return []

    bt_where, bt_params = _bitemporal_where(as_of)

    # Build per-table bi-temporal predicates (avoids fragile f-string replace)
    edge_bt = bt_where.replace("valid_from", "e.valid_from").replace("valid_to", "e.valid_to")
    entity_bt = bt_where.replace("valid_from", "ke.valid_from").replace("valid_to", "ke.valid_to")
    mem_bt = bt_where  # memories table uses default column names

    query = f"""
    WITH RECURSIVE
    -- Step 1: Resolve seed memory IDs to their source episodes
    seed_episodes AS (
        SELECT DISTINCT m.source_episode_id AS episode_id
        FROM memories m
        WHERE m.id = ANY(%s)
    ),
    -- Step 2: Find entity IDs linked to those episodes
    seed_entities AS (
        SELECT DISTINCT ke.id AS entity_id
        FROM knowledge_entities ke
        INNER JOIN seed_episodes se ON ke.source_episode_id = se.episode_id
        WHERE ({entity_bt})
        AND ke.epistemic_status = ANY(%s)
    ),
    -- Step 3: k-hop traversal on knowledge_edges
    graph_walk AS (
        -- Base: outgoing edges from seed entities
        SELECT
            e.dst_entity_id AS entity_id,
            1 AS depth
        FROM knowledge_edges e
        INNER JOIN seed_entities se ON e.src_entity_id = se.entity_id
        WHERE ({edge_bt})

        UNION

        -- Recursive: follow outgoing edges up to k hops
        SELECT
            e.dst_entity_id,
            gw.depth + 1
        FROM knowledge_edges e
        INNER JOIN graph_walk gw ON e.src_entity_id = gw.entity_id
        WHERE gw.depth < %s
        AND ({edge_bt})
    ),
    -- Step 4: Collect all reached entities (seed + walked)
    all_reached_entities AS (
        SELECT entity_id FROM seed_entities
        UNION
        SELECT entity_id FROM graph_walk
    ),
    -- Step 5: Find episodes linked to reached entities
    reached_episodes AS (
        SELECT DISTINCT ke.source_episode_id AS episode_id
        FROM knowledge_entities ke
        INNER JOIN all_reached_entities re ON ke.id = re.entity_id
        WHERE ({entity_bt})
    )
    -- Step 6: Fetch memories from those episodes
    SELECT DISTINCT ON (m.id)
        m.id,
        m.statement,
        m.kind,
        m.valid_from,
        m.valid_to,
        m.source_episode_id,
        m.epistemic_status,
        1.0 / (1.0 + COALESCE(gw.depth, 0))::float AS score
    FROM reached_episodes re
    JOIN memories m ON m.source_episode_id = re.episode_id
    LEFT JOIN graph_walk gw ON gw.entity_id IN (
        SELECT ke2.id FROM knowledge_entities ke2
        WHERE ke2.source_episode_id = re.episode_id
    )
    WHERE m.scope = %s
    AND {mem_bt}
    AND m.epistemic_status = ANY(%s)
    ORDER BY m.id, score DESC
    """

    eph_list = list(epistemic_filter)
    params: list = []
    # Step 1: seed memory IDs (ANY)
    params.append(seed_ids)
    # Step 2: entity bi-temporal + epistemic filter
    params.extend(bt_params)
    params.append(eph_list)
    # Step 3 base: edge bi-temporal
    params.extend(bt_params)
    # Step 3 recursive: k_hops
    params.append(k_hops)
    # Step 3 recursive: edge bi-temporal
    params.extend(bt_params)
    # Step 5: entity bi-temporal for reached_episodes
    params.extend(bt_params)
    # Step 6: scope
    params.append(scope)
    # Step 6: memory bi-temporal
    params.extend(bt_params)
    # Step 6: epistemic filter
    params.append(eph_list)

    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, params)
                rows = await cur.fetchall()
    except Exception as exc:
        log.warning("Graph expansion failed: %s", exc)
        return []

    return [
        {"id": r[0], "text": r[1], "tier": r[2],
         "valid_from": r[3], "valid_to": r[4],
         "source_episode_id": r[5], "epistemic_status": r[6],
         "score": float(r[7]) if r[7] else 0.0,
         "signals": {"graph": float(r[7]) if r[7] else 0.0}}
        for r in rows
    ]


# ── Stage 3b: Cross-encoder rerank ─────────────────────────────────────────


async def _rerank(
    reranker: Any, query: str, candidates: list[dict], top_k: int = 30,
) -> list[dict]:
    """Cross-encoder rerank over merged candidate set."""
    if not candidates or reranker is None:
        return candidates[:top_k]

    docs = [c["text"] for c in candidates]
    ranked = await reranker.rerank(query, docs, top_k=top_k)

    reranked = []
    for orig_idx, score in ranked:
        c = candidates[orig_idx]
        c["score"] = score
        reranked.append(c)
    return reranked


# ── Stage 4: Agentic iterative loop ────────────────────────────────────────


def _detect_coverage_gap(query: str, items: list[dict]) -> str | None:
    """Detect if top results don't cover the query. Returns a follow-up query or None.

    Simple heuristic: if top-3 results all have fused_score < 0.1 or if
    the query contains multiple question aspects not addressed by results.
    """
    if not items:
        return query + " details"

    top_score = items[0].get("fused_score", items[0].get("score", 0.0))
    if top_score < 0.05:
        # Very weak results — try a broader query
        words = query.split()
        if len(words) > 3:
            return " ".join(words[:3])  # shorter query
        return query + " overview"

    # Check if query has multiple aspects (e.g. "what is X and how does Y")
    query_lower = query.lower()
    aspect_markers = [" and ", " or ", " vs ", " versus ", " compared ", " difference "]
    has_multiple_aspects = sum(1 for m in aspect_markers if m in query_lower) >= 1
    if has_multiple_aspects and len(items) < 3:
        # Extract the second aspect as a follow-up
        for marker in aspect_markers:
            if marker in query_lower:
                parts = query_lower.split(marker, 1)
                if len(parts) == 2 and parts[1].strip():
                    return parts[1].strip().capitalize()
    return None


# ── Stage 5: Context assembler ─────────────────────────────────────────────

def _assemble_context(
    items: list[dict], budget: int = 4000,
) -> tuple[list[dict], int, bool]:
    """Token-budgeted context packing.

    Tier allocation: 40% semantic, 30% episodic, 20% graph, 10% working.
    Lost-in-the-middle: put highest-value at start and end.
    Deduplicate by statement_key (text prefix).
    """
    # Rough token estimate: ~4 chars per token
    def est_tokens(text: str) -> int:
        return len(text) // 4 + 1

    # Budget allocation per tier
    tier_budgets: dict[str, int] = {
        "semantic": int(budget * 0.40),
        "episodic": int(budget * 0.30),
        "graph": int(budget * 0.20),
        "working": int(budget * 0.10),
    }
    tier_used: dict[str, int] = {t: 0 for t in tier_budgets}

    chosen: list[dict] = []
    used = 0
    seen_texts: set[str] = set()

    for item in items:
        text = item.get("text", "")
        tier = item.get("tier", "semantic")

        # Dedup by text prefix
        text_sig = text[:100].lower().strip()
        if text_sig in seen_texts:
            continue
        seen_texts.add(text_sig)

        cost = est_tokens(text) + 5  # +5 for provenance tag
        tb = tier_budgets.get(tier, tier_budgets["semantic"])
        if tier_used.get(tier, 0) + cost > tb:
            continue  # skip but don't break — other tiers may have budget
        if used + cost > budget:
            break

        chosen.append(item)
        tier_used[tier] = tier_used.get(tier, 0) + cost
        used += cost

    truncated = any(
        tier_used.get(t, 0) >= tier_budgets.get(t, 0) for t in tier_budgets
    ) or (len(chosen) < len(items))

    # Lost-in-the-middle mitigation: highest-score at edges
    if len(chosen) > 2:
        # Sort interior by score descending, then place best at start and end
        chosen.sort(key=lambda x: -(x.get("fused_score", x.get("score", 0.0))))
        ordered = [chosen[0]] + chosen[2:] + [chosen[1]]
    elif len(chosen) > 1:
        ordered = [chosen[0], chosen[-1]]
    else:
        ordered = chosen
    return ordered, used, truncated


# ── MMR diversity (for divergent mode) ─────────────────────────────────────


def _mmr_diversify(
    items: list[dict],
    lambda_param: float = 0.5,
    top_k: int = 30,
) -> list[dict]:
    """Maximal Marginal Relevance diversification for divergent mode.

    Balances relevance with diversity. lambda_param controls the tradeoff:
    1.0 = pure relevance, 0.0 = pure diversity.
    """
    if not items:
        return items

    selected: list[dict] = []
    remaining = list(items)

    # First pick: highest score
    remaining.sort(key=lambda x: -(x.get("fused_score", x.get("score", 0.0))))
    selected.append(remaining.pop(0))

    while remaining and len(selected) < top_k:
        best_score = -float("inf")
        best_idx = 0

        for i, candidate in enumerate(remaining):
            relevance = candidate.get("fused_score", candidate.get("score", 0.0))
            # Max similarity to already selected items (using text overlap as proxy)
            max_sim = 0.0
            cand_text = set(candidate.get("text", "").lower().split())
            for sel in selected:
                sel_text = set(sel.get("text", "").lower().split())
                if cand_text and sel_text:
                    overlap = len(cand_text & sel_text) / max(len(cand_text), 1)
                    max_sim = max(max_sim, overlap)

            mmr = lambda_param * relevance - (1.0 - lambda_param) * max_sim
            if mmr > best_score:
                best_score = mmr
                best_idx = i

        selected.append(remaining.pop(best_idx))

    return selected


# ── Main entry point ───────────────────────────────────────────────────────


async def retrieve(
    request: RetrievalRequest,
    pool: Any,
    embedder: Any,
    reranker: Any = None,
    redis: Any = None,
) -> RetrievalResult:
    """Retrieve relevant memories for a query.

    Pipeline: hybrid → fuse → graph expand → rerank → iterative loop → assemble.
    Supports modes: fast, deep, divergent.
    """
    mode = request.mode
    divergent = mode == "divergent"
    deep = mode == "deep"

    ef_search = 128 if deep else 64
    k = 50 if deep else 20
    graph_hops = 2 if deep else 1
    max_rounds = 3 if deep else 1

    # Epistemic filter based on mode
    epistemic_filter = _epistemic_filter_for_mode(mode)

    # Check if embeddings exist
    has_embeddings = False
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT 1 FROM memories WHERE scope = %s AND embedding IS NOT NULL LIMIT 1",
                    [request.scope],
                )
                has_embeddings = await cur.fetchone() is not None
    except Exception:
        pass

    # Stage 1: Hybrid first-stage (dense + sparse + entity in parallel)
    dense_task = None
    if has_embeddings:
        dense_task = asyncio.create_task(
            _dense_recall(
                pool, embedder, request.query, request.scope, k=k,
                ef_search=ef_search, as_of=request.as_of,
                epistemic_filter=epistemic_filter,
            )
        )
    sparse_task = asyncio.create_task(
        _sparse_recall(
            pool, request.query, request.scope, k=k,
            as_of=request.as_of, epistemic_filter=epistemic_filter,
        )
    )
    entity_task = asyncio.create_task(
        _entity_match(
            pool, request.query, request.scope, as_of=request.as_of,
        )
    )

    dense = await dense_task if dense_task else []
    sparse = await sparse_task
    entity = await entity_task

    # Stage 2: RRF fusion with proper per-signal ranks
    candidates = _rrf_fuse(
        dense + sparse + entity,
        dense_ranked=dense,
        sparse_ranked=sparse,
        entity_ranked=entity,
        divergent=divergent,
    )

    # Stage 3: Graph expansion from top seed nodes
    graph_items: list[dict] = []
    graph_skipped = False
    seed_ids = [c["id"] for c in candidates[:5]]
    try:
        graph_items = await _graph_expand(
            pool, seed_ids, request.scope,
            k_hops=graph_hops, as_of=request.as_of,
            epistemic_filter=epistemic_filter,
        )
    except Exception as exc:
        log.warning("Graph expansion skipped: %s", exc)
        graph_skipped = True

    # Merge graph items into candidates
    all_candidates = candidates + graph_items
    # Re-fuse with graph signal
    if graph_items:
        all_candidates = _rrf_fuse(
            all_candidates,
            dense_ranked=dense,
            sparse_ranked=sparse,
            entity_ranked=entity,
            divergent=divergent,
        )

    # Stage 3b: Cross-encoder rerank (top-50 → top-30)
    reranked = await _rerank(reranker, request.query, all_candidates, top_k=50)

    # Stage 4: Agentic iterative loop
    rounds_done = 1
    current_query = request.query
    all_retrieved: dict[str, dict] = {item["id"]: item for item in reranked}

    for round_num in range(1, max_rounds + 1):
        current_items = sorted(
            all_retrieved.values(),
            key=lambda x: -(x.get("fused_score", x.get("score", 0.0))),
        )
        follow_up = _detect_coverage_gap(current_query, current_items[:10])
        if follow_up is None:
            break  # coverage is sufficient

        rounds_done = round_num + 1
        log.info("Retrieval round %d: follow-up query = %r", rounds_done, follow_up)

        # Run a lightweight retrieval for the follow-up
        follow_dense: list[dict] = []
        if has_embeddings:
            follow_dense = await _dense_recall(
                pool, embedder, follow_up, request.scope,
                k=10, ef_search=ef_search, as_of=request.as_of,
                epistemic_filter=epistemic_filter,
            )
        follow_sparse = await _sparse_recall(
            pool, follow_up, request.scope, k=10,
            as_of=request.as_of, epistemic_filter=epistemic_filter,
        )
        follow_fused = _rrf_fuse(
            follow_dense + follow_sparse,
            dense_ranked=follow_dense,
            sparse_ranked=follow_sparse,
            entity_ranked=[],
            divergent=divergent,
        )
        for item in follow_fused:
            if item["id"] not in all_retrieved:
                all_retrieved[item["id"]] = item

    # Collect all unique items
    final_items = sorted(
        all_retrieved.values(),
        key=lambda x: -(x.get("fused_score", x.get("score", 0.0))),
    )

    # Divergent mode: apply MMR diversification
    if divergent:
        final_items = _mmr_diversify(final_items, lambda_param=0.5, top_k=30)

    # Stage 5: Context assembler
    assembled, used_tokens, truncated = _assemble_context(
        final_items, budget=request.token_budget,
    )

    # Build result items
    result_items = [
        RetrievedItem(
            id=item["id"],
            tier=item.get("tier", "semantic"),
            text=item.get("text", ""),
            score=item.get("fused_score", item.get("score", 0.0)),
            valid_from=item.get("valid_from", datetime.now(timezone.utc)),
            valid_to=item.get("valid_to"),
            source_episode_id=item.get("source_episode_id", ""),
            signals=item.get("signals", {}),
            epistemic_status=item.get("epistemic_status", "canonical"),
            simulated=item.get("epistemic_status") == "possible_world",
        )
        for item in assembled
    ]

    cache_key = f"cache:retr:{request.scope}:{hashlib.sha256(request.query.encode()).hexdigest()[:16]}"

    return RetrievalResult(
        items=result_items,
        used_tokens=used_tokens,
        rounds=rounds_done,
        truncated=truncated,
        cache_key=cache_key,
        graph_skipped=graph_skipped,
        reranked=reranker is not None,
    )
