#!/usr/bin/env python3
"""Restore edges that were deleted with no replacement and no live reason.

Following the 2026-11-16 deletion audit, every gone edge now has an
invalidation event. The audit also found a residual set: edges between two
still-live entities where no edge survives between the same pair at all. Those
are facts that were genuinely lost — a stale-edge cleanup would have left the
endpoint invalidated, a dedupe would have left an equivalent edge behind, and a
relation rewrite would have left the pair connected under another rel_type.

The pair-level test is deliberate: an edge whose endpoints are still connected
by a different relation is not lost, and restoring it would add a
near-duplicate.

The restore is evented, not patched: each edge gets a `knowledge.edge.restored`
event staged and applied in one transaction. Replay order is created → the
backfilled invalidation → restored, so rebuilding from the log ends with the
edge open.

Dry run by default.

Usage (from the repo root):
    .venv/bin/python deploy/restore_edges.py            # list what it would do
    .venv/bin/python deploy/restore_edges.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

REASON = "restore: deleted with no replacement and both endpoints live"


async def find_candidates(pool, scope: str) -> list[dict]:
    """Created edges with no row, no duplicate triple, and two live endpoints."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT DISTINCT ON (coalesce(data->>'edge_id', subject)) "
                "  coalesce(data->>'edge_id', subject) AS edge_id, data, created_at "
                "FROM events WHERE scope = %s AND type LIKE %s "
                "ORDER BY coalesce(data->>'edge_id', subject), seq",
                (scope, "%knowledge.edge.created"),
            )
            created = await cur.fetchall()

    candidates: list[dict] = []
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            for edge_id, data, created_at in created:
                await cur.execute(
                    "SELECT 1 FROM knowledge_edges WHERE id = %s", (edge_id,))
                if await cur.fetchone() is not None:
                    continue
                await cur.execute(
                    "SELECT 1 FROM knowledge_edges WHERE src_entity_id = %s "
                    "AND dst_entity_id = %s",
                    (data.get("src_entity_id"), data.get("dst_entity_id")),
                )
                if await cur.fetchone() is not None:
                    continue
                await cur.execute(
                    "SELECT id FROM knowledge_entities WHERE id = ANY(%s) "
                    "AND valid_to IS NULL",
                    ([data.get("src_entity_id"), data.get("dst_entity_id")],),
                )
                if len(await cur.fetchall()) < 2:
                    continue
                candidates.append({
                    "edge_id": edge_id,
                    "src_entity_id": data.get("src_entity_id"),
                    "dst_entity_id": data.get("dst_entity_id"),
                    "rel_type": data.get("rel_type"),
                    "properties": data.get("properties") or {},
                    "source_episode_id": data.get("source_episode_id"),
                    "logged_at": created_at.isoformat(),
                })
    return candidates


async def restore(pool, scope: str, *, apply: bool = False) -> dict:
    from datetime import datetime, timezone

    from knowledge.graph.projection import apply_event
    from shared.events import build_event
    from shared.events.implementations import PgOnlyPublisher
    from shared.scope import scope_parts

    candidates = await find_candidates(pool, scope)
    result: dict = {
        "scope": scope, "dry_run": not apply,
        "candidates": len(candidates),
        "sample": candidates[:5],
    }
    if not apply or not candidates:
        return result

    publisher = PgOnlyPublisher()
    created = datetime.now(timezone.utc)
    restored = 0
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                for item in candidates:
                    event = build_event(
                        short_type="knowledge.edge.restored",
                        subject=item["edge_id"],
                        scope=scope_parts(scope),
                        data={
                            "edge_id": item["edge_id"],
                            "src_entity_id": item["src_entity_id"],
                            "dst_entity_id": item["dst_entity_id"],
                            "rel_type": item["rel_type"],
                            "scope": scope,
                            "properties": item["properties"],
                            "source_episode_id": item["source_episode_id"],
                            "valid_from": created.isoformat(),
                            "reason": REASON,
                        },
                        actor="system",
                        source="curlyos-core/deploy/restore_edges",
                    )
                    await publisher.stage(event, conn)
                    await apply_event(
                        cur, "knowledge.edge.restored", event["data"], scope, created,
                    )
                    restored += cur.rowcount or 0
    result["restored"] = restored
    return result


async def main() -> int:
    import psycopg.rows
    from psycopg_pool import AsyncConnectionPool

    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", default=None)
    parser.add_argument("--apply", action="store_true",
                        help="write the restores; default is a report")
    args = parser.parse_args()

    scope = args.scope or os.environ.get("CURLYOS_SCOPE") or "user:usr_hiten"
    dsn = (os.environ.get("CURLYOS_DATABASE_URL")
           or "postgresql://curlyos:postgres@127.0.0.1:54321/curlyos")

    pool = AsyncConnectionPool(
        dsn, min_size=1, max_size=2, open=False,
        kwargs={"row_factory": psycopg.rows.tuple_row},
    )
    await pool.open()
    try:
        result = await restore(pool, scope, apply=args.apply)
    finally:
        await pool.close()
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
