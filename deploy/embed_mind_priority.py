"""Targeted, resumable embed of the HIGH-VALUE mind chunk-memories with local
bge-m3 — the life narrative (memoirs/journals/philosophy/relationships/...),
NOT the 18k ChatGPT-archive chunks (those are a separate 17h CPU job).

Why: this box embeds bge-m3 at ~0.3 chunks/s (CPU-bound, ~1 vCPU). Embedding all
19k mind chunks would take ~17.6h, but 98% of them are archives/. The 373
life-content chunks + the Delhi-mentioning chunks (the user's actual goal:
"life in Delhi" recall) embed in ~25 min. Delhi chunks go FIRST so /api/recall
works within minutes while the rest finish.

Vectors are identical bge-m3 dense (1024-dim) -> fully compatible with the live
recall query embedder. Resumable: only touches embedding IS NULL rows.

  set INCLUDE_ARCHIVES=1 to also sweep archives/ (the slow 17h bulk).
  set DELHI_ONLY=1 for just the Delhi chunks (instant verify).

Run:
    set -a; . ./.env; set +a
    .venv/bin/python deploy/embed_mind_priority.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg
from shared.embeddings.implementations import LocalBgeM3

DSN = os.environ["CURLYOS_DATABASE_URL"]
BATCH = int(os.environ.get("EMBED_BATCH", "16"))
CAP = int(os.environ.get("CHUNK_CAP", "2200"))
INCLUDE_ARCHIVES = os.environ.get("INCLUDE_ARCHIVES") == "1"
DELHI_ONLY = os.environ.get("DELHI_ONLY") == "1"


def vec_lit(v) -> str:
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


async def main() -> None:
    emb = LocalBgeM3()
    await emb.embed(["warmup"])
    print("embedder loaded", flush=True)

    topdir = "split_part(substring(e.source_ref from 6),'/',1)"
    if DELHI_ONLY:
        where = "m.statement ILIKE '%delhi%'"
    elif INCLUDE_ARCHIVES:
        where = "TRUE"
    else:
        # life content + any Delhi chunk wherever it lives
        where = f"({topdir} <> 'archives' OR m.statement ILIKE '%delhi%')"

    conn = psycopg.connect(DSN, autocommit=False)
    rows = conn.execute(
        f"SELECT m.id, m.statement FROM memories m JOIN episodes e "
        f"ON m.source_episode_id = e.id "
        f"WHERE e.source_ref LIKE 'mind:%' AND m.embedding IS NULL AND ({where}) "
        f"ORDER BY (m.statement ILIKE '%delhi%') DESC, m.id"  # Delhi first; no params -> literal query
    ).fetchall()
    total = len(rows)
    print(f"chunks to embed: {total} (delhi-first; archives={'in' if INCLUDE_ARCHIVES else 'ex'}cluded)", flush=True)

    done = 0
    for i in range(0, total, BATCH):
        batch = rows[i:i + BATCH]
        vecs = await emb.embed([(t or "")[:CAP] for _, t in batch])
        payload = [(vec_lit(v), rid) for (rid, _), v in zip(batch, vecs)]
        with conn.cursor() as cur:
            cur.executemany("UPDATE memories SET embedding = %s::vector WHERE id = %s", payload)
        conn.commit()
        done += len(batch)
        if done % (BATCH * 4) == 0 or done == total:
            print(f"  {done}/{total} embedded", flush=True)
    conn.close()
    print("DONE", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
