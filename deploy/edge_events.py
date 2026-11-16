"""Stage `knowledge.edge.created` events for edges a script writes by hand.

`create_edge` stages its event in the same transaction as the row, which is why
the graph is replayable. The one-off graph scripts (densify, bridge) insert
edges with raw SQL for speed, so their edges used to land in the projection
with no record of how they got there — invisible to `rebuild_projections` and
to the dispatcher.

Call this with the exact rows you inserted. The payload is built from the same
tuple, so a row and its event cannot describe different things.

Usage inside a script:

    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO knowledge_edges (...) VALUES (...)", payload)
            stage_edge_created_events(cur, SCOPE, payload)

The helper takes a *cursor*, not a connection: the caller owns the transaction,
and both the sync scripts and the async integration test can use it.
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from typing import Any, Iterable

from shared.events import build_event
from shared.scope import scope_parts


def stage_edge_created_events(
    cur: Any, scope: str, rows: Iterable[tuple],
) -> int:
    """Insert one event per (edge_id, src, dst, rel_type, properties_json) row.

    `properties_json` may be a JSON string or a dict. Returns the number of
    events written. Idempotent at replay: the edge rows already exist, so the
    projection's `ON CONFLICT (id) DO NOTHING` leaves them untouched.
    """
    created = datetime.now(timezone.utc)
    events = []
    seen: set[str] = set()
    for edge_id, src_id, dst_id, rel_type, properties in rows:
        if edge_id in seen:
            continue
        seen.add(edge_id)
        props = properties if isinstance(properties, dict) else json.loads(properties or "{}")
        event = build_event(
            short_type="knowledge.edge.created",
            subject=edge_id,
            scope=scope_parts(scope),
            data={
                "edge_id": edge_id,
                "src_entity_id": src_id,
                "dst_entity_id": dst_id,
                "rel_type": rel_type,
                "scope": scope,
                "properties": props,
                "source_episode_id": None,
                "valid_from": created.isoformat(),
            },
            actor="system",
            source="curlyos-core/deploy/edge_events",
        )
        events.append((event["id"], event["type"], event["subject"], scope,
                       json.dumps(event["data"])))

    if not events:
        return 0

    result = cur.executemany(
        "INSERT INTO events (id, type, subject, scope, data) "
        "VALUES (%s, %s, %s, %s, %s::jsonb)",
        events,
    )
    if inspect.isawaitable(result):
        # An AsyncCursor returns a coroutine here. Calling it without awaiting
        # writes nothing and still looks successful — that is exactly how a
        # table ends up with rows whose events were never recorded.
        raise RuntimeError(
            "stage_edge_created_events is synchronous; pass a sync psycopg cursor"
        )
    return len(events)
