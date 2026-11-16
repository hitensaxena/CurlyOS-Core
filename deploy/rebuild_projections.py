#!/usr/bin/env python3
"""Report or rebuild projections from the event log.

Safe by default: without --apply it only reports what is missing. It never
deletes rows; an extra row is reported, not removed.

Usage:
    .venv/bin/python deploy/rebuild_projections.py --projection graph
    .venv/bin/python deploy/rebuild_projections.py --projection all --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")


async def main() -> int:
    import psycopg.rows

    import api_server
    from memory.rebuild import (
        event_coverage, rebuild_graph, rebuild_identity, rebuild_memories,
    )

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--projection",
        choices=("graph", "identity", "memories", "coverage", "all"),
        default="coverage",
    )
    parser.add_argument("--scope", default=api_server.SCOPE)
    parser.add_argument("--apply", action="store_true",
                        help="write repairs; default is a dry run")
    parser.add_argument(
        "--since", default=None,
        help="only consider events at or after this ISO timestamp. Use it to "
             "make untracked deletions an alarm: history before deletions were "
             "evented will always report gaps that are not new.",
    )
    args = parser.parse_args()

    since = None
    if args.since:
        from datetime import datetime, timezone

        since = datetime.fromisoformat(args.since)
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)

    pool = await api_server._get_async_pool(row_factory=psycopg.rows.tuple_row)
    dry_run = not args.apply
    result: dict = {"scope": args.scope, "dry_run": dry_run}
    if since:
        result["since"] = since.isoformat()

    if args.projection in ("coverage", "all"):
        result["coverage"] = await event_coverage(pool, args.scope)
    if args.projection in ("graph", "all"):
        result["graph"] = await rebuild_graph(
            pool, args.scope, dry_run=dry_run, since=since)
    if args.projection in ("identity", "all"):
        result["identity"] = await rebuild_identity(pool, args.scope, dry_run=dry_run)
    if args.projection in ("memories", "all"):
        result["memories"] = await rebuild_memories(pool, args.scope, dry_run=dry_run)

    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
