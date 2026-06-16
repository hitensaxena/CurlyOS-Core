"""Re-embed memories.embedding via the Core ML / ANE embedding sidecar so the
stored corpus vectors live in the SAME fp16 space as the query embeddings the
recall path now produces (CURLYOS_EMBED_URL).

The new embeddings are cos ~0.99999 to the old sentence-transformers ones, so
the system stays correct DURING the run (mixed old/new vectors are fine). This
just removes the last tiny drift by making query + corpus self-consistent.

    set -a; . ./.env; set +a
    .venv/bin/python deploy/reembed_memories_sidecar.py            # all rows
    LIMIT=200 .venv/bin/python deploy/reembed_memories_sidecar.py  # test batch
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from psycopg_pool import AsyncConnectionPool
from shared.embeddings.implementations import HttpEmbedder

DSN = os.environ["CURLYOS_DATABASE_URL"]
URL = os.environ.get("CURLYOS_EMBED_URL", "http://127.0.0.1:8650")
CAP = 4000          # matches the system's memory-embedding text cap
BATCH = 64          # texts per sidecar HTTP round-trip
LIMIT = int(os.environ.get("LIMIT", "0"))  # 0 = all


def log(m: str) -> None:
    print(m, flush=True)


async def main() -> None:
    embedder = HttpEmbedder(URL)
    await embedder.embed(["warmup"])
    log(f"sidecar ready at {URL}")
    pool = AsyncConnectionPool(DSN, min_size=1, max_size=3, open=False)
    await pool.open()
    async with pool.connection() as c:
        q = "SELECT id, statement FROM memories WHERE statement IS NOT NULL ORDER BY id"
        if LIMIT:
            q += f" LIMIT {LIMIT}"
        rows = await (await c.execute(q)).fetchall()
    log(f"memories to re-embed: {len(rows)}")
    done = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        texts = [(s or "")[:CAP] for _, s in chunk]
        try:
            vecs = await embedder.embed(texts)
            async with pool.connection() as c:
                async with c.cursor() as cur:
                    for (mid, _), v in zip(chunk, vecs):
                        await cur.execute(
                            "UPDATE memories SET embedding = %s::vector WHERE id = %s", (str(v), mid)
                        )
                await c.commit()
            done += len(chunk)
        except Exception as e:  # noqa: BLE001
            log(f"  batch @{i} failed: {e}")
        if done % 1024 < BATCH:
            log(f"  {done}/{len(rows)}")
    log(f"DONE: re-embedded {done}/{len(rows)} memories")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
