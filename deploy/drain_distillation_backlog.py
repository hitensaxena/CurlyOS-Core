#!/usr/bin/env python3
"""Drain the episode distillation backlog.

Runs only the SUMMARIZE pass, in a loop, so each batch of episodes is distilled
without paying for dedup/decay/recombine every time. Facts are embedded inline
and deduplicated by content hash, so re-running is safe.

Usage:
    .venv/bin/python deploy/drain_distillation_backlog.py
    .venv/bin/python deploy/drain_distillation_backlog.py --max-runs 5
    .venv/bin/python deploy/drain_distillation_backlog.py --sleep 0
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")


async def _backlog(pool, scope: str) -> int:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM episodes e WHERE e.scope = %s "
                "AND COALESCE(e.source_ref, '') NOT LIKE 'jrnl:%%' "
                "AND NOT EXISTS (SELECT 1 FROM memories m "
                "                WHERE m.source_episode_id = e.id AND m.scope = %s)",
                (scope, scope),
            )
            return int((await cur.fetchone())[0])


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", default=None)
    parser.add_argument("--max-runs", type=int, default=100)
    parser.add_argument("--sleep", type=float, default=2.0)
    args = parser.parse_args()

    import psycopg.rows

    import api_server
    from memory.consolidation import _pass_summarize

    scope = args.scope or api_server.SCOPE
    pool = await api_server._get_async_pool(row_factory=psycopg.rows.tuple_row)
    embedder = await api_server.get_shared_embedder()
    publisher = api_server._make_publisher_sync()
    llm_client, llm_model = api_server._make_llm_client()

    total_episodes = 0
    total_facts = 0
    total_errors = 0
    runs = 0
    while runs < args.max_runs:
        backlog = await _backlog(pool, scope)
        print(f"backlog={backlog}", flush=True)
        if backlog == 0:
            break

        result = await _pass_summarize(
            pool, None, embedder, publisher, scope, llm_client, llm_model,
        )
        runs += 1
        episodes = int(result.get("episodes_processed", 0))
        facts = int(result.get("facts_extracted", 0))
        errors = int(result.get("errors", 0))
        total_episodes += episodes
        total_facts += facts
        total_errors += errors
        print(
            f"run {runs}: episodes={episodes} facts={facts} "
            f"errors={errors} skipped_no_llm={result.get('skipped_no_llm', 0)}",
            flush=True,
        )
        if episodes == 0 and errors == 0:
            print("no progress; stopping", flush=True)
            break
        if args.sleep:
            await asyncio.sleep(args.sleep)

    print(
        f"done: runs={runs} episodes={total_episodes} "
        f"facts={total_facts} errors={total_errors}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
