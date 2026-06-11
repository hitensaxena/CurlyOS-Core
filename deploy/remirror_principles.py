"""Refresh the principle -> memory mirror after consolidation.

Consolidating the principles table left ~44 stale verbose principle-mirror
memories in recall. Invalidate those and re-mirror the current 11 canonical
principles as fresh atomic, embedded memories (grounded in one provenance episode).

    set -a; . ./.env; set +a
    .venv/bin/python deploy/remirror_principles.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg
from psycopg_pool import AsyncConnectionPool
from shared.embeddings.implementations import LocalBgeM3
from shared.events.implementations import PgOnlyPublisher
from memory.governance import record_episode, add

DSN = os.environ["CURLYOS_DATABASE_URL"]
SCOPE = os.environ.get("CURLYOS_SCOPE", "user:usr_hiten")


def log(m): print(m, flush=True)


async def main():
    # 1. Invalidate stale principle-mirror memories.
    conn = psycopg.connect(DSN, autocommit=True)
    n = conn.execute(
        "UPDATE memories SET valid_to = now() "
        "WHERE valid_to IS NULL AND source_episode_id IN ("
        "  SELECT id FROM episodes WHERE source_ref ILIKE 'meta:principle%' "
        "  OR content ILIKE 'Principles distilled%')"
    ).rowcount
    log(f"invalidated {n} stale principle-mirror memories")
    principles = [r[0] for r in conn.execute(
        "SELECT statement FROM principles WHERE valid_to IS NULL AND scope = %s ORDER BY domain, statement",
        [SCOPE]).fetchall()]
    conn.close()
    log(f"canonical principles to mirror: {len(principles)}")
    if not principles:
        return

    # 2. Re-mirror as fresh embedded memories under one provenance episode.
    embedder = LocalBgeM3()
    await embedder.embed_single("warmup")
    pool = AsyncConnectionPool(DSN, min_size=1, max_size=3, open=False)
    await pool.open()
    pub = PgOnlyPublisher()
    epi = await record_episode(pool, pub, SCOPE,
                               content="Canonical principles (consolidated).",
                               source_ref="meta:principles-mirror")
    epi_id = epi["epi_id"]
    made = 0
    for stmt in principles:
        mem = await add(pool, pub, SCOPE, statement=stmt, source_episode_id=epi_id,
                        kind="fact", epistemic_status="canonical")
        vec = await embedder.embed_single(stmt[:4000])
        async with pool.connection() as c:
            await c.execute("UPDATE memories SET embedding = %s::vector WHERE id = %s",
                            (str(vec), mem["mem_id"]))
        # embed the provenance episode once
        made += 1
    evec = await embedder.embed_single("Canonical principles (consolidated).")
    async with pool.connection() as c:
        await c.execute("UPDATE episodes SET embedding = %s::vector WHERE id = %s", (str(evec), epi_id))
    log(f"DONE: mirrored {made} principles into fresh embedded memories")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
