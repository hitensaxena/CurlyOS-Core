"""One-off backfill: (2) embed entities missing vectors, (3) re-extract episodes
that never produced graph edges. Uses the FIXED resolution path so re-runs
dedupe into existing nodes instead of minting fresh ones.

Run from the repo root with the env sourced:
    set -a; . ./.env; set +a
    .venv/bin/python deploy/backfill_kg_extract.py
"""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from psycopg_pool import AsyncConnectionPool
from shared.embeddings.implementations import LocalBgeM3
from shared.events.implementations import PgOnlyPublisher
from knowledge.graph import extract_and_project

logging.basicConfig(level=logging.WARNING)
DSN = os.environ["CURLYOS_DATABASE_URL"]
SCOPE = os.environ.get("CURLYOS_SCOPE", "user:usr_hiten")
MIN_LEN = 80  # skip tiny/empty episodes


def log(m: str) -> None:
    print(m, flush=True)


async def make_llm():
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return None
    from openai import AsyncOpenAI
    return AsyncOpenAI(
        base_url="https://openrouter.ai/api/v1", api_key=key,
        timeout=60.0, max_retries=2,
    )


async def main() -> None:
    embedder = LocalBgeM3()
    await embedder.embed_single("warmup")  # load model once
    log("embedder loaded")

    pool = AsyncConnectionPool(DSN, min_size=1, max_size=3, open=False)
    await pool.open()
    pub = PgOnlyPublisher()
    llm = await make_llm()
    log(f"llm={'yes' if llm else 'NO'}")

    # ── Phase 2: backfill entity embeddings ──────────────────────────────────
    async with pool.connection() as c:
        cur = await c.execute(
            "SELECT id, name FROM knowledge_entities "
            "WHERE valid_to IS NULL AND embedding IS NULL"
        )
        rows = await cur.fetchall()
    log(f"embedding backfill: {len(rows)} entities")
    done = 0
    for eid, name in rows:
        try:
            vec = await embedder.embed_single(name)
            async with pool.connection() as c:
                await c.execute(
                    "UPDATE knowledge_entities SET embedding = %s::vector WHERE id = %s",
                    (str(vec), eid),
                )
            done += 1
        except Exception as e:  # noqa: BLE001
            log(f"  emb fail {eid}: {e}")
    log(f"embedding backfill done: {done}/{len(rows)}")

    # ── Phase 3: re-extract un-mined episodes ────────────────────────────────
    async with pool.connection() as c:
        cur = await c.execute(
            "WITH mined AS ("
            "  SELECT DISTINCT source_episode_id FROM knowledge_edges "
            "  WHERE source_episode_id IS NOT NULL) "
            "SELECT id, content FROM episodes "
            "WHERE scope = %s AND content IS NOT NULL AND char_length(content) >= %s "
            "AND id NOT IN (SELECT source_episode_id FROM mined) "
            "ORDER BY created_at DESC",
            (SCOPE, MIN_LEN),
        )
        eps = await cur.fetchall()
    log(f"re-extraction: {len(eps)} episodes")
    tot_e = tot_edge = fails = 0
    for i, (epi_id, content) in enumerate(eps, 1):
        try:
            r = await extract_and_project(
                pool, pub, SCOPE, epi_id, content,
                embedder=embedder, llm_client=llm,
            )
            tot_e += r["entities_created"]
            tot_edge += r["edges_created"]
        except Exception as e:  # noqa: BLE001
            fails += 1
            log(f"  extract fail {epi_id}: {e}")
        if i % 10 == 0:
            log(f"  {i}/{len(eps)} eps; +{tot_e} entities +{tot_edge} edges ({fails} fails)")
    log(f"re-extraction done: +{tot_e} entities, +{tot_edge} edges, {fails} fails")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
