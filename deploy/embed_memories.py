"""Embed valid memories that have no vector yet (bge-m3, local). Used after a
bulk insert (e.g. refine_blob_memories) so the new facts are dense-recallable
without waiting for the startup sweep.

    set -a; . ./.env; set +a
    .venv/bin/python deploy/embed_memories.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from psycopg_pool import AsyncConnectionPool
from shared.embeddings.implementations import LocalBgeM3

DSN = os.environ["CURLYOS_DATABASE_URL"]
SCOPE = os.environ.get("CURLYOS_SCOPE", "user:usr_hiten")
CAP = 4000  # matches the system's memory-embedding text cap


def log(m: str) -> None:
    print(m, flush=True)


async def main() -> None:
    embedder = LocalBgeM3()
    await embedder.embed_single("warmup")
    log("embedder loaded")
    pool = AsyncConnectionPool(DSN, min_size=1, max_size=3, open=False)
    await pool.open()
    async with pool.connection() as c:
        cur = await c.execute(
            "SELECT id, statement FROM memories "
            "WHERE valid_to IS NULL AND embedding IS NULL AND scope = %s",
            [SCOPE],
        )
        rows = await cur.fetchall()
    log(f"memories needing embeddings: {len(rows)}")
    done = 0
    for mid, stmt in rows:
        try:
            vec = await embedder.embed_single((stmt or "")[:CAP])
            async with pool.connection() as c:
                await c.execute("UPDATE memories SET embedding = %s::vector WHERE id = %s", (str(vec), mid))
            done += 1
        except Exception as e:  # noqa: BLE001
            log(f"  embed fail {mid}: {e}")
        if done % 100 == 0 and done:
            log(f"  {done}/{len(rows)}")
    log(f"DONE: embedded {done}/{len(rows)}")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
