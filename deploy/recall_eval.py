#!/usr/bin/env python3
"""Fixed-query benchmark for the recall pipeline — the Tier-2 verification harness.

Runs the same queries through the same code path /api/recall uses, and reports
what the result set looks like *and* what the knowledge graph contributed, so a
change to _graph_expand can be judged instead of felt.

Per query it records:
  n            items returned
  episodes     distinct source episodes (low = the list is one conversation
               repeated, which is what recall looked like before the 2026-07
               per-episode cap)
  max_ep       the largest single-episode share
  graph        items _graph_expand returned
  g_in_top10   how many of those survived into the first ten
  chatter      flagged `is_chatter` rows that made it into the list
  ms           wall time

Usage (from the repo root):
    set -a; . ./.env; set +a
    .venv/bin/python -I deploy/recall_eval.py            # print table
    .venv/bin/python -I deploy/recall_eval.py --json out.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import psycopg.rows  # noqa: E402
from psycopg_pool import AsyncConnectionPool  # noqa: E402

from shared.embeddings.implementations import (  # noqa: E402
    CachingEmbedder,
    FakeReranker,
    LocalBgeM3,
)
from shared.types import RetrievalRequest  # noqa: E402

import memory.retrieval as R  # noqa: E402

SCOPE = os.environ.get("CURLYOS_SCOPE", "user:usr_hiten")
DSN = os.environ["CURLYOS_DATABASE_URL"]

# Deliberately spans the domains the graph is supposed to connect. Fixed, so
# runs are comparable; add queries only by appending.
QUERIES = [
    "what am I building with curlyos",
    "which tracks have I released as crazymage",
    "ableton live setup and remote scripts",
    "express browser product design work",
    "my liver and health markers",
    "dehradun live set venues",
    "claude code delegation workflow",
    "3d audio visual project christmas",
    "ballam ai automation business",
    "figma design system work",
    "what models does hermes route to",
    "neti neti meditation",
]


async def run_query(pool, embedder, q: str) -> dict:
    calls: list[list[dict]] = []
    orig = R._graph_expand

    async def spy(*a, **k):
        out = await orig(*a, **k)
        calls.append(out)
        return out

    R._graph_expand = spy
    t0 = time.monotonic()
    try:
        result = await R.retrieve(
            RetrievalRequest(query=q, scope=SCOPE, mode="deep", token_budget=50000),
            pool=pool,
            embedder=embedder,
            reranker=FakeReranker(),
        )
    finally:
        R._graph_expand = orig
    ms = (time.monotonic() - t0) * 1000

    items = list(result.items)
    graph_ids = [i["id"] for c in calls for i in c]
    top10 = {i.id for i in items[:10]}

    per_ep: dict[str, int] = {}
    for it in items:
        ep = getattr(it, "source_episode_id", None) or "?"
        per_ep[ep] = per_ep.get(ep, 0) + 1

    return {
        "query": q,
        "n": len(items),
        "episodes": len(per_ep),
        "max_ep": max(per_ep.values()) if per_ep else 0,
        "graph": len(graph_ids),
        "graph_unique": len(set(graph_ids)),
        "g_in_top10": len(set(graph_ids) & top10),
        "ids": [i.id for i in items],
        "ms": round(ms),
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None, help="write raw rows here")
    args = ap.parse_args()

    pool = AsyncConnectionPool(
        DSN,
        min_size=1,
        max_size=4,
        kwargs={"row_factory": psycopg.rows.tuple_row},
        open=False,
    )
    await pool.open()
    embedder = CachingEmbedder(LocalBgeM3())
    await embedder.embed_single("warmup")

    rows = []
    for q in QUERIES:
        rows.append(await run_query(pool, embedder, q))

    # chatter and duplicate-rate need the DB, so measure them in one pass
    all_ids = [i for r in rows for i in r["ids"]]
    chat = set()
    if all_ids:
        async with pool.connection() as c:
            cur = await c.execute(
                "SELECT id FROM memories WHERE is_chatter AND id = ANY(%s)", [all_ids]
            )
            chat = {r[0] for r in await cur.fetchall()}
    for r in rows:
        r["chatter"] = sum(1 for i in r["ids"] if i in chat)

    hdr = f"{'query':<34}{'n':>4}{'eps':>5}{'maxep':>6}{'graph':>7}{'g<10':>6}{'chat':>5}{'ms':>6}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r['query'][:33]:<34}{r['n']:>4}{r['episodes']:>5}{r['max_ep']:>6}"
              f"{r['graph']:>7}{r['g_in_top10']:>6}{r['chatter']:>5}{r['ms']:>6}")

    def agg(k: str) -> float:
        return sum(r[k] for r in rows) / len(rows)

    print("-" * len(hdr))
    print(f"{'mean':<34}{agg('n'):>4.0f}{agg('episodes'):>5.0f}{agg('max_ep'):>6.1f}"
          f"{agg('graph'):>7.1f}{agg('g_in_top10'):>6.2f}{agg('chatter'):>5.1f}{agg('ms'):>6.0f}")
    print(f"\ngraph items that reached the top 10: "
          f"{sum(r['g_in_top10'] for r in rows)} of {sum(r['graph'] for r in rows)}")

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(rows, fh, indent=1)
        print(f"raw rows → {args.json}")

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
