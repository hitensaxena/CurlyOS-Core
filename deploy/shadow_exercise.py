#!/usr/bin/env python3
"""Exercise the graph projection shadow verifier end-to-end.

The shadow period is only evidence if events actually flow through it. This
script drives the *production* write path (`create_entity`, `create_edge`,
`invalidate_entity`, `invalidate_edge`) in a throwaway scope, then asks the
running API server to dispatch those events to the graph projection handler and
reads back the counters that gate phase 2.

Two observations, in order:

  1. clean run   — 5 events, projection applies all 5, discrepancies +0.
  2. fault run   — one row is deliberately corrupted before a replay, so a
                   discrepancy MUST appear. If it does not, shadow verification
                   is not actually wired into the live process and observation 1
                   is a false green.

The fault is confined to `test:shadow-exercise` and the row is restored before
the script exits. Nothing outside that scope is touched.

Usage (from the repo root):
    .venv/bin/python deploy/shadow_exercise.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

SCOPE_PREFIX = "test:shadow-exercise"
API = "http://127.0.0.1:8643"
HANDLER = "graph_projection"


def _auth() -> dict[str, str]:
    """Bearer token when the API gate is armed (dormant by default)."""
    token = os.environ.get("CURLYOS_API_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _get(path: str) -> dict:
    req = urllib.request.Request(f"{API}{path}", headers=_auth())
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def _post(path: str) -> dict:
    req = urllib.request.Request(f"{API}{path}", method="POST", headers=_auth())
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.load(resp)


def _projection(scope: str) -> dict[str, float]:
    return _get(f"/api/events/dispatch?scope={scope}")["projection"]


def _delta(before: dict, after: dict) -> dict[str, float]:
    keys = set(before) | set(after)
    return {k: after.get(k, 0) - before.get(k, 0) for k in keys}


async def main() -> int:
    import psycopg.rows

    import api_server
    from knowledge.graph import (
        create_edge, create_entity, invalidate_edge, invalidate_entity,
    )

    pool = await api_server._get_async_pool(row_factory=psycopg.rows.tuple_row)
    publisher = api_server._make_publisher_sync()

    keep = "--keep" in sys.argv
    suffix = str(int(asyncio.get_event_loop().time() * 1000))[-8:]
    # A fresh scope per run: each invocation is an independent observation and
    # a replay from seq 0 covers exactly the events this run emitted.
    scope = f"{SCOPE_PREFIX}-{suffix}"
    print(f"scope={scope}")

    async def reset_watermark() -> None:
        async with pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM event_dispatch_watermarks "
                        "WHERE scope = %s AND handler = %s",
                        (scope, HANDLER),
                    )

    # ── 1. Drive the real write path ────────────────────────────────────────
    src = await create_entity(pool, publisher, scope, f"Shadow Probe A {suffix}",
                              label="Entity")
    dst = await create_entity(pool, publisher, scope, f"Shadow Probe B {suffix}",
                              label="Entity")
    edge = await create_edge(pool, publisher, src["id"], dst["id"], "probe_rel")
    await invalidate_edge(pool, publisher, edge["id"])
    await invalidate_entity(pool, publisher, dst["id"])

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT type, count(*) FROM events WHERE scope = %s GROUP BY 1 "
                "ORDER BY 1", (scope,),
            )
            emitted = {t.split("curlyos.")[-1]: n for t, n in await cur.fetchall()}
    emitted_total = sum(emitted.values())
    print(f"emitted {emitted_total} events: {emitted}")

    # ── 2. Clean dispatch (positive observation) ────────────────────────────
    before = _projection(scope)
    await reset_watermark()
    _post(f"/api/events/dispatch/run?scope={scope}&backfill=true")
    after = _projection(scope)
    clean = _delta(before, after)
    print(f"clean run  delta={clean}")

    # ── 3. SQL truth ────────────────────────────────────────────────────────
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, name, scope, valid_to IS NOT NULL FROM knowledge_entities "
                "WHERE id = ANY(%s) ORDER BY id", ([src["id"], dst["id"]],),
            )
            ents = await cur.fetchall()
            await cur.execute(
                "SELECT valid_to IS NOT NULL FROM knowledge_edges WHERE id = %s",
                (edge["id"],),
            )
            edge_closed = (await cur.fetchone())[0]
    print("rows:", [(r[1], r[3]) for r in ents], "edge closed:", edge_closed)

    # ── 4. Fault injection (negative control) ───────────────────────────────
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE knowledge_entities SET name = 'CORRUPTED' WHERE id = %s",
                    (src["id"],),
                )
    before2 = _projection(scope)
    await reset_watermark()
    _post(f"/api/events/dispatch/run?scope={scope}&backfill=true")
    fault = _delta(before2, _projection(scope))
    print(f"fault run  delta={fault}")

    # restore, then confirm the verifier goes quiet again
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE knowledge_entities SET name = %s WHERE id = %s",
                    (src["name"], src["id"]),
                )
    before3 = _projection(scope)
    await reset_watermark()
    _post(f"/api/events/dispatch/run?scope={scope}&backfill=true")
    restored = _delta(before3, _projection(scope))
    print(f"restore    delta={restored}")

    # ── 5. Cleanup, then verdict ────────────────────────────────────────────
    cleaned = "kept (--keep)"
    if not keep:
        async with pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        "DELETE FROM events WHERE scope = %s", (scope,))
                    events_deleted = cur.rowcount
                    await cur.execute(
                        "DELETE FROM knowledge_edges WHERE id = %s", (edge["id"],))
                    await cur.execute(
                        "DELETE FROM knowledge_entities WHERE id = ANY(%s)",
                        ([src["id"], dst["id"]],))
                    await cur.execute(
                        "DELETE FROM event_dispatch_watermarks "
                        "WHERE scope = %s", (scope,))
        cleaned = f"removed {events_deleted} events and the probe rows"

    applied = clean.get("event.graph_projection.applied", 0)
    clean_mismatch = clean.get("event.graph_projection.discrepancies", 0)
    fault_mismatch = fault.get("event.graph_projection.discrepancies", 0)
    restore_mismatch = restored.get("event.graph_projection.discrepancies", 0)

    checks = [
        ("all emitted events dispatched", applied == emitted_total),
        ("clean run has zero discrepancies", clean_mismatch == 0),
        ("injected corruption is detected", fault_mismatch >= 1),
        ("restored row is quiet again", restore_mismatch == 0),
        ("entity invalidation closed", ents and any(r[3] for r in ents)),
        ("edge invalidation closed", edge_closed is True),
    ]
    print("\nchecks:")
    ok = True
    for name, passed in checks:
        ok = ok and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    print(f"\ncleanup: {cleaned}")
    verdict = ("shadow gate observed" if ok
               else "NOT proven — do not remove inline writes")
    print("VERDICT:", verdict)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
