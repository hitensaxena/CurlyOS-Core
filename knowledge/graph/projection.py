"""Graph projection handler for the event dispatcher.

Phase 1 of the migration: the inline writes in `create_entity`/`create_edge`
remain (callers need read-your-writes), and this handler independently applies
the same events idempotently. It is a verifier and repair path, not yet the
sole writer. Every operation is ON CONFLICT/WHERE valid_to IS NULL, so
replaying an event twice is harmless.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from shared import metrics
from shared.events.dispatch import Handler, register, registered

log = logging.getLogger("curlyos.knowledge.projection")

GRAPH_EVENT_TYPES = frozenset({
    "knowledge.entity.created",
    "knowledge.edge.created",
    "knowledge.entity.invalidated",
    "knowledge.edge.invalidated",
})


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


async def apply_event(
    cur: Any, short: str, data: dict, scope: str, created_at: Any,
) -> int:
    """Apply one graph event idempotently. Returns affected row count."""
    created = (
        created_at if isinstance(created_at, datetime)
        else _dt(created_at, datetime.now(timezone.utc))
    )

    if short == "knowledge.entity.created":
        await cur.execute(
            "INSERT INTO knowledge_entities "
            "(id, scope, name, label, properties, epistemic_status, "
            " valid_from, valid_to, ingested_at, source_episode_id, created_at) "
            "VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, NULL, %s, %s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (
                data.get("entity_id"), scope, data.get("name") or data.get("entity_id"),
                data.get("label") or "Entity",
                json.dumps(data.get("properties") or {}),
                data.get("epistemic_status") or "canonical",
                _dt(data.get("valid_from"), created), created,
                data.get("source_episode_id"), created,
            ),
        )
        return cur.rowcount or 0

    if short == "knowledge.edge.created":
        await cur.execute(
            "INSERT INTO knowledge_edges "
            "(id, src_entity_id, dst_entity_id, rel_type, properties, "
            " valid_from, valid_to, source_episode_id, created_at) "
            "VALUES (%s, %s, %s, %s, %s::jsonb, %s, NULL, NULL, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (
                data.get("edge_id"), data.get("src_entity_id"),
                data.get("dst_entity_id"), data.get("rel_type"),
                json.dumps(data.get("properties") or {}),
                _dt(data.get("valid_from"), created), created,
            ),
        )
        return cur.rowcount or 0

    if short == "knowledge.entity.invalidated":
        await cur.execute(
            "UPDATE knowledge_entities SET valid_to = %s "
            "WHERE id = %s AND scope = %s AND valid_to IS NULL",
            (created, data.get("entity_id"), scope),
        )
        return cur.rowcount or 0

    if short == "knowledge.edge.invalidated":
        await cur.execute(
            "UPDATE knowledge_edges SET valid_to = %s "
            "WHERE id = %s AND valid_to IS NULL",
            (created, data.get("edge_id")),
        )
        return cur.rowcount or 0

    return 0


def _discrepancy(event_id: str, detail: str) -> None:
    metrics.incr("event.graph_projection.discrepancies")
    log.warning("graph projection shadow discrepancy event=%s: %s", event_id, detail)


async def _verify(
    cur: Any, short: str, data: dict, scope: str, event_id: str,
) -> None:
    """Compare the stored row with the event payload. Report, never repair."""
    if short == "knowledge.entity.created":
        await cur.execute(
            "SELECT name, label, scope, epistemic_status FROM knowledge_entities "
            "WHERE id = %s",
            (data.get("entity_id"),),
        )
        row = await cur.fetchone()
        if row is None:
            _discrepancy(event_id, "entity row missing after apply")
            return
        expected = (
            data.get("name") or data.get("entity_id"),
            data.get("label") or "Entity",
            scope,
            data.get("epistemic_status") or "canonical",
        )
        if tuple(row) != expected:
            _discrepancy(event_id, f"entity fields differ: row={tuple(row)} event={expected}")

    elif short == "knowledge.edge.created":
        await cur.execute(
            "SELECT src_entity_id, dst_entity_id, rel_type FROM knowledge_edges "
            "WHERE id = %s",
            (data.get("edge_id"),),
        )
        row = await cur.fetchone()
        if row is None:
            _discrepancy(event_id, "edge row missing after apply")
            return
        expected = (
            data.get("src_entity_id"), data.get("dst_entity_id"), data.get("rel_type"),
        )
        if tuple(row) != expected:
            _discrepancy(event_id, f"edge fields differ: row={tuple(row)} event={expected}")

    elif short == "knowledge.entity.invalidated":
        await cur.execute(
            "SELECT valid_to FROM knowledge_entities WHERE id = %s",
            (data.get("entity_id"),),
        )
        row = await cur.fetchone()
        if row is None or row[0] is None:
            _discrepancy(event_id, "entity invalidation did not close the row")

    elif short == "knowledge.edge.invalidated":
        await cur.execute(
            "SELECT valid_to FROM knowledge_edges WHERE id = %s",
            (data.get("edge_id"),),
        )
        row = await cur.fetchone()
        if row is None or row[0] is None:
            _discrepancy(event_id, "edge invalidation did not close the row")


def build_graph_handler(
    pool_factory: Callable[[], Awaitable[Any]], *, shadow: bool = True,
) -> Handler:
    async def _apply(event: dict) -> None:
        pool = await pool_factory()
        async with pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await apply_event(
                        cur, event["short_type"], event["data"],
                        event["scope"], event["created_at"],
                    )
                    metrics.incr("event.graph_projection.applied")
                    if shadow:
                        await _verify(
                            cur, event["short_type"], event["data"],
                            event["scope"], event["subject"],
                        )

    return Handler("graph_projection", _apply, GRAPH_EVENT_TYPES)


def register_graph_handler(
    pool_factory: Callable[[], Awaitable[Any]], *, shadow: bool = True,
) -> None:
    if "graph_projection" in {handler.name for handler in registered()}:
        return
    register(build_graph_handler(pool_factory, shadow=shadow))
