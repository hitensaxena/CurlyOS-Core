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
import re
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

from shared.types import (
    RetrievalRequest, RetrievalResult, RetrievedItem,
)

log = logging.getLogger("curlyos.retrieval")

_RRF_K = 60  # RRF constant

# Weight of the graph channel against dense/sparse/entity in RRF. Graph hits are
# a different kind of evidence — "this memory is one hop from a node you hit" —
# so they contribute, but at half the weight of direct semantic match.
GRAPH_RRF_WEIGHT = 0.5

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
            # Use the precomputed `search_tsv` column (maintained by the
            # trg_memories_tsv trigger as to_tsvector('english', statement)) so
            # this hits the idx_memories_tsv GIN index. Computing to_tsvector()
            # inline instead forced a parallel seq scan + per-row tsvector over
            # all ~21k rows (~0.8s each, twice with the fallback ≈ 1.6s).
            # Try plainto_tsquery first (less aggressive stemming)
            await cur.execute(
                f"SELECT id, statement, kind, valid_from, valid_to, source_episode_id, "
                f"epistemic_status, ts_rank(search_tsv, "
                f"plainto_tsquery('english', %s)) AS score "
                f"FROM memories "
                f"WHERE scope = %s AND {bt_where} "
                f"AND epistemic_status = ANY(%s) "
                f"AND search_tsv @@ plainto_tsquery('english', %s) "
                f"ORDER BY score DESC LIMIT %s",
                [query, scope, *bt_params, list(epistemic_filter), query, k],
            )
            rows = await cur.fetchall()
            # Fallback to websearch_to_tsquery if no results
            if not rows:
                await cur.execute(
                    f"SELECT id, statement, kind, valid_from, valid_to, source_episode_id, "
                    f"epistemic_status, ts_rank(search_tsv, "
                    f"websearch_to_tsquery('english', %s)) AS score "
                    f"FROM memories "
                    f"WHERE scope = %s AND {bt_where} "
                    f"AND epistemic_status = ANY(%s) "
                    f"AND search_tsv @@ websearch_to_tsquery('english', %s) "
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
    graph_ranked: list[dict] | None = None,
    graph_weight: float = GRAPH_RRF_WEIGHT,
) -> list[dict]:
    """Reciprocal Rank Fusion combining dense + sparse + entity (+ graph).

    RRF score = Σ(1/(k + rank)) with k=60 for each signal channel.
    The graph channel is scaled by `graph_weight`: without it graph items enter
    with no rank at all and can only ever duplicate what the other channels
    already found (measured 2026-11-11: 0.67% of graph items reached the top
    ten).
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
    graph_ranks = _build_rank_map(graph_ranked or [])

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
        if cid in graph_ranks:
            rrf_score += graph_weight * (1.0 / (rrf_k + graph_ranks[cid]))

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

# How many entities one hop may contribute. MEASURED 2026-11-11: unbounded
# 1-hop expansion returned 449 items per query on average (2,687 for
# "neti neti meditation") and 0.67% of them survived into the top ten — the
# graph was flooding the candidate pool, not informing it. Neighbours are
# ranked by how specific the link is before this cut.
GRAPH_ENTITIES_PER_HOP = 400

# Memories one expansion may return. 400 entities × the ~6 episodes each is
# still thousands of rows, so the list is cut as well as the walk.
GRAPH_MEMORIES_MAX = 120


async def _load_alias_pairs(pool: Any) -> tuple[list[str], list[str]]:
    """(raw_id, canonical_id) for every entity whose spelling was absorbed.

    Tier 1 recorded the duplicate spellings in knowledge_entity_aliases but left
    the edges pointing where they were, so the graph's centre is still split
    four ways (Hiten Saxena 4,649 edges, User 2,150, CRAZYMAGE 501, assistant
    553). Folding endpoints through these pairs fixes traversal without
    rewriting a single row.
    """
    sql = """
      SELECT DISTINCT ON (en.id) en.id, a.entity_id
      FROM knowledge_entities en
      JOIN knowledge_entity_aliases a
        ON a.alias_norm = en.name_norm AND a.entity_id <> en.id
      WHERE en.valid_to IS NULL
      ORDER BY en.id, a.entity_id
    """
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql)
                rows = await cur.fetchall()
    except Exception as exc:
        # A missing alias table must not take recall down: fall back to raw ids.
        log.warning("Alias lookup failed, traversing raw entity ids: %s", exc)
        return [], []
    return [r[0] for r in rows], [r[1] for r in rows]


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

    Entity ids are folded through knowledge_entity_aliases first (see
    _load_alias_pairs), so one person or project is one node no matter how the
    extractor spelled it in each episode.

    Filters edges and entities by bi-temporal validity.
    """
    if not seed_ids:
        return []

    bt_where, bt_params = _bitemporal_where(as_of)

    # Build per-table bi-temporal predicates (avoids fragile f-string replace)
    edge_bt = bt_where.replace("valid_from", "e.valid_from").replace("valid_to", "e.valid_to")
    entity_bt = bt_where.replace("valid_from", "ke.valid_from").replace("valid_to", "ke.valid_to")
    mem_bt = bt_where  # memories table uses default column names

    raw_ids, canon_ids = await _load_alias_pairs(pool)

    query = f"""
    WITH
    -- Step 0: alias resolution table (empty arrays = no aliases known)
    alias_map AS (
        SELECT * FROM unnest(%s::text[], %s::text[]) AS t(raw_id, canon_id)
    ),
    -- Canonical view of the edges. Self-loops created by folding are dropped:
    -- "User works_on CurlyOS" and "Hiten Saxena works_on CurlyOS" are one fact.
    edge_c AS (
        SELECT coalesce(a1.canon_id, e.src_entity_id) AS src,
               coalesce(a2.canon_id, e.dst_entity_id) AS dst,
               e.rel_norm
        FROM knowledge_edges e
        LEFT JOIN alias_map a1 ON a1.raw_id = e.src_entity_id
        LEFT JOIN alias_map a2 ON a2.raw_id = e.dst_entity_id
        WHERE ({edge_bt})
    ),
    -- How informative a predicate is. `related_to` accounts for roughly a
    -- fifth of the mapped edges and says nothing, so a link through it is the
    -- last thing expansion should spend its budget on; the specific predicates
    -- cost nothing. Unmapped phrases sit in between — the extractor chose them
    -- because the generic ones did not fit.
    edge_scored AS (
        SELECT src, dst, rel_norm,
               CASE
                   WHEN rel_norm = 'related_to' THEN 3.0
                   WHEN rel_norm IS NULL THEN 1.0
                   ELSE 0.0
               END AS rel_cost
        FROM edge_c
    ),
    -- Step 1: Resolve seed memory IDs to their source episodes
    seed_episodes AS (
        SELECT DISTINCT m.source_episode_id AS episode_id
        FROM memories m
        WHERE m.id = ANY(%s)
    ),
    -- Step 2: Find entity IDs linked to those episodes (canonical ids)
    seed_entities AS (
        SELECT DISTINCT coalesce(am.canon_id, ke.id) AS entity_id
        FROM knowledge_entities ke
        INNER JOIN seed_episodes se ON ke.source_episode_id = se.episode_id
        LEFT JOIN alias_map am ON am.raw_id = ke.id
        WHERE ({entity_bt})
        AND ke.epistemic_status = ANY(%s)
    ),
    -- Step 3: traversal on the canonical edge view, damped by node degree.
    -- A raw 1-hop from the Hiten Saxena hub reaches thousands of entities, so
    -- expansion has to be bounded twice: rank neighbours by how specific the
    -- connection is (log degree at both ends), and keep only the best
    -- GRAPH_ENTITIES_PER_HOP of them per depth.
    deg AS (
        SELECT node, count(*) AS d FROM (
            SELECT src AS node FROM edge_c
            UNION ALL
            SELECT dst AS node FROM edge_c
        ) g GROUP BY 1
    ),
    hop1_ranked AS (
        SELECT e.dst AS entity_id,
               min(ln(1 + coalesce(ds.d, 1)) + ln(1 + coalesce(dd.d, 1))
                   + e.rel_cost) AS cost
        FROM edge_scored e
        INNER JOIN seed_entities se ON e.src = se.entity_id
        LEFT JOIN deg ds ON ds.node = e.src
        LEFT JOIN deg dd ON dd.node = e.dst
        WHERE e.src <> e.dst
        GROUP BY 1
    ),
    hop1 AS (
        SELECT entity_id FROM hop1_ranked ORDER BY cost LIMIT %s
    ),
    hop2_ranked AS (
        SELECT e.dst AS entity_id,
               min(ln(1 + coalesce(ds.d, 1)) + ln(1 + coalesce(dd.d, 1))
                   + e.rel_cost) AS cost
        FROM edge_scored e
        INNER JOIN hop1 h ON e.src = h.entity_id
        LEFT JOIN deg ds ON ds.node = e.src
        LEFT JOIN deg dd ON dd.node = e.dst
        WHERE e.src <> e.dst
          AND e.dst NOT IN (SELECT entity_id FROM hop1)
        GROUP BY 1
    ),
    hop2 AS (
        SELECT entity_id FROM hop2_ranked ORDER BY cost LIMIT %s
    ),
    graph_walk AS (
        SELECT entity_id, 1 AS depth FROM hop1
        UNION ALL
        SELECT entity_id, 2 AS depth FROM hop2
    ),
    -- Step 4: Collect all reached entities (seed + walked)
    all_reached_entities AS (
        SELECT entity_id FROM seed_entities
        UNION
        SELECT entity_id FROM graph_walk
    ),
    -- Step 5: Fold entities to their canonical node once, then attribute
    -- episodes to the shallowest reached node. Doing this as a join instead of
    -- a correlated subquery in the final SELECT matters: the old shape ran an
    -- index scan per output row (55k loops on a 5-seed query).
    reached AS (
        SELECT DISTINCT coalesce(am2.canon_id, ke.id) AS canon_id,
               ke.source_episode_id AS episode_id
        FROM knowledge_entities ke
        LEFT JOIN alias_map am2 ON am2.raw_id = ke.id
        WHERE ({entity_bt})
          AND ke.source_episode_id IS NOT NULL
    ),
    reached_episodes AS (
        SELECT r.episode_id, min(COALESCE(gw.depth, 0)) AS depth
        FROM reached r
        INNER JOIN all_reached_entities re ON re.entity_id = r.canon_id
        LEFT JOIN graph_walk gw ON gw.entity_id = r.canon_id
        -- The seed memories' own episodes are already represented by the seed
        -- hits; re-adding them spends the budget on memories the other channels
        -- could have found. MEASURED 2026-11-11: 285 of 619 expansion items
        -- came from a seed episode, and 100 percent of them for
        -- "figma design system work".
        WHERE NOT EXISTS (
            SELECT 1 FROM seed_episodes se WHERE se.episode_id = r.episode_id
        )
        GROUP BY r.episode_id
    )
    -- Step 6: Fetch memories from those episodes
    SELECT * FROM (
        SELECT DISTINCT ON (m.id)
            m.id,
            m.statement,
            m.kind,
            m.valid_from,
            m.valid_to,
            m.source_episode_id,
            m.epistemic_status,
            1.0 / (1.0 + re.depth)::float AS score
        FROM reached_episodes re
        JOIN memories m ON m.source_episode_id = re.episode_id
        WHERE m.scope = %s
        AND {mem_bt}
        AND m.epistemic_status = ANY(%s)
        ORDER BY m.id, score DESC
    ) picked
    ORDER BY score DESC
    LIMIT %s
    """

    eph_list = list(epistemic_filter)
    params: list = []
    # Step 0: alias resolution arrays
    params.append(raw_ids)
    params.append(canon_ids)
    # Canonical edge view: edge bi-temporal
    params.extend(bt_params)
    # Step 1: seed memory IDs (ANY)
    params.append(seed_ids)
    # Step 2: entity bi-temporal + epistemic filter
    params.extend(bt_params)
    params.append(eph_list)
    # Step 3: entities kept per hop (0 disables the second hop on fast mode)
    params.append(GRAPH_ENTITIES_PER_HOP)
    params.append(GRAPH_ENTITIES_PER_HOP if k_hops >= 2 else 0)
    # Step 5: entity bi-temporal for reached_episodes
    params.extend(bt_params)
    # Step 6: scope
    params.append(scope)
    # Step 6: memory bi-temporal
    params.extend(bt_params)
    # Step 6: epistemic filter
    params.append(eph_list)
    # Step 6: keep only the strongest graph hits — episodes reached through a
    # well-connected entity multiply, so the entity cap alone does not bound
    # the memory list.
    params.append(GRAPH_MEMORIES_MAX)

    try:
        async with pool.connection() as conn:
            # This query is wide rather than deep, and the cost estimate crosses
            # Postgres's JIT threshold: EXPLAIN ANALYZE showed 732 ms of a
            # 1,042 ms run spent compiling expressions that then run once.
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute("SET LOCAL jit = off")
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


# ── Episode diversity ──────────────────────────────────────────────────────
# Bulk imports (the 2026-06 ChatGPT import, phase 4 repair, phase 5 documents)
# wrote many memories per source episode: one conversation can hold tens of
# ~2 KB transcript chunks plus one distilled summary. Cosine re-ranking alone
# fills every result slot from one long conversation, so recall answers
# "my approach to design" with six fragments of a single chat.
#
# Measured 2026-11-11 over 8 queries × top-6: 40% of slots were raw archive
# chunks, and for 3 of the 8 no summary appeared at all. Retirement was
# rejected (an LLM audit found 85% of chunks carry facts the summaries omit),
# so the fix is here: cap each episode's contribution and give distilled
# summaries a small edge over the raw text they summarise.
_ARCHIVE_SUMMARY_PREFIXES = ("ChatGPT conversation ", "ChatGPT document ", "ChatGPT Site ")

_CONV_CHUNK = re.compile(r"^ChatGPT conversation: (.+?)\s*\((\d{4}-\d{2}-\d{2})\)")
_CONV_SUMMARY = re.compile(r'^ChatGPT conversation "(.+?)"\s*\((\d{4}-\d{2}-\d{2})\)')
_DOC_CHUNK = re.compile(r"^ChatGPT document: (.+?)\s*\(uploaded (\d{4}-\d{2}-\d{2})\)")
_DOC_SUMMARY = re.compile(r'^ChatGPT document "(.+?)"\s*\((\d{4}-\d{2}-\d{2})\)')
_LEGACY_CHUNK = re.compile(
    r"^# (?:archives/chatgpt/)?(?:\d{4}-\d{2}/)?(\d{4}-\d{2}-\d{2})_(.+?)\.md"
)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def conversation_key(text: str) -> str | None:
    """Grouping key shared by an archive item and its distilled summary.

    Phase 3 imported one episode per chunk, so chunks of a single conversation
    do NOT share a source_episode_id; the statement header is the only thing
    that ties them together. Phase 4 chunks and their summaries DO share an
    episode, but keying on the text works for both — and lets a legacy
    `# archives/chatgpt/…` chunk line up with the summary written from it.
    """
    if not text:
        return None
    for rx in (_CONV_CHUNK, _CONV_SUMMARY):
        m = rx.match(text)
        if m:
            return f"conv:{m.group(2)}:{_slug(m.group(1))}"
    for rx in (_DOC_CHUNK, _DOC_SUMMARY):
        m = rx.match(text)
        if m:
            return f"doc:{m.group(2)}:{_slug(m.group(1))}"
    m = _LEGACY_CHUNK.match(text)
    if m:
        return f"conv:{m.group(1)}:{_slug(m.group(2))}"
    if text.startswith("ChatGPT Site "):
        m = re.match(r'ChatGPT Site "(.+?)"', text)
        if m:
            return f"site:{_slug(m.group(1))}"
    return None


def is_archive_summary(text: str) -> bool:
    """True for the distilled one-memory-per-item summaries written by the
    ChatGPT importers. Their text starts with the item kind, never '# '."""
    return bool(text) and text.startswith(_ARCHIVE_SUMMARY_PREFIXES)


_SUMMARY_INDEX: dict[str, Any] = {"at": 0.0, "map": {}}


async def load_summary_index(pool: Any, scope: str, ttl: float = 300.0) -> dict[str, dict]:
    """conversation_key → distilled summary row, cached briefly.

    Keys come from two places because they disagree: a legacy chunk's statement
    starts with the mirror FILE name (`…_cover-letter-for-product-designer-role`)
    while its summary says `"Cover Letter for Product Designer"`. The ledger is
    the only thing that knows both belong together, so it supplies the extra
    keys. Summaries only change on import, and the recall cache is keyed on a
    generation counter, so a short TTL keeps this off the hot path.
    """
    now = time.monotonic()
    if now - float(_SUMMARY_INDEX["at"]) < ttl and _SUMMARY_INDEX["map"]:
        return _SUMMARY_INDEX["map"]
    index: dict[str, dict] = {}
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                # NB: psycopg treats a bare % as a placeholder, so LIKE patterns
                # in a parameterised query must be written with %%.
                "SELECT id, statement, source_episode_id, tier, epistemic_status, valid_from "
                "FROM memories WHERE scope = %s AND valid_to IS NULL "
                "AND (statement LIKE 'ChatGPT conversation %%' "
                "OR statement LIKE 'ChatGPT document %%' OR statement LIKE 'ChatGPT Site %%')",
                (scope,),
            )
            for rid, stmt, epi, tier, status, vfrom in await cur.fetchall():
                entry = {"id": rid, "text": stmt, "source_episode_id": epi,
                         "tier": tier, "epistemic_status": status, "valid_from": vfrom}
                key = conversation_key(stmt)
                if key:
                    index[key] = entry

            # Conversation ledger: file-name slug → its summary.
            await cur.execute(
                "SELECT cc.created_at, cc.title, cc.md_path, m.id, m.statement, "
                "m.source_episode_id, m.tier, m.epistemic_status, m.valid_from "
                "FROM chatgpt_conversations cc JOIN memories m ON m.id = cc.summary_mem_id"
            )
            for created, title, md_path, rid, stmt, epi, tier, status, vfrom in await cur.fetchall():
                entry = {"id": rid, "text": stmt, "source_episode_id": epi,
                         "tier": tier, "epistemic_status": status, "valid_from": vfrom}
                date = created.strftime("%Y-%m-%d") if created else ""
                names = [title]
                if md_path:
                    stem = Path(md_path).stem        # 2026-01-28_cover-letter-…
                    names.append(stem[11:] if len(stem) > 11 else stem)
                for name in names:
                    if name:
                        index[f"conv:{date}:{_slug(name)}"] = entry

            # Document ledger: name only — the chunk header's date and the
            # ledger's can differ (undated uploads).
            await cur.execute(
                "SELECT cf.file_name, m.id, m.statement, m.source_episode_id, m.tier, "
                "m.epistemic_status, m.valid_from "
                "FROM chatgpt_files cf JOIN memories m ON m.id = cf.summary_mem_id"
            )
            for file_name, rid, stmt, epi, tier, status, vfrom in await cur.fetchall():
                index[f"doc:*:{_slug(file_name)}"] = {
                    "id": rid, "text": stmt, "source_episode_id": epi,
                    "tier": tier, "epistemic_status": status, "valid_from": vfrom}
    _SUMMARY_INDEX["at"], _SUMMARY_INDEX["map"] = now, index
    return index


def lookup_summary(index: dict[str, dict], key: str) -> dict | None:
    """Exact key first, then the date-insensitive document key."""
    hit = index.get(key)
    if hit is not None:
        return hit
    if key.startswith("doc:"):
        return index.get("doc:*:" + key.split(":", 2)[-1])
    return None


def diversify_by_episode(
    items: list[Any],
    score_of: Any,
    max_per_episode: int = 2,
    summary_boost: float = 0.03,
    key_of: Any = None,
) -> list[Any]:
    """Re-order ranked items so no single source item dominates the set.

    `score_of(item) -> float` supplies relevance; `key_of(item) -> str` the
    grouping key (defaults to source_episode_id). Items are taken greedily in
    boosted order; a group may contribute at most `max_per_episode` items, and
    only its summary when one is present among the candidates.

    Stable: ties keep their incoming order, so callers can pass a cosine-sorted
    list and get the same order back when nothing is capped.
    """
    if key_of is None:
        def key_of(it):
            return getattr(it, "source_episode_id", "") or getattr(it, "id", "")
    decorated = [
        ((score_of(it) + (summary_boost if is_archive_summary(getattr(it, "text", "") or "")
                          else 0.0)), idx, it)
        for idx, it in enumerate(items)
    ]
    decorated.sort(key=lambda t: (-t[0], t[1]))

    has_summary: set[str] = {
        key_of(it) for it in items
        if is_archive_summary(getattr(it, "text", "") or "")
    }
    counts: dict[str, int] = {}
    out: list[Any] = []
    for _, _, it in decorated:
        key = key_of(it)
        text = getattr(it, "text", "") or ""
        if key in has_summary:
            # The summary is the item's representative: a chunk from the same
            # conversation is redundant next to it, however well it scored.
            if not is_archive_summary(text) or counts.get(key, 0):
                continue
            counts[key] = 1
            out.append(it)
            continue
        cap = max(1, max_per_episode)
        if counts.get(key, 0) >= cap:
            continue
        counts[key] = counts.get(key, 0) + 1
        out.append(it)
    return out


# ── Main entry point ───────────────────────────────────────────────────────


async def retrieve(
    request: RetrievalRequest,
    pool: Any,
    embedder: Any,
    reranker: Any = None,
    redis: Any = None,
    fast_followup: bool = False,
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
    # Agentic follow-up rounds. MEASURED 2026-06-16: deep's 3 follow-up rounds
    # were latency-only with ZERO quality gain — the /api/recall endpoint (deep's
    # ONLY caller) re-ranks the candidate pool by true cosine to the ORIGINAL
    # query, and round-1 dense (ef_search=128, k=50) already covers that query,
    # so the degenerate follow-up ("what are my…", produced by truncation in
    # _detect_coverage_gap) can only add items that never out-rank round-1 hits.
    # Verified across 8 diverse queries: top-8 byte-identical with rounds 3→0,
    # deep recall 815-1436ms → 236-297ms (3-4x). So deep now does 0 follow-ups;
    # its depth comes from ef_search/k/graph_hops below, not from rounds.
    # Divergent KEEPS its 1 round — it's consumed in fused order by
    # orchestration/workflows.py discovery (no cosine re-rank downstream), where
    # the extra round can still contribute. fast opts in via recall_fast_followups.
    max_rounds = 1 if divergent else (1 if fast_followup else 0)

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

    # Await them together, not one after another: `await dense_task` raising on
    # the first branch used to leave sparse/entity running unpolled, holding
    # pool connections until the loop closed them.
    stage1 = [t for t in (dense_task, sparse_task, entity_task) if t is not None]
    try:
        dense = await dense_task if dense_task else []
        sparse = await sparse_task
        entity = await entity_task
    finally:
        for t in stage1:
            if not t.done():
                t.cancel()
        await asyncio.gather(*stage1, return_exceptions=True)

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
    # Re-fuse, now with the graph as its own rank channel. Without a channel the
    # graph could only re-confirm what dense/sparse/entity already returned.
    if graph_items:
        all_candidates = _rrf_fuse(
            all_candidates,
            dense_ranked=dense,
            sparse_ranked=sparse,
            entity_ranked=entity,
            divergent=divergent,
            graph_ranked=graph_items,
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
