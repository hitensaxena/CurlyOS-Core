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
    """Canonical comparison form for an entity name.

    Must stay identical to the expression behind knowledge_entities.name_norm
    (migration 0020): punctuation that only varies by typing — full stop, comma,
    hyphen-used-as-a-space — is dropped, whitespace collapsed, case lowered.
    Matching on lower(name) instead let "CurlyOS Core" and "curlyos-core" live
    as two nodes, which is how 25 duplicate pairs survived Tier 1.
    """
    stripped = (mention or "").replace(".", "").replace(",", "").replace("-", " ")
    return re.sub(r"\s+", " ", stripped).strip().lower()


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

    # 1. Known spelling variant → the entity it was folded into (Tier 1 alias
    #    table). Checked BEFORE the name match: an alias row is an explicit
    #    statement that two names are one thing, while a name match can land on
    #    a duplicate row that the alias already superseded ("User" and
    #    "CRAZYMAGE" still exist as entities; they mean Hiten Saxena).
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT a.entity_id FROM knowledge_entity_aliases a "
                    "JOIN knowledge_entities en ON en.id = a.entity_id "
                    "WHERE a.alias_norm = %s AND en.scope = %s AND en.valid_to IS NULL "
                    "ORDER BY a.created_at ASC LIMIT 1",
                    (normalized, scope),
                )
                row = await cur.fetchone()
                if row is not None:
                    return ResolutionDecision.MERGE, row[0]
    except Exception as e:  # noqa: BLE001
        log.warning("alias resolution failed for %r: %s", mention, e)

    # 2. Exact normalized-name match (scope-bound). Pick the OLDEST row so
    #    repeated resolution converges on a single canonical node. name_norm is
    #    a generated column, so the comparison cannot drift from the stored name.
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id FROM knowledge_entities "
                    "WHERE scope = %s AND name_norm = %s AND valid_to IS NULL "
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
