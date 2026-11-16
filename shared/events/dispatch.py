"""Event dispatcher: at-least-once delivery to registered handlers.

The `events` table is both the source of truth and the transport (no broker).
Each handler keeps its own watermark in `event_dispatch_watermarks`, so one
slow or failing handler cannot block another. Delivery is at-least-once: a
handler that fails mid-batch keeps the watermark at the last success and the
failed event is retried on the next poll. Handlers MUST be idempotent.

New handlers start at the current max seq by default; pass ``backfill=True``
to replay history.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

from shared import metrics
from shared.events import full_type, short_type

log = logging.getLogger("curlyos.events.dispatch")


@dataclass(frozen=True)
class Handler:
    name: str
    fn: Callable[[dict], Awaitable[None]]
    # Short types this handler consumes; None means every type.
    types: frozenset[str] | None = None

    def matches(self, event_short_type: str) -> bool:
        return self.types is None or event_short_type in self.types


_REGISTRY: dict[str, Handler] = {}


def register(handler: Handler) -> Handler:
    if not handler.name:
        raise ValueError("handler name is required")
    if handler.name in _REGISTRY:
        raise ValueError(f"handler already registered: {handler.name}")
    _REGISTRY[handler.name] = handler
    return handler


def registered() -> list[Handler]:
    return sorted(_REGISTRY.values(), key=lambda h: h.name)


def register_default_handlers() -> None:
    """Idempotent registration of the built-in handlers."""
    if "metrics" in _REGISTRY:
        return

    async def _metrics(event: dict) -> None:
        metrics.incr("event.dispatched")
        metrics.incr(f"event.dispatched.{event['short_type']}")

    register(Handler("metrics", _metrics))


async def _read_watermark(pool: Any, scope: str, name: str) -> tuple[int, int] | None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT last_seq, error_count FROM event_dispatch_watermarks "
                "WHERE scope = %s AND handler = %s",
                (scope, name),
            )
            row = await cur.fetchone()
    return (int(row[0]), int(row[1])) if row else None


async def _max_seq(pool: Any, scope: str) -> int:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT coalesce(max(seq), 0) FROM events WHERE scope = %s",
                (scope,),
            )
            return int((await cur.fetchone())[0])


async def _save_watermark(
    pool: Any, scope: str, name: str, last_seq: int,
    error: str | None, error_count: int,
) -> None:
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO event_dispatch_watermarks "
                    "(scope, handler, last_seq, last_error, error_count, updated_at) "
                    "VALUES (%s, %s, %s, %s, %s, now()) "
                    "ON CONFLICT (scope, handler) DO UPDATE SET "
                    "  last_seq = EXCLUDED.last_seq, "
                    "  last_error = EXCLUDED.last_error, "
                    "  error_count = EXCLUDED.error_count, "
                    "  updated_at = now()",
                    (scope, name, last_seq, error, error_count),
                )


async def _fetch(
    pool: Any, scope: str, after: int,
    types: frozenset[str] | None, batch: int,
) -> list[tuple]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            if types is None:
                await cur.execute(
                    "SELECT seq, type, subject, data, created_at FROM events "
                    "WHERE scope = %s AND seq > %s ORDER BY seq LIMIT %s",
                    (scope, after, batch),
                )
            else:
                await cur.execute(
                    "SELECT seq, type, subject, data, created_at FROM events "
                    "WHERE scope = %s AND seq > %s AND type = ANY(%s) "
                    "ORDER BY seq LIMIT %s",
                    (scope, after, [full_type(t) for t in types], batch),
                )
            return list(await cur.fetchall())


def _envelope(row: tuple, scope: str) -> dict:
    seq, ev_type, subject, data, created_at = row
    return {
        "seq": int(seq),
        "type": ev_type,
        "short_type": short_type(ev_type),
        "subject": subject,
        "data": data,
        "created_at": created_at.isoformat() if isinstance(created_at, datetime) else created_at,
        "scope": scope,
    }


async def dispatch_once(
    pool: Any,
    scope: str,
    *,
    batch: int = 100,
    handler_list: list[Handler] | None = None,
    backfill: bool = False,
) -> dict:
    """One pass over every handler. Returns counters, never raises per handler."""
    handlers = handler_list if handler_list is not None else registered()
    result = {
        "handlers": len(handlers),
        "dispatched": 0,
        "errors": 0,
        "skipped_initial": 0,
    }

    for handler in handlers:
        watermark = await _read_watermark(pool, scope, handler.name)
        if watermark is None and not backfill:
            # Start new handlers at "now"; history is opt-in.
            await _save_watermark(pool, scope, handler.name, await _max_seq(pool, scope), None, 0)
            result["skipped_initial"] += 1
            continue

        last_seq, error_count = watermark if watermark else (0, 0)
        rows = await _fetch(pool, scope, last_seq, handler.types, batch)
        if not rows:
            if error_count:
                await _save_watermark(pool, scope, handler.name, last_seq, None, 0)
            continue

        last_ok = last_seq
        failure: str | None = None
        for row in rows:
            event = _envelope(row, scope)
            if not handler.matches(event["short_type"]):
                last_ok = event["seq"]
                continue
            try:
                await handler.fn(event)
                last_ok = event["seq"]
                result["dispatched"] += 1
            except Exception as exc:
                failure = f"{type(exc).__name__}: {exc}"[:500]
                metrics.incr("event.dispatch.errors")
                log.warning("handler %s failed at seq=%s: %s", handler.name, row[0], failure)
                break

        if failure:
            result["errors"] += 1
            await _save_watermark(pool, scope, handler.name, last_ok, failure, error_count + 1)
        else:
            await _save_watermark(pool, scope, handler.name, last_ok, None, 0)

    return result


async def dispatch_status(pool: Any, scope: str) -> dict:
    max_seq = await _max_seq(pool, scope)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT handler, last_seq, last_error, error_count, updated_at "
                "FROM event_dispatch_watermarks WHERE scope = %s ORDER BY handler",
                (scope,),
            )
            rows = await cur.fetchall()
    return {
        "scope": scope,
        "max_seq": max_seq,
        "handlers": [
            {
                "handler": r[0], "last_seq": int(r[1]), "lag": max_seq - int(r[1]),
                "last_error": r[2], "error_count": int(r[3]),
                "updated_at": r[4].isoformat() if isinstance(r[4], datetime) else r[4],
            }
            for r in rows
        ],
    }


class Dispatcher:
    """Background loop around dispatch_once."""

    def __init__(
        self, pool_factory: Callable[[], Awaitable[Any]],
        scope: str, *, interval: float = 2.0, batch: int = 100,
    ) -> None:
        self._pool_factory = pool_factory
        self._scope = scope
        self._interval = interval
        self._batch = batch
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            delay = self._interval
            try:
                pool = await self._pool_factory()
                result = await dispatch_once(pool, self._scope, batch=self._batch)
                if result["dispatched"] >= self._batch:
                    delay = 0.1
                metrics.incr("event.dispatch.ticks")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("dispatcher tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
