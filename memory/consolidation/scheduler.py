"""Consolidation scheduler — runs the memory projection worker on a cadence.

Two modes:
  1. Hermes cron job  — `curlyos-consolidate` runs `run_once()` on schedule
  2. Background thread — embedded in the provider, runs every N minutes

Cadence (from ~/hitenos-architecture/02b-memory-governance.md):
  - interval:  every 30m (baseline)
  - session_close: consolidate on session end
  - nightly_deep: cron "0 3 * * *" (full dedup + summarize + decay)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import threading
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("curlyos.consolidation.scheduler")

# Default intervals (seconds)
BASELINE_INTERVAL = 30 * 60     # 30 minutes
NIGHTLY_INTERVAL = 24 * 60 * 60  # 24 hours (cron handles this, but fallback)


class ConsolidationScheduler:
    """Background consolidation runner.

    Spawns a daemon thread that periodically calls the async
    consolidation worker for all scopes with events.
    """

    def __init__(
        self,
        pool: Any,
        redis: Any,
        embedder: Any,
        publisher: Any,
        interval_seconds: int = BASELINE_INTERVAL,
    ):
        self._pool = pool
        self._redis = redis
        self._embedder = embedder
        self._publisher = publisher
        self._interval = interval_seconds
        self._running = False
        self._thread: threading.Thread | None = None
        self._last_run: float = 0.0

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def last_run_at(self) -> str:
        if not self._last_run:
            return "never"
        return datetime.fromtimestamp(self._last_run, tz=timezone.utc).isoformat()

    def start(self) -> None:
        """Start the background consolidation thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="curlyos-consolidation"
        )
        self._thread.start()
        log.info("Consolidation scheduler started (interval=%ds)", self._interval)

    def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        log.info("Consolidation scheduler stopped")

    def _run_loop(self) -> None:
        """Main loop — runs consolidation on interval."""
        from memory.consolidation import run_once

        while self._running:
            try:
                # Run consolidation for all scopes
                loop = asyncio.new_event_loop()
                try:
                    result = loop.run_until_complete(
                        run_once(
                            self._pool,
                            self._redis,
                            self._embedder,
                            self._publisher,
                            live=True,
                        )
                    )
                    self._last_run = time.time()
                    scopes = result.get("scopes", [])
                    total_processed = sum(s.get("processed", 0) for s in scopes)
                    if total_processed > 0:
                        log.info("Consolidation pass: %d events processed across %d scopes",
                                 total_processed, len(scopes))
                finally:
                    loop.close()
            except Exception as e:
                log.error("Consolidation pass failed: %s", e, exc_info=True)

            # Sleep in small increments so stop() is responsive
            for _ in range(self._interval):
                if not self._running:
                    return
                time.sleep(1)

    def run_once_sync(self, scope: str | None = None, replay: bool = False) -> dict:
        """Run a single consolidation pass synchronously (for cron jobs / manual trigger)."""
        from memory.consolidation import run_once

        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                run_once(
                    self._pool,
                    self._redis,
                    self._embedder,
                    self._publisher,
                    scope=scope,
                    replay=replay,
                    live=True,
                )
            )
            self._last_run = time.time()
            return result
        finally:
            loop.close()


async def start_scheduler(
    pool: Any,
    redis: Any,
    embedder: Any,
    publisher: Any,
    interval_seconds: int = BASELINE_INTERVAL,
) -> ConsolidationScheduler:
    """Create and start the background consolidation scheduler.

    Fast path runs every interval_seconds (default 30 min).
    Deep path is triggered separately via run_deep_path().

    Returns the started ConsolidationScheduler instance.
    """
    scheduler = ConsolidationScheduler(
        pool, redis, embedder, publisher,
        interval_seconds=interval_seconds,
    )
    scheduler.start()
    return scheduler


async def run_fast_path(
    pool: Any,
    redis: Any,
    embedder: Any,
    publisher: Any,
    reranker: Any = None,
    scope: str | None = None,
) -> dict:
    """Run fast consolidation path: process new events + DEDUP + CONFLICT-RESOLVE.

    This is the lightweight path that runs every 15 minutes.
    Only processes new events since last watermark, then runs
    dedup and conflict resolution passes.
    """
    from memory.consolidation import run_consolidation

    log.info("Starting fast consolidation path (scope=%s)", scope)
    result = await run_consolidation(
        pool, redis, embedder, publisher, reranker,
        scope=scope,
        deep=False,
    )
    log.info("Fast consolidation path complete: %s", result)
    return result


async def run_deep_path(
    pool: Any,
    redis: Any,
    embedder: Any,
    publisher: Any,
    reranker: Any = None,
    scope: str | None = None,
) -> dict:
    """Run deep consolidation path: full consolidation pipeline.

    This is the nightly path (3am) that runs all passes:
    DEDUP, MERGE/PROMOTE, CONFLICT-RESOLVE, SUMMARIZE, DECAY, RECOMBINE/INCUBATE.
    """
    from memory.consolidation import run_consolidation

    log.info("Starting deep consolidation path (scope=%s)", scope)
    result = await run_consolidation(
        pool, redis, embedder, publisher, reranker,
        scope=scope,
        deep=True,
    )
    log.info("Deep consolidation path complete: %s", result)
    return result


def run_consolidation_standalone(
    dsn: str | None = None,
    redis_url: str | None = None,
    scope: str | None = None,
    replay: bool = False,
    embedder_type: str = "fake",
) -> dict:
    """Standalone consolidation runner — callable from Hermes cron or CLI.

    Usage:
      # Via hermes cron:
      hermes cron --prompt "Run curlyos memory consolidation"

      # Via CLI:
      python3 -m memory.consolidation.scheduler --scope user:usr_hiten
    """
    from memory.consolidation import run_once
    from shared.events.implementations import PgOnlyPublisher

    dsn = dsn or os.environ.get("CURLYOS_DATABASE_URL", "")
    redis_url = redis_url or os.environ.get("CURLYOS_REDIS_URL", "")

    if not dsn:
        return {"error": "CURLYOS_DATABASE_URL not set"}

    import psycopg

    # Sync pool adapter (reuse from smoketest)
    class SyncPool:
        def __init__(self, d): self._dsn = d
        def connection(self): return _CtxMgr(self._dsn)
    class _CtxMgr:
        def __init__(self, d): self._dsn = d; self._conn = None
        async def __aenter__(self):
            self._conn = psycopg.connect(self._dsn, autocommit=False)
            return _CW(self._conn)
        async def __aexit__(self, *a):
            if self._conn: self._conn.commit(); self._conn.close()
    class _CW:
        def __init__(self, c): self._c = c
        def cursor(self): return _CC(self._c)
    class _CC:
        def __init__(self, c): self._c = c
        async def __aenter__(self): return _CA(self._c.cursor())
        async def __aexit__(self, *a): pass
    class _CA:
        def __init__(self, c): self._c = c
        async def execute(self, q, p=None): self._c.execute(q, p)
        async def fetchone(self): return self._c.fetchone()
        async def fetchall(self): return self._c.fetchall()

    pool = SyncPool(dsn)
    publisher = PgOnlyPublisher()

    # Redis
    if redis_url:
        import redis.asyncio as aioredis
        redis_client = aioredis.from_url(redis_url)
    else:
        # Minimal in-memory fake
        class FakeRedis:
            def __init__(self): self._data = {}
            async def set(self, k, v, nx=False, px=None):
                if nx and k in self._data: return False
                self._data[k] = v; return True
            async def get(self, k): return self._data.get(k)
            async def delete(self, *keys):
                for k in keys: self._data.pop(k, None)
            async def hset(self, n, k, v):
                if n not in self._data: self._data[n] = {}
                self._data[n][k] = v
            async def hdel(self, n, *keys):
                for k in keys: self._data.get(n, {}).pop(k, None)
            async def scan_iter(self, match=None):
                import fnmatch
                for k in self._data:
                    if match is None or fnmatch.fnmatch(k, match): yield k
            async def keys(self, pattern=None):
                import fnmatch
                if pattern: return [k for k in self._data if fnmatch.fnmatch(k, pattern)]
                return list(self._data.keys())
        redis_client = FakeRedis()

    # Embedder
    if embedder_type == "bge-m3":
        from shared.embeddings.implementations import LocalBgeM3
        embedder = LocalBgeM3()
    else:
        from shared.embeddings.implementations import FakeEmbedder
        embedder = FakeEmbedder()

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(
            run_once(pool, redis_client, embedder, publisher, scope=scope, replay=replay, live=True)
        )
        return result
    finally:
        loop.close()


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Run CurlyOS memory consolidation")
    parser.add_argument("--scope", default=None, help="Scope to consolidate (default: all)")
    parser.add_argument("--replay", action="store_true", help="Rebuild projections from scratch")
    parser.add_argument("--embedder", default="fake", choices=["fake", "bge-m3"])
    args = parser.parse_args()

    result = run_consolidation_standalone(
        scope=args.scope, replay=args.replay, embedder_type=args.embedder
    )
    print(json.dumps(result, default=str, indent=2))


# ── Status helper ───────────────────────────────────────────────────────────

async def get_consolidation_status(
    pool: Any,
    scope: str,
) -> dict[str, Any]:
    """Read consolidation status for a scope from projection_watermarks.

    Returns a dict with:
        - last_seq: minimum watermark across all projections
        - last_run_at: most recent updated_at across projections
        - events_pending: count of events above the watermark
        - status: "idle" | "stale" | "empty"
    """
    async with pool.connection() as conn:
        # Read watermark rows for this scope
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT projection, last_seq, updated_at "
                "FROM projection_watermarks "
                "WHERE scope = %s",
                (scope,),
            )
            rows = await cur.fetchall()

        if not rows:
            return {
                "last_seq": 0,
                "last_run_at": None,
                "events_pending": 0,
                "status": "empty",
            }

        last_seq = min(int(r[1]) for r in rows)
        last_run_at = max(r[2] for r in rows)
        last_run_iso = last_run_at.isoformat() if last_run_at else None

        # Count unprocessed events (seq > watermark)
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT COUNT(*) FROM events "
                "WHERE scope = %s AND seq > %s",
                (scope, last_seq),
            )
            pending = (await cur.fetchone())[0]

    # Determine status
    if pending == 0:
        status = "idle"
    else:
        status = "stale"

    return {
        "last_seq": last_seq,
        "last_run_at": last_run_iso,
        "events_pending": pending,
        "status": status,
    }
