#!/usr/bin/env python3
"""Backfill invalidation events for graph rows deleted before events existed.

The audit of 2026-11-16 found 6,449 edges and 290 entities that the event log
says were created, are absent from the graph, and have no invalidation event.
Most are duplicates collapsed by `graph_hygiene --janitor` before it emitted
events (Nov 10-11) plus an unattributed earlier era (Jun-Sep).

The graph is correct; the *log* is incomplete. This writes the missing
invalidation event for each id, so the log describes what actually happened.
That matters for two reasons:

  - `rebuild_projections --projection graph --apply` becomes safe: it skips
    invalidated ids, so it can no longer re-insert the duplicates and merged
    entities the janitor deliberately removed.
  - `graph_diff` untracked-deletion counts become meaningful again.

Events are stamped at backfill time with `backfilled: true` and an explicit
reason, because the real deletion time is unknown and inventing one would be
worse than admitting it. The shadow verifier accepts a backfilled invalidation
for a row that no longer exists; it is the whole point of the record.

Dry run by default.

Usage (from the repo root):
    .venv/bin/python deploy/backfill_invalidations.py            # report
    .venv/bin/python deploy/backfill_invalidations.py --apply    # write
    .venv/bin/python deploy/backfill_invalidations.py --since 2026-06-01
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

REASON = "backfill: deleted before graph deletions were evented"


async def _missing(pool, scope: str, kind: str, since) -> list[str]:
    """Ids with a created event, no row, and no invalidation event."""
    if kind == "entity":
        created_type = "%knowledge.entity.created"
        invalidated_type = "%knowledge.entity.invalidated"
        id_key = "entity_id"
        table = "knowledge_entities"
    else:
        created_type = "%knowledge.edge.created"
        invalidated_type = "%knowledge.edge.invalidated"
        id_key = "edge_id"
        table = "knowledge_edges"

    since_sql = " AND created_at >= %s" if since else ""

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT DISTINCT coalesce(data->>%s, subject) FROM events "
                "WHERE scope = %s AND type LIKE %s" + since_sql,
                [id_key, scope, created_type] + ([since] if since else []),
            )
            created = {r[0] for r in await cur.fetchall() if r[0]}

            await cur.execute(
                f"SELECT id FROM {table} WHERE id = ANY(%s)", (list(created),),
            )
            present = {r[0] for r in await cur.fetchall()}

            await cur.execute(
                "SELECT DISTINCT coalesce(data->>%s, subject) FROM events "
                "WHERE scope = %s AND type LIKE %s",
                (id_key, scope, invalidated_type),
            )
            already = {r[0] for r in await cur.fetchall() if r[0]}

    return sorted(created - present - already)


async def backfill(pool, scope: str, *, apply: bool = False, since=None) -> dict:
    from shared.events import build_event
    from shared.scope import scope_parts

    result: dict = {"scope": scope, "dry_run": not apply}
    if since is not None:
        result["since"] = since.isoformat()

    jobs = [
        ("entities", "knowledge.entity.invalidated", "entity_id"),
        ("edges", "knowledge.edge.invalidated", "edge_id"),
    ]
    total = 0
    for label, short_type, id_key in jobs:
        kind = "entity" if label == "entities" else "edge"
        ids = await _missing(pool, scope, kind, since)
        result[f"{label}_missing"] = len(ids)
        result[f"{label}_sample"] = ids[:5]
        total += len(ids)
        if not apply or not ids:
            continue

        rows = []
        for item_id in ids:
            event = build_event(
                short_type=short_type,
                subject=item_id,
                scope=scope_parts(scope),
                data={id_key: item_id, "reason": REASON, "backfilled": True},
                actor="system",
                source="curlyos-core/deploy/backfill_invalidations",
            )
            rows.append((event["id"], event["type"], event["subject"], scope,
                         json.dumps(event["data"])))

        async with pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.executemany(
                        "INSERT INTO events (id, type, subject, scope, data) "
                        "VALUES (%s, %s, %s, %s, %s::jsonb)",
                        rows,
                    )
        result[f"{label}_backfilled"] = len(rows)

    result["total_missing"] = total
    return result


async def main() -> int:
    import os

    import psycopg.rows
    from psycopg_pool import AsyncConnectionPool

    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", default=None)
    parser.add_argument("--apply", action="store_true",
                        help="write the events; default is a report")
    parser.add_argument("--since", default=None, help="ISO timestamp floor")
    args = parser.parse_args()

    since = None
    if args.since:
        from datetime import datetime, timezone

        since = datetime.fromisoformat(args.since)
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)

    scope = args.scope or os.environ.get("CURLYOS_SCOPE") or "user:usr_hiten"
    dsn = (os.environ.get("CURLYOS_DATABASE_URL")
           or "postgresql://curlyos:postgres@127.0.0.1:54321/curlyos")

    # A plain pool: importing api_server would pull in the embedder and take
    # minutes for a script that only reads the event log.
    pool = AsyncConnectionPool(
        dsn, min_size=1, max_size=2, open=False,
        kwargs={"row_factory": psycopg.rows.tuple_row},
    )
    await pool.open()
    try:
        result = await backfill(pool, scope, apply=args.apply, since=since)
    finally:
        await pool.close()
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
