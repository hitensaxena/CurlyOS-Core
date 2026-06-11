"""Entity resolution — decide whether a mention refers to an EXISTING graph node.

Resolution is performed directly against the `knowledge_entities` table (the
store the graph is actually built from), scope-bound, in two stages:

  1. Exact match  — normalized-name equality (uses idx_ke_name).
  2. ANN blocking — embedding cosine similarity >= threshold (uses idx_ke_hnsw),
                    e.g. "Zed" ≈ "Zed Editor".

A MERGE result carries a real `ent_` id that can be used directly as an edge
endpoint. This is what lets a node accumulate edges across mentions/episodes
instead of every triple minting a fresh, single-edge duplicate.

Decision outcomes: MERGE (link to existing entity), MINT (create a new entity).
"""
from __future__ import annotations

import logging
import re
from enum import StrEnum
from typing import Any

log = logging.getLogger("curlyos.knowledge.resolution")

DEFAULT_SCOPE = "user:usr_hiten"


class ResolutionDecision(StrEnum):
    MERGE = "merge"          # Same entity — link to the returned existing id
    MINT = "mint"            # New entity — create fresh
    AMBIGUOUS = "ambiguous"  # Reserved (defer to Reflection); not yet emitted


def normalize_mention(mention: str) -> str:
    """Canonical comparison form for an entity name."""
    return re.sub(r"\s+", " ", (mention or "").strip().lower())


async def resolve_entity(
    mention: str,
    scope: str = DEFAULT_SCOPE,
    pool: Any = None,
    embedder: Any = None,
    similarity_threshold: float = 0.85,
) -> tuple[ResolutionDecision, str | None]:
    """Resolve `mention` against existing entities in `scope`.

    Returns (decision, entity_id_or_None). With no pool we cannot look anything
    up, so we MINT (the offline/test path). The returned id on MERGE is a real
    knowledge_entities.id — safe to pass straight to create_edge.
    """
    normalized = normalize_mention(mention)
    if len(normalized) < 2 or pool is None:
        return ResolutionDecision.MINT, None

    # 1. Exact normalized-name match (scope-bound). Pick the OLDEST row so
    #    repeated resolution converges on a single canonical node.
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id FROM knowledge_entities "
                    "WHERE scope = %s AND lower(name) = %s AND valid_to IS NULL "
                    "ORDER BY created_at ASC LIMIT 1",
                    (scope, normalized),
                )
                row = await cur.fetchone()
                if row is not None:
                    return ResolutionDecision.MERGE, row[0]
    except Exception as e:  # noqa: BLE001 — resolution must never break ingest
        log.warning("exact-match resolution failed for %r: %s", mention, e)

    # 2. ANN blocking — semantic near-duplicate. Only sees embedded rows.
    if embedder is not None:
        try:
            vec = await embedder.embed_single(mention)
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT id, 1 - (embedding <=> %s::vector) AS sim "
                        "FROM knowledge_entities "
                        "WHERE embedding IS NOT NULL AND scope = %s AND valid_to IS NULL "
                        "ORDER BY embedding <=> %s::vector LIMIT 1",
                        (str(vec), scope, str(vec)),
                    )
                    row = await cur.fetchone()
                    if row is not None and float(row[1]) >= similarity_threshold:
                        return ResolutionDecision.MERGE, row[0]
        except Exception as e:  # noqa: BLE001
            log.debug("ANN resolution failed for %r: %s", mention, e)

    # 3. No match → mint a new entity.
    return ResolutionDecision.MINT, None
