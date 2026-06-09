"""Entity resolution — determine whether two mentions refer to the same real-world entity.

Pipeline:
  1. Exact match — string equality (fastest)
  2. Alias match — known aliases (e.g. "Zed" == "Zed Editor")
  3. ANN blocking — embedding similarity >= threshold
  4. Cross-encoder — final decision (LLM or model)

Decision outcomes: MERGE, MINT (new entity), AMBIGUOUS (route to Reflection)
"""
from __future__ import annotations

import logging
from enum import StrEnum
from typing import Any

from shared.types.ulid import mint

log = logging.getLogger("curlyos.knowledge.resolution")


class ResolutionDecision(StrEnum):
    MERGE = "merge"       # Same entity — link to existing
    MINT = "mint"         # New entity — create fresh
    AMBIGUOUS = "ambiguous"  # Uncertain — defer to Reflection agent


# ── Entity mention store ────────────────────────────────────────────────────

# In-memory cache of known entities (populated from Postgres at startup)
_known_entities: dict[str, dict] = {}  # name → {id, embedding, aliases, mention_count}


def load_known_entities(pool: Any) -> None:
    """Load entity mentions from memories table into the resolution cache."""
    global _known_entities
    import psycopg

    try:
        conn = psycopg.connect(
            pool._dsn if hasattr(pool, '_dsn') else str(pool),
            autocommit=True
        )
        rows = conn.execute(
            "SELECT DISTINCT ON (statement_key) id, statement, statement_key "
            "FROM memories WHERE kind = 'fact' AND valid_to IS NULL "
            "ORDER BY statement_key, created_at DESC LIMIT 500"
        ).fetchall()
        for r in rows:
            key = r[2]  # statement_key (normalised)
            if key and len(key) > 3:
                _known_entities[key] = {"id": r[0], "statement": r[1]}
        conn.close()
        log.info("Loaded %d known entities for resolution", len(_known_entities))
    except Exception as e:
        log.warning("Failed to load known entities: %s", e)


async def resolve_entity(
    mention: str,
    embedder: Any = None,
    pool: Any = None,
    similarity_threshold: float = 0.85,
) -> tuple[ResolutionDecision, str | None]:
    """Resolve a mentioned entity against known entities.

    Returns (decision, existing_entity_key_or_None).
    """
    from shared.types.ulid import is_valid

    # Normalize
    import re
    normalized = re.sub(r"\s+", " ", mention.strip().lower())

    # 1. Exact match
    if normalized in _known_entities:
        return ResolutionDecision.MERGE, normalized

    # 2. Substring match (e.g. "Zed" in "Zed Editor")
    for key, info in _known_entities.items():
        if normalized in key or key in normalized:
            if len(normalized) >= 3 and len(key) >= 3:  # avoid tiny matches
                return ResolutionDecision.MERGE, key

    # 3. ANN blocking (if embedder available)
    if embedder is not None and pool is not None:
        try:
            vec = await embedder.embed_single(mention)
            # Query pgvector for nearest neighbor
            import psycopg
            conn = psycopg.connect(
                pool._dsn if hasattr(pool, '_dsn') else str(pool),
                autocommit=True
            )
            result = conn.execute(
                "SELECT statement_key, 1 - (embedding <=> %s::vector) AS sim "
                "FROM memories WHERE embedding IS NOT NULL AND kind = 'fact' AND valid_to IS NULL "
                "ORDER BY embedding <=> %s::vector LIMIT 1",
                [str(vec), str(vec)]
            ).fetchone()
            conn.close()
            if result and float(result[1]) >= similarity_threshold:
                return ResolutionDecision.MERGE, result[0]
        except Exception as e:
            log.debug("ANN resolution failed: %s", e)

    # 4. No match found → mint new entity
    return ResolutionDecision.MINT, None
