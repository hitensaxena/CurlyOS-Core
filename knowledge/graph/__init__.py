"""Knowledge graph projection — canonical + speculative projections.

Architecture:
  - Canonical projection: :Entity, :Person, :Project, :Decision, :Concept
  - Speculative projection: :Hypothesis, :Concept, :Possibility, :Scenario, :Goal

Implementation: Postgres-backed knowledge store using a `knowledge_entities`
and `knowledge_edges` table. Neo4j can be swapped in later via the same
interface — all consumers call the GraphStore ABC.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from shared.types.ulid import mint, is_valid
from shared.events import build_event

log = logging.getLogger("curlyos.knowledge.graph")


# ── DDL ─────────────────────────────────────────────────────────────────────

GRAPH_DDL = """
CREATE TABLE IF NOT EXISTS knowledge_entities (
  id               text        PRIMARY KEY,
  scope            text        NOT NULL,
  name             text        NOT NULL,
  label            text        NOT NULL DEFAULT 'Entity',
  properties       jsonb       NOT NULL DEFAULT '{}',
  embedding        vector(1024),
  epistemic_status text        NOT NULL DEFAULT 'canonical',
  valid_from       timestamptz NOT NULL DEFAULT now(),
  valid_to         timestamptz,
  ingested_at      timestamptz NOT NULL DEFAULT now(),
  source_episode_id text,
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS knowledge_edges (
  id               text        PRIMARY KEY,
  src_entity_id    text        NOT NULL REFERENCES knowledge_entities(id),
  dst_entity_id    text        NOT NULL REFERENCES knowledge_entities(id),
  rel_type         text        NOT NULL,
  properties       jsonb       NOT NULL DEFAULT '{}',
  valid_from       timestamptz NOT NULL DEFAULT now(),
  valid_to         timestamptz,
  source_episode_id text,
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ke_scope_label ON knowledge_entities (scope, label) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_ke_name ON knowledge_entities (name) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_ke_hnsw ON knowledge_entities
  USING hnsw (embedding vector_cosine_ops) WITH (m=32, ef_construction=200);
CREATE INDEX IF NOT EXISTS idx_kedge_src ON knowledge_edges (src_entity_id) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_kedge_dst ON knowledge_edges (dst_entity_id) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_kedge_rel ON knowledge_edges (rel_type) WHERE valid_to IS NULL;
"""


# ── Helpers ──────────────────────────────────────────────────────────────────

def _row_to_entity(row) -> dict:
    """Convert a knowledge_entities row to a dict."""
    return {
        "id": row[0],
        "scope": row[1],
        "name": row[2],
        "label": row[3],
        "properties": row[4] if row[4] is not None else {},
        "embedding": row[5],
        "epistemic_status": row[6],
        "valid_from": row[7],
        "valid_to": row[8],
        "ingested_at": row[9],
        "source_episode_id": row[10],
        "created_at": row[11],
    }


def _row_to_edge(row) -> dict:
    """Convert a knowledge_edges row to a dict."""
    return {
        "id": row[0],
        "src_entity_id": row[1],
        "dst_entity_id": row[2],
        "rel_type": row[3],
        "properties": row[4] if row[4] is not None else {},
        "valid_from": row[5],
        "valid_to": row[6],
        "source_episode_id": row[7],
        "created_at": row[8],
    }


# ── Core graph operations ──────────────────────────────────────────────────

async def create_entity(
    pool: Any,
    publisher: Any,
    scope: str,
    name: str,
    label: str = "Entity",
    properties: dict | None = None,
    source_episode_id: str | None = None,
    epistemic_status: str = "canonical",
    embedder: Any = None,
) -> dict:
    """Create a knowledge entity node.

    Generates a ULID with 'ent' prefix, inserts into knowledge_entities,
    optionally generates an embedding, and stages a knowledge.entity.created event.
    """
    entity_id = mint("ent")
    props = properties or {}

    # Build embedding text if embedder is provided. Use embed_single — embed()
    # expects a list and returns a list-of-vectors; passing a bare string only
    # produced a usable vector by accident.
    embedding = None
    if embedder is not None:
        embed_text = f"{name} {json.dumps(props)}" if props else name
        try:
            embedding = await embedder.embed_single(embed_text)
        except Exception as e:
            log.warning("Embedding generation failed for entity %s: %s", entity_id, e)

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            if embedding is not None:
                await cur.execute(
                    "INSERT INTO knowledge_entities "
                    "(id, scope, name, label, properties, embedding, epistemic_status, source_episode_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "RETURNING id, scope, name, label, properties, embedding, "
                    "epistemic_status, valid_from, valid_to, ingested_at, source_episode_id, created_at",
                    (entity_id, scope, name, label,
                     json.dumps(props), embedding, epistemic_status, source_episode_id),
                )
            else:
                await cur.execute(
                    "INSERT INTO knowledge_entities "
                    "(id, scope, name, label, properties, epistemic_status, source_episode_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "RETURNING id, scope, name, label, properties, embedding, "
                    "epistemic_status, valid_from, valid_to, ingested_at, source_episode_id, created_at",
                    (entity_id, scope, name, label,
                     json.dumps(props), epistemic_status, source_episode_id),
                )
            row = await cur.fetchone()
            entity = _row_to_entity(row)

        # Stage event inside the same transaction
        event = build_event(
            short_type="knowledge.entity.created",
            subject=entity_id,
            scope={"level": "user", "scope": scope},
            data={
                "entity_id": entity_id,
                "name": name,
                "label": label,
                "epistemic_status": epistemic_status,
                "scope": scope,
            },
        )
        await publisher.stage(event, conn)

    return entity


async def get_entity(pool: Any, entity_id: str) -> dict | None:
    """Fetch a single entity by ID. Returns None if not found."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, scope, name, label, properties, embedding, "
                "epistemic_status, valid_from, valid_to, ingested_at, source_episode_id, created_at "
                "FROM knowledge_entities WHERE id = %s",
                (entity_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            return _row_to_entity(row)


async def search_entities(
    pool: Any,
    scope: str,
    query: str | None = None,
    label: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Search entities by name (ILIKE) and/or label.

    If query is provided, uses ILIKE on name for text search.
    Dense vector search via pgvector can be added later.
    """
    conditions = ["scope = %s", "valid_to IS NULL"]
    params: list[Any] = [scope]

    if query is not None:
        conditions.append("name ILIKE %s")
        params.append(f"%{query}%")

    if label is not None:
        conditions.append("label = %s")
        params.append(label)

    params.append(limit)
    where_clause = " AND ".join(conditions)

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT id, scope, name, label, properties, embedding, "
                f"epistemic_status, valid_from, valid_to, ingested_at, source_episode_id, created_at "
                f"FROM knowledge_entities "
                f"WHERE {where_clause} "
                f"ORDER BY created_at DESC "
                f"LIMIT %s",
                tuple(params),
            )
            rows = await cur.fetchall()
            return [_row_to_entity(r) for r in rows]


async def create_edge(
    pool: Any,
    publisher: Any,
    src_entity_id: str,
    dst_entity_id: str,
    rel_type: str,
    properties: dict | None = None,
    source_episode_id: str | None = None,
) -> dict:
    """Create a knowledge edge between two entities.

    Validates both entities exist, inserts into knowledge_edges,
    and stages a knowledge.edge.created event.
    """
    edge_id = mint("cor")
    props = properties or {}

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            # Validate source entity exists
            await cur.execute(
                "SELECT id FROM knowledge_entities WHERE id = %s AND valid_to IS NULL",
                (src_entity_id,),
            )
            if await cur.fetchone() is None:
                raise ValueError(f"Source entity {src_entity_id} not found or invalidated")

            # Validate destination entity exists
            await cur.execute(
                "SELECT id FROM knowledge_entities WHERE id = %s AND valid_to IS NULL",
                (dst_entity_id,),
            )
            if await cur.fetchone() is None:
                raise ValueError(f"Destination entity {dst_entity_id} not found or invalidated")

            # Insert edge
            await cur.execute(
                "INSERT INTO knowledge_edges "
                "(id, src_entity_id, dst_entity_id, rel_type, properties, source_episode_id) "
                "VALUES (%s, %s, %s, %s, %s, %s) "
                "RETURNING id, src_entity_id, dst_entity_id, rel_type, properties, "
                "valid_from, valid_to, source_episode_id, created_at",
                (edge_id, src_entity_id, dst_entity_id, rel_type,
                 json.dumps(props), source_episode_id),
            )
            row = await cur.fetchone()
            edge = _row_to_edge(row)

        # Stage event inside the same transaction
        event = build_event(
            short_type="knowledge.edge.created",
            subject=edge_id,
            scope={"level": "user"},
            data={
                "edge_id": edge_id,
                "src_entity_id": src_entity_id,
                "dst_entity_id": dst_entity_id,
                "rel_type": rel_type,
            },
        )
        await publisher.stage(event, conn)

    return edge


async def get_neighbors(
    pool: Any,
    entity_id: str,
    rel_type: str | None = None,
    direction: str = "both",
    limit: int = 20,
) -> list[dict]:
    """Get neighboring entities and the edges connecting them.

    direction='outgoing': edges where entity_id is src
    direction='incoming': edges where entity_id is dst
    direction='both': UNION of both directions

    Returns list of {"entity": ..., "edge": ...} dicts.
    """
    rel_filter = ""
    params: list[Any] = []

    if rel_type is not None:
        rel_filter = "AND k.rel_type = %s"
        params.append(rel_type)

    limit_clause = "LIMIT %s"
    params.append(limit)

    if direction == "outgoing":
        sql = (
            "SELECT "
            "  e.id, e.scope, e.name, e.label, e.properties, e.embedding, "
            "  e.epistemic_status, e.valid_from, e.valid_to, e.ingested_at, "
            "  e.source_episode_id, e.created_at, "
            "  k.id, k.src_entity_id, k.dst_entity_id, k.rel_type, k.properties, "
            "  k.valid_from, k.valid_to, k.source_episode_id, k.created_at "
            "FROM knowledge_edges k "
            "JOIN knowledge_entities e ON e.id = k.dst_entity_id "
            "WHERE k.src_entity_id = %s AND k.valid_to IS NULL "
            f"{rel_filter} {limit_clause}"
        )
        query_params = [entity_id] + params

    elif direction == "incoming":
        sql = (
            "SELECT "
            "  e.id, e.scope, e.name, e.label, e.properties, e.embedding, "
            "  e.epistemic_status, e.valid_from, e.valid_to, e.ingested_at, "
            "  e.source_episode_id, e.created_at, "
            "  k.id, k.src_entity_id, k.dst_entity_id, k.rel_type, k.properties, "
            "  k.valid_from, k.valid_to, k.source_episode_id, k.created_at "
            "FROM knowledge_edges k "
            "JOIN knowledge_entities e ON e.id = k.src_entity_id "
            "WHERE k.dst_entity_id = %s AND k.valid_to IS NULL "
            f"{rel_filter} {limit_clause}"
        )
        query_params = [entity_id] + params

    else:  # both
        sql = (
            "(SELECT "
            "  e.id, e.scope, e.name, e.label, e.properties, e.embedding, "
            "  e.epistemic_status, e.valid_from, e.valid_to, e.ingested_at, "
            "  e.source_episode_id, e.created_at, "
            "  k.id, k.src_entity_id, k.dst_entity_id, k.rel_type, k.properties, "
            "  k.valid_from, k.valid_to, k.source_episode_id, k.created_at "
            "FROM knowledge_edges k "
            "JOIN knowledge_entities e ON e.id = k.dst_entity_id "
            "WHERE k.src_entity_id = %s AND k.valid_to IS NULL "
            f"{rel_filter}) "
            "UNION ALL "
            "(SELECT "
            "  e.id, e.scope, e.name, e.label, e.properties, e.embedding, "
            "  e.epistemic_status, e.valid_from, e.valid_to, e.ingested_at, "
            "  e.source_episode_id, e.created_at, "
            "  k.id, k.src_entity_id, k.dst_entity_id, k.rel_type, k.properties, "
            "  k.valid_from, k.valid_to, k.source_episode_id, k.created_at "
            "FROM knowledge_edges k "
            "JOIN knowledge_entities e ON e.id = k.src_entity_id "
            "WHERE k.dst_entity_id = %s AND k.valid_to IS NULL "
            f"{rel_filter}) "
            f"LIMIT %s"
        )
        # For UNION ALL, we need params for each branch plus the outer limit
        branch_params = [entity_id] + params[:-1]  # entity_id + rel_type (no limit)
        query_params = branch_params + branch_params + [limit]

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, tuple(query_params))
            rows = await cur.fetchall()
            results = []
            for r in rows:
                entity = _row_to_entity(r[:12])
                edge = _row_to_edge(r[12:])
                results.append({"entity": entity, "edge": edge})
            return results


async def k_hop_expand(
    pool: Any,
    entity_ids: list[str],
    k: int = 1,
    scope: str | None = None,
) -> set[str]:
    """Expand seed entities by k hops using a recursive CTE.

    Returns a set of entity IDs discovered within k hops (excluding seeds).
    """
    if not entity_ids:
        return set()

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            # Use recursive CTE for k-hop expansion
            # Join with knowledge_entities to filter by scope if provided
            if scope is not None:
                sql = (
                    "WITH RECURSIVE expansion AS ("
                    "  SELECT k.dst_entity_id as entity_id, 1 as depth "
                    "  FROM knowledge_edges k "
                    "  JOIN knowledge_entities e ON e.id = k.dst_entity_id "
                    "  WHERE k.src_entity_id = ANY(%s) AND k.valid_to IS NULL "
                    "  AND e.valid_to IS NULL AND e.scope = %s "
                    "  UNION "
                    "  SELECT k.dst_entity_id, e.depth + 1 "
                    "  FROM knowledge_edges k "
                    "  JOIN expansion e ON k.src_entity_id = e.entity_id "
                    "  JOIN knowledge_entities ke ON ke.id = k.dst_entity_id "
                    "  WHERE e.depth < %s AND k.valid_to IS NULL "
                    "  AND ke.valid_to IS NULL AND ke.scope = %s "
                    ") SELECT DISTINCT entity_id FROM expansion"
                )
                await cur.execute(sql, (entity_ids, scope, k, scope))
            else:
                sql = (
                    "WITH RECURSIVE expansion AS ("
                    "  SELECT k.dst_entity_id as entity_id, 1 as depth "
                    "  FROM knowledge_edges k "
                    "  WHERE k.src_entity_id = ANY(%s) AND k.valid_to IS NULL "
                    "  UNION "
                    "  SELECT k.dst_entity_id, e.depth + 1 "
                    "  FROM knowledge_edges k "
                    "  JOIN expansion e ON k.src_entity_id = e.entity_id "
                    "  WHERE e.depth < %s AND k.valid_to IS NULL "
                    ") SELECT DISTINCT entity_id FROM expansion"
                )
                await cur.execute(sql, (entity_ids, k))

            rows = await cur.fetchall()
            return {r[0] for r in rows}


async def invalidate_entity(
    pool: Any,
    publisher: Any,
    entity_id: str,
) -> dict:
    """Invalidate (soft-delete) an entity by setting valid_to = now()."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE knowledge_entities SET valid_to = now() WHERE id = %s "
                "RETURNING id, valid_to",
                (entity_id,),
            )
            row = await cur.fetchone()
            if row is None:
                raise ValueError(f"Entity {entity_id} not found")

        # Stage event inside the same transaction
        event = build_event(
            short_type="knowledge.entity.invalidated",
            subject=entity_id,
            scope={"level": "user"},
            data={"entity_id": entity_id},
        )
        await publisher.stage(event, conn)

    return {"id": row[0], "valid_to": row[1], "action": "invalidated"}


async def invalidate_edge(
    pool: Any,
    publisher: Any,
    edge_id: str,
) -> dict:
    """Invalidate (soft-delete) an edge by setting valid_to = now()."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE knowledge_edges SET valid_to = now() WHERE id = %s "
                "RETURNING id, valid_to",
                (edge_id,),
            )
            row = await cur.fetchone()
            if row is None:
                raise ValueError(f"Edge {edge_id} not found")

        # Stage event inside the same transaction
        event = build_event(
            short_type="knowledge.edge.invalidated",
            subject=edge_id,
            scope={"level": "user"},
            data={"edge_id": edge_id},
        )
        await publisher.stage(event, conn)

    return {"id": row[0], "valid_to": row[1], "action": "invalidated"}


# ── Legacy / compatibility functions ────────────────────────────────────────

async def merge_entity(
    pool: Any,
    existing_id: str,
    new_name: str,
    label: str = "Entity",
    properties: dict | None = None,
) -> dict:
    """Merge a new mention into an existing entity (add alias, bump count)."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            # Add new name as alias in properties
            await cur.execute(
                "SELECT properties FROM knowledge_entities WHERE id = %s",
                (existing_id,),
            )
            row = await cur.fetchone()
            if row:
                props = json.loads(row[0]) if isinstance(row[0], str) else (row[0] or {})
                aliases = props.get("aliases", [])
                if new_name not in aliases and new_name != props.get("name"):
                    aliases.append(new_name)
                    props["aliases"] = aliases
                    props["mention_count"] = props.get("mention_count", 1) + 1
                    await cur.execute(
                        "UPDATE knowledge_entities SET properties = %s WHERE id = %s",
                        (json.dumps(props), existing_id),
                    )
    return {"id": existing_id, "action": "merged"}


async def extract_and_project(
    pool: Any,
    publisher: Any,
    scope: str,
    episode_id: str,
    episode_content: str,
    embedder: Any = None,
    llm_client: Any = None,
) -> dict:
    """Full extraction → resolution → projection pipeline for one episode.

    This is the main entry point called by the consolidation worker
    when processing `memory.episode.recorded` events.
    """
    from knowledge.extraction import extract_with_llm, ExtractedTriple
    from knowledge.resolution import resolve_entity, ResolutionDecision, normalize_mention

    # 1. Extract triples
    triples = await extract_with_llm(episode_content, episode_id, llm_client=llm_client)

    # 2. Resolve entities + project
    entities_created = 0
    entities_merged = 0
    edges_created = 0

    # Within-episode cache (normalized name → entity id). Dedupes repeated
    # mentions in one episode and dodges any read-after-write visibility gap
    # between the create_entity commit and the next resolve_entity lookup.
    seen: dict[str, str] = {}

    async def _get_or_create(mention: str) -> str | None:
        nonlocal entities_created, entities_merged
        norm = normalize_mention(mention)
        if len(norm) < 2:
            return None
        if norm in seen:
            entities_merged += 1
            return seen[norm]
        decision, key = await resolve_entity(
            mention, scope=scope, pool=pool, embedder=embedder,
        )
        if decision == ResolutionDecision.MINT or not key:
            ent = await create_entity(
                pool, publisher, scope, mention.strip(),
                source_episode_id=episode_id, embedder=embedder,
            )
            entities_created += 1
            eid = ent["id"]
        else:
            entities_merged += 1
            eid = key
        seen[norm] = eid
        return eid

    for triple in triples:
        s_id = await _get_or_create(triple.subject)
        o_id = await _get_or_create(triple.object)
        if s_id and o_id and s_id != o_id:
            await create_edge(
                pool, publisher,
                s_id, o_id, triple.predicate,
                properties={"confidence": triple.confidence},
                source_episode_id=episode_id,
            )
            edges_created += 1

    return {
        "triples_extracted": len(triples),
        "entities_created": entities_created,
        "entities_merged": entities_merged,
        "edges_created": edges_created,
    }
