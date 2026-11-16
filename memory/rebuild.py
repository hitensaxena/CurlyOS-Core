"""Rebuild projections from the event log.

The event log is the source of truth for graph and identity changes; memory
facts written after 2026-11-16 also carry their statement, so they are
rebuildable too. This module reports what is missing and restores it from the
events. It never deletes: an extra row is reported, not removed.

Historical memory.fact.stored events predate the statement field and cannot
recreate their row; `event_coverage` reports that split explicitly.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from shared.events import full_type

log = logging.getLogger("curlyos.memory.rebuild")

_GRAPH_TYPES = (
    full_type("knowledge.entity.created"),
    full_type("knowledge.edge.created"),
)
_GRAPH_INVALIDATED = (
    full_type("knowledge.entity.invalidated"),
    full_type("knowledge.edge.invalidated"),
)
_IDENTITY_TYPES = (full_type("identity.fact.updated"),)
_MEMORY_TYPES = (
    full_type("memory.fact.stored"),
    full_type("memory.fact.invalidated"),
)


def _dt(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            pass
    return fallback


async def _events(
    pool: Any, scope: str, types: tuple[str, ...], since: datetime | None = None,
) -> list[tuple]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            if since is None:
                await cur.execute(
                    "SELECT seq, type, subject, data, created_at FROM events "
                    "WHERE scope = %s AND type = ANY(%s) ORDER BY seq",
                    (scope, list(types)),
                )
            else:
                await cur.execute(
                    "SELECT seq, type, subject, data, created_at FROM events "
                    "WHERE scope = %s AND type = ANY(%s) AND created_at >= %s "
                    "ORDER BY seq",
                    (scope, list(types), since),
                )
            return list(await cur.fetchall())


async def _all_events(pool: Any, scope: str) -> list[tuple]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT seq, type, subject, data, created_at FROM events "
                "WHERE scope = %s ORDER BY seq",
                (scope,),
            )
            return list(await cur.fetchall())


async def _invalidated_ids(pool: Any, scope: str, types: tuple[str, ...]) -> set[str]:
    rows = await _events(pool, scope, types)
    return {
        row[3].get("entity_id") or row[3].get("edge_id") or row[2]
        for row in rows
    }


async def graph_diff(
    pool: Any, scope: str, since: datetime | None = None,
) -> dict:
    events = await _events(pool, scope, _GRAPH_TYPES, since)
    entity_ids = {
        e[3].get("entity_id") or e[2] for e in events if e[1] == _GRAPH_TYPES[0]
    }
    edge_ids = {e[3].get("edge_id") or e[2] for e in events if e[1] == _GRAPH_TYPES[1]}

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM knowledge_entities WHERE scope = %s", (scope,)
            )
            current_entities = {r[0] for r in await cur.fetchall()}
            await cur.execute(
                "SELECT ke.id FROM knowledge_edges ke "
                "JOIN knowledge_entities e ON e.id = ke.src_entity_id "
                "WHERE e.scope = %s",
                (scope,),
            )
            current_edges = {r[0] for r in await cur.fetchall()}

    gone_entities = entity_ids - current_entities
    gone_edges = edge_ids - current_edges
    invalidated_entities = await _invalidated_ids(pool, scope, (_GRAPH_INVALIDATED[0],))
    invalidated_edges = await _invalidated_ids(pool, scope, (_GRAPH_INVALIDATED[1],))
    return {
        "entities_expected": len(entity_ids),
        "entities_missing": sorted(gone_entities)[:20],
        "entities_missing_count": len(gone_entities),
        # Tracked deletions were evented; untracked ones were deleted by a path
        # that did not emit (historical graph_hygiene runs).
        "entities_tracked_deletions": len(gone_entities & invalidated_entities),
        "entities_untracked_deletions": len(gone_entities - invalidated_entities),
        "edges_expected": len(edge_ids),
        "edges_missing": sorted(gone_edges)[:20],
        "edges_missing_count": len(gone_edges),
        "edges_tracked_deletions": len(gone_edges & invalidated_edges),
        "edges_untracked_deletions": len(gone_edges - invalidated_edges),
    }


async def rebuild_graph(
    pool: Any, scope: str, *, dry_run: bool = False,
    since: datetime | None = None,
) -> dict:
    events = await _events(pool, scope, _GRAPH_TYPES, since)
    diff = await graph_diff(pool, scope, since)
    invalidated_entities = await _invalidated_ids(pool, scope, (_GRAPH_INVALIDATED[0],))
    invalidated_edges = await _invalidated_ids(pool, scope, (_GRAPH_INVALIDATED[1],))
    restored_entities = 0
    restored_edges = 0
    skipped_edges = 0

    if dry_run:
        return {**diff, "dry_run": True, "restored_entities": 0, "restored_edges": 0}

    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                # Entities first: an edge needs both endpoints.
                for _seq, ev_type, subject, data, created_at in events:
                    if ev_type != _GRAPH_TYPES[0]:
                        continue
                    entity_id = data.get("entity_id") or subject
                    if entity_id in invalidated_entities:
                        continue
                    await cur.execute(
                        "INSERT INTO knowledge_entities "
                        "(id, scope, name, label, properties, epistemic_status, "
                        " valid_from, valid_to, ingested_at, source_episode_id, created_at) "
                        "VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, NULL, %s, %s, %s) "
                        "ON CONFLICT (id) DO NOTHING",
                        (
                            entity_id, scope, data.get("name") or entity_id,
                            data.get("label") or "Entity",
                            json.dumps(data.get("properties") or {}),
                            data.get("epistemic_status") or "canonical",
                            _dt(data.get("valid_from"), created_at),
                            created_at,
                            data.get("source_episode_id"),
                            created_at,
                        ),
                    )
                    restored_entities += cur.rowcount or 0

                for _seq, ev_type, subject, data, created_at in events:
                    if ev_type != _GRAPH_TYPES[1]:
                        continue
                    edge_id = data.get("edge_id") or subject
                    if edge_id in invalidated_edges:
                        continue
                    await cur.execute(
                        "INSERT INTO knowledge_edges "
                        "(id, src_entity_id, dst_entity_id, rel_type, properties, "
                        " valid_from, valid_to, source_episode_id, created_at) "
                        "VALUES (%s, %s, %s, %s, %s::jsonb, %s, NULL, NULL, %s) "
                        "ON CONFLICT (id) DO NOTHING",
                        (
                            edge_id, data.get("src_entity_id"), data.get("dst_entity_id"),
                            data.get("rel_type"),
                            json.dumps(data.get("properties") or {}),
                            _dt(data.get("valid_from"), created_at),
                            created_at,
                        ),
                    )
                    restored_edges += cur.rowcount or 0
                    if cur.rowcount == 0:
                        skipped_edges += 1

    return {
        **diff, "dry_run": False,
        "restored_entities": restored_entities,
        "restored_edges": restored_edges,
        "skipped_edges": skipped_edges,
    }


async def identity_diff(pool: Any, scope: str) -> dict:
    events = await _events(pool, scope, _IDENTITY_TYPES)
    expected = {
        e[3].get("idf_id") or e[2].removeprefix("identity_fact:")
        for e in events
        if e[3].get("action_taken") in ("inserted", "superseded")
    }
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT id FROM identity_facts WHERE scope = %s", (scope,))
            current = {r[0] for r in await cur.fetchall()}
    return {
        "facts_expected": len(expected),
        "facts_missing": sorted(expected - current)[:20],
        "facts_missing_count": len(expected - current),
    }


async def rebuild_identity(pool: Any, scope: str, *, dry_run: bool = False) -> dict:
    events = await _events(pool, scope, _IDENTITY_TYPES)
    diff = await identity_diff(pool, scope)
    restored = 0
    closed = 0
    if dry_run:
        return {**diff, "dry_run": True, "restored_facts": 0, "closed_facts": 0}

    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                for _seq, _type, subject, data, created_at in events:
                    action = data.get("action_taken")
                    fact_id = data.get("idf_id") or subject.removeprefix("identity_fact:")
                    if action in ("inserted", "superseded"):
                        await cur.execute(
                            "INSERT INTO identity_facts "
                            "(id, scope, predicate, object, confidence, epistemic_status, "
                            " valid_from, ingested_at, source_episode_id) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                            "ON CONFLICT (id) DO NOTHING",
                            (
                                fact_id, scope, data.get("predicate"), data.get("object"),
                                data.get("confidence", 0.5),
                                data.get("epistemic_status") or "hypothesis",
                                _dt(data.get("valid_from"), created_at),
                                created_at, data.get("source_episode_id"),
                            ),
                        )
                        restored += cur.rowcount or 0

                    superseded_id = data.get("superseded_id")
                    if action == "superseded" and superseded_id:
                        await cur.execute(
                            "UPDATE identity_facts SET valid_to = %s, superseded_by = %s "
                            "WHERE id = %s AND scope = %s AND valid_to IS NULL",
                            (_dt(data.get("valid_to"), created_at), fact_id, superseded_id, scope),
                        )
                        closed += cur.rowcount or 0
                    elif action == "invalidated":
                        await cur.execute(
                            "UPDATE identity_facts SET valid_to = %s, superseded_by = %s "
                            "WHERE id = %s AND scope = %s AND valid_to IS NULL",
                            (_dt(data.get("valid_to"), created_at), data.get("superseded_by"),
                             fact_id, scope),
                        )
                        closed += cur.rowcount or 0

    return {
        **diff, "dry_run": False,
        "restored_facts": restored, "closed_facts": closed,
    }


async def memory_diff(pool: Any, scope: str) -> dict:
    events = await _events(pool, scope, _MEMORY_TYPES)
    stored = [e for e in events if e[1] == _MEMORY_TYPES[0]]
    rebuildable = {e[3].get("mem_id") or e[2] for e in stored if e[3].get("statement")}
    legacy = [e for e in stored if not e[3].get("statement")]
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT id FROM memories WHERE scope = %s", (scope,))
            current = {r[0] for r in await cur.fetchall()}
    return {
        "stored_events": len(stored),
        "rebuildable": len(rebuildable),
        "legacy_without_statement": len(legacy),
        "missing_rebuildable": sorted(rebuildable - current)[:20],
        "missing_rebuildable_count": len(rebuildable - current),
    }


async def rebuild_memories(pool: Any, scope: str, *, dry_run: bool = False) -> dict:
    events = await _events(pool, scope, _MEMORY_TYPES)
    diff = await memory_diff(pool, scope)
    restored = 0
    closed = 0
    if dry_run:
        return {**diff, "dry_run": True, "restored_memories": 0, "closed_memories": 0}

    from memory.governance import statement_key

    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                for _seq, ev_type, subject, data, created_at in events:
                    if ev_type != _MEMORY_TYPES[0]:
                        continue
                    statement = data.get("statement")
                    if not statement:
                        continue
                    mem_id = data.get("mem_id") or subject
                    skey = statement_key(statement)
                    await cur.execute(
                        "INSERT INTO memories "
                        "(id, scope, statement, statement_key, content_hash, kind, tier, "
                        " epistemic_status, valid_from, valid_to, ingested_at, source_episode_id) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s, %s) "
                        "ON CONFLICT (id) DO NOTHING",
                        (
                            mem_id, scope, statement, skey, data.get("content_hash"),
                            data.get("kind") or "fact", data.get("tier") or "semantic",
                            data.get("epistemic_status") or "canonical",
                            _dt(data.get("valid_from"), created_at), created_at,
                            data.get("source_episode_id"),
                        ),
                    )
                    restored += cur.rowcount or 0

                for _seq, ev_type, subject, data, created_at in events:
                    if ev_type != _MEMORY_TYPES[1]:
                        continue
                    mem_id = data.get("mem_id") or subject
                    await cur.execute(
                        "UPDATE memories SET valid_to = %s, superseded_by = %s "
                        "WHERE id = %s AND scope = %s AND valid_to IS NULL",
                        (_dt(data.get("valid_to"), created_at), data.get("superseded_by"),
                         mem_id, scope),
                    )
                    closed += cur.rowcount or 0

    return {
        **diff, "dry_run": False,
        "restored_memories": restored, "closed_memories": closed,
    }


async def event_coverage(pool: Any, scope: str) -> dict:
    rows = await _all_events(pool, scope)
    by_type: dict[str, int] = {}
    for _seq, ev_type, _subject, _data, _created in rows:
        by_type[ev_type] = by_type.get(ev_type, 0) + 1
    return {
        "total_events": len(rows),
        "by_type": by_type,
        "graph": await graph_diff(pool, scope),
        "identity": await identity_diff(pool, scope),
        "memories": await memory_diff(pool, scope),
    }
