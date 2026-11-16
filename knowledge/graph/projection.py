"""Graph projection: the single implementation that writes graph rows.

Every writer — `create_entity`, `create_edge`, the invalidation paths, the
decision-loop lesson mirror, and the event dispatcher — calls `apply_event` on
the cursor of its own transaction. The event payload is therefore both the
record of the write and its description: there is one INSERT for entities and
one for edges in the whole system, so a payload and a row cannot drift.

The dispatcher replay is the repair path, not the primary one. `apply_event` is
idempotent (`ON CONFLICT (id) DO NOTHING`, `WHERE valid_to IS NULL`), so
replaying an event that was already applied inline changes nothing.
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


def _vector(value: Any) -> str | None:
    """Render an embedding as a pgvector literal.

    Callers hand us either a list of floats (the embedder's return type) or an
    already-formatted string. Normalising here means no connection-level
    pgvector adapter is required, and `%s::vector` accepts both.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return "[" + ",".join(repr(float(x)) for x in value) + "]"


async def apply_event(
    cur: Any, short: str, data: dict, scope: str, created_at: Any,
    *, embedding: Any = None,
) -> int:
    """Apply one graph event idempotently. Returns affected row count.

    Call this on the cursor of the caller's own transaction, right after
    staging the event on the same connection, so the row and its event commit
    together. `embedding` is the one field not carried by the event: vectors
    are derived state, so a replay correctly leaves them untouched.
    """
    created = (
        created_at if isinstance(created_at, datetime)
        else _dt(created_at, datetime.now(timezone.utc))
    )

    if short == "knowledge.entity.created":
        await cur.execute(
            "INSERT INTO knowledge_entities "
            "(id, scope, name, label, properties, embedding, epistemic_status, "
            " valid_from, valid_to, ingested_at, source_episode_id, created_at) "
            "VALUES (%s, %s, %s, %s, %s::jsonb, %s::vector, %s, %s, NULL, %s, %s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (
                data.get("entity_id"), scope, data.get("name") or data.get("entity_id"),
                data.get("label") or "Entity",
                json.dumps(data.get("properties") or {}),
                _vector(embedding),
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
            "VALUES (%s, %s, %s, %s, %s::jsonb, %s, NULL, %s, %s) "
            "ON CONFLICT (id) DO NOTHING",
            (
                data.get("edge_id"), data.get("src_entity_id"),
                data.get("dst_entity_id"), data.get("rel_type"),
                json.dumps(data.get("properties") or {}),
                _dt(data.get("valid_from"), created),
                data.get("source_episode_id"), created,
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
        if row is None:
            # A backfilled invalidation records a deletion that happened before
            # deletions were evented; there is no row left to close, and that
            # is exactly what it says.
            if not data.get("backfilled"):
                _discrepancy(event_id, "entity invalidation did not close the row")
        elif row[0] is None:
            _discrepancy(event_id, "entity invalidation did not close the row")

    elif short == "knowledge.edge.invalidated":
        await cur.execute(
            "SELECT valid_to FROM knowledge_edges WHERE id = %s",
            (data.get("edge_id"),),
        )
        row = await cur.fetchone()
        if row is None:
            if not data.get("backfilled"):
                _discrepancy(event_id, "edge invalidation did not close the row")
        elif row[0] is None:
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
