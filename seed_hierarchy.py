"""Seed the workspace→project hierarchy from the user's real active goals.

For every active goal that isn't placed yet, create/look-up a workspace + project
(via workspace.hierarchy.place_goal) so each goal gets a real home on disk and a
studio. Idempotent — safe to re-run; already-placed goals are skipped.

Run:  cd ~/curlyos-core && set -a && . ./.env; set +a; .venv/bin/python3 seed_hierarchy.py
"""
from __future__ import annotations

import asyncio
import os

import psycopg_pool

from workspace.hierarchy import place_goal

SCOPE = os.environ.get("CURLYOS_SCOPE", "user:usr_hiten")
DSN = os.environ["CURLYOS_DATABASE_URL"]


async def main() -> None:
    pool = psycopg_pool.AsyncConnectionPool(DSN, min_size=1, max_size=3, open=False)
    await pool.open()
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, title FROM goals "
                    "WHERE scope=%s AND status='active' AND valid_to IS NULL "
                    "AND project_id IS NULL ORDER BY priority DESC", (SCOPE,),
                )
                goals = await cur.fetchall()
        if not goals:
            print("No unplaced active goals — nothing to seed.")
            return
        print(f"Placing {len(goals)} goal(s)…")
        for gid, title in goals:
            placement = await place_goal(pool, scope=SCOPE, goal_id=gid)
            if placement:
                p = placement["project"]
                print(f"  ✓ {title[:50]:50}  → {p['slug']}  ({placement['studio_dir']})")
            else:
                print(f"  ✗ {title[:50]} — placement failed")
        print("Done.")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
