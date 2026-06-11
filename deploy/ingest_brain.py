"""One-off: ingest the orphaned brain-service store (/home/hiten/brain/data/brain.db
`nodes`) into curlyos-core as episodes + recallable memories, embed them, and run
the FIXED knowledge-graph extraction so the content joins the unified graph.

Mirrors the /api/ingest pipeline but runs in a fresh process (the live service
still has the pre-fix resolver loaded). Idempotent: skips nodes already ingested
(episodes.source_ref = 'brain:<node_id>').

Run from repo root with env sourced:
    set -a; . ./.env; set +a
    .venv/bin/python deploy/ingest_brain.py
"""
import asyncio
import logging
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from psycopg_pool import AsyncConnectionPool
from shared.embeddings.implementations import LocalBgeM3
from shared.events.implementations import PgOnlyPublisher
from memory.governance import record_episode, add
from knowledge.graph import extract_and_project

logging.basicConfig(level=logging.WARNING)
DSN = os.environ["CURLYOS_DATABASE_URL"]
SCOPE = os.environ.get("CURLYOS_SCOPE", "user:usr_hiten")
BRAIN_DB = os.environ.get("BRAIN_DB", "/tmp/brain_ro.db")
EPI_EMBED_CAP = 8000   # chars — keep encode within bge-m3 limit + memory-safe
MEM_STMT_CAP = 4000    # matches /api/ingest


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


def load_nodes():
    db = sqlite3.connect(BRAIN_DB)
    rows = db.execute(
        "SELECT id, type, coalesce(title,''), coalesce(content,'') FROM nodes "
        "WHERE length(coalesce(content,'')) > 0 ORDER BY created_at"
    ).fetchall()
    db.close()
    return rows


async def embed_row(pool, embedder, table, row_id, text):
    vec = await embedder.embed_single(text)
    async with pool.connection() as c:
        await c.execute(
            f"UPDATE {table} SET embedding = %s::vector WHERE id = %s",
            (str(vec), row_id),
        )


async def main() -> None:
    embedder = LocalBgeM3()
    await embedder.embed_single("warmup")
    log("embedder loaded")

    pool = AsyncConnectionPool(DSN, min_size=1, max_size=3, open=False)
    await pool.open()
    pub = PgOnlyPublisher()
    llm = await make_llm()
    log(f"llm={'yes' if llm else 'NO'}")

    nodes = load_nodes()
    log(f"brain nodes with content: {len(nodes)}")

    ingested = skipped = ent = edge = fails = 0
    for i, (nid, ntype, title, content) in enumerate(nodes, 1):
        source_ref = f"brain:{nid}"
        try:
            async with pool.connection() as c:
                cur = await c.execute(
                    "SELECT 1 FROM episodes WHERE source_ref = %s LIMIT 1", (source_ref,)
                )
                if await cur.fetchone():
                    skipped += 1
                    continue

            text = f"{title}\n\n{content}" if title else content
            epi = await record_episode(pool, pub, SCOPE, content=text, source_ref=source_ref)
            epi_id = epi["epi_id"]
            mem = await add(
                pool, pub, SCOPE, statement=text[:MEM_STMT_CAP],
                source_episode_id=epi_id, kind="fact", epistemic_status="canonical",
            )
            await embed_row(pool, embedder, "episodes", epi_id, text[:EPI_EMBED_CAP])
            if mem.get("mem_id"):
                await embed_row(pool, embedder, "memories", mem["mem_id"], text[:MEM_STMT_CAP])
            r = await extract_and_project(
                pool, pub, SCOPE, epi_id, text, embedder=embedder, llm_client=llm,
            )
            ent += r["entities_created"]
            edge += r["edges_created"]
            ingested += 1
        except Exception as e:  # noqa: BLE001
            fails += 1
            log(f"  fail node {nid} ({ntype}): {e}")
        if i % 10 == 0:
            log(f"  {i}/{len(nodes)}; ingested={ingested} skipped={skipped} "
                f"+{ent} entities +{edge} edges ({fails} fails)")

    log(f"DONE: ingested={ingested} skipped={skipped} +{ent} entities +{edge} edges, {fails} fails")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
