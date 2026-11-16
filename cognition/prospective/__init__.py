"""Prospective memory: intentions surfaced at the right time.

Decision reviews are the first real source: decisions.review_at stored a
reminder that nothing fired. A five-minute scan materializes due reviews as
intentions, surfaces each one exactly once, and leaves it visible in the due
endpoint until it is completed, dismissed or snoozed.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from shared.events import build_event
from shared.scope import scope_parts
from shared.types.ulid import mint

log = logging.getLogger("curlyos.cognition.prospective")


class IntentionError(ValueError):
    pass


async def _stage(publisher: Any, conn: Any, short_type: str, subject: str,
                 scope: str, data: dict) -> tuple[str, str, dict] | None:
    if publisher is None:
        return None
    ev = build_event(
        short_type=short_type,
        subject=subject,
        scope=scope_parts(scope),
        data=data,
        actor="agent:intentions",
        source="curlyos-core/cognition",
    )
    return await publisher.stage(ev, conn)


async def create_intention(
    pool: Any,
    publisher: Any,
    scope: str,
    text: str,
    *,
    due_at: datetime | None = None,
    context: str | None = None,
    source: str = "manual",
    source_ref: str | None = None,
) -> dict:
    if not text.strip():
        raise IntentionError("text is required")
    intention_id = mint("int")
    subject = stamped = None
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO intentions "
                    "(id, scope, text, due_at, context, source, source_ref) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (scope, source, source_ref) "
                    "WHERE source_ref IS NOT NULL DO NOTHING "
                    "RETURNING id",
                    (intention_id, scope, text.strip(), due_at, context, source, source_ref),
                )
                inserted = await cur.fetchone()
                if inserted is None and source_ref is not None:
                    await cur.execute(
                        "SELECT id FROM intentions "
                        "WHERE scope = %s AND source = %s AND source_ref = %s",
                        (scope, source, source_ref),
                    )
                    existing = await cur.fetchone()
                    return {"id": existing[0] if existing else None, "created": False}
                staged = await _stage(
                    publisher, conn, "cognition.intention.created", intention_id,
                    scope, {"intention_id": intention_id, "source": source,
                            "source_ref": source_ref},
                )
                if staged:
                    _, subject, stamped = staged

    if subject is not None:
        try:
            await publisher.emit(subject, stamped)
        except Exception:
            log.warning("intention created event emit failed", exc_info=True)
    return {"id": intention_id, "created": True}


async def sync_decision_reviews(pool: Any, publisher: Any, scope: str) -> int:
    """Materialize due decision reviews as intentions. Returns new count."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, title, chosen, review_at FROM decisions "
                "WHERE scope = %s AND outcome IS NULL "
                "AND review_at IS NOT NULL AND review_at <= now()",
                (scope,),
            )
            decisions = await cur.fetchall()

    created = 0
    for decision_id, title, chosen, review_at in decisions:
        result = await create_intention(
            pool, publisher, scope,
            f"Review decision: {title}",
            due_at=review_at,
            context=chosen,
            source="decision_review",
            source_ref=decision_id,
        )
        if result.get("created"):
            created += 1
    return created


async def due_intentions(pool: Any, scope: str, limit: int = 50) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, text, due_at, context, status, source, source_ref, "
                "       created_at, surfaced_at, snooze_until "
                "FROM intentions "
                "WHERE scope = %s AND status IN ('pending', 'surfaced') "
                "AND due_at IS NOT NULL AND due_at <= now() "
                "AND (snooze_until IS NULL OR snooze_until <= now()) "
                "ORDER BY due_at LIMIT %s",
                (scope, limit),
            )
            return [
                {
                    "id": r[0], "text": r[1], "due_at": r[2], "context": r[3],
                    "status": r[4], "source": r[5], "source_ref": r[6],
                    "created_at": r[7], "surfaced_at": r[8], "snooze_until": r[9],
                }
                for r in await cur.fetchall()
            ]


async def surface_due_intentions(pool: Any, publisher: Any, scope: str) -> int:
    """Mark due pending intentions surfaced and emit one event each."""
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE intentions SET status = 'surfaced', surfaced_at = now() "
                    "WHERE scope = %s AND status = 'pending' "
                    "AND due_at IS NOT NULL AND due_at <= now() "
                    "AND (snooze_until IS NULL OR snooze_until <= now()) "
                    "RETURNING id, text",
                    (scope,),
                )
                rows = await cur.fetchall()
                for intention_id, text in rows:
                    await _stage(
                        publisher, conn, "cognition.intention.surfaced",
                        intention_id, scope,
                        {"intention_id": intention_id, "text": text[:300]},
                    )
    return len(rows)


async def list_intentions(
    pool: Any, scope: str, status: str | None = None, limit: int = 50,
) -> list[dict]:
    where = "scope = %s"
    params: list[Any] = [scope]
    if status:
        where += " AND status = %s"
        params.append(status)
    params.append(limit)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, text, due_at, context, status, source, source_ref, "
                "       created_at, surfaced_at, completed_at, snooze_until "
                f"FROM intentions WHERE {where} ORDER BY created_at DESC LIMIT %s",
                tuple(params),
            )
            return [
                {
                    "id": r[0], "text": r[1], "due_at": r[2], "context": r[3],
                    "status": r[4], "source": r[5], "source_ref": r[6],
                    "created_at": r[7], "surfaced_at": r[8],
                    "completed_at": r[9], "snooze_until": r[10],
                }
                for r in await cur.fetchall()
            ]


async def _close(pool: Any, publisher: Any, scope: str, intention_id: str,
                 status: str) -> bool:
    subject = stamped = None
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE intentions SET status = %s, completed_at = now() "
                    "WHERE id = %s AND scope = %s AND status IN ('pending', 'surfaced') "
                    "RETURNING id",
                    (status, intention_id, scope),
                )
                row = await cur.fetchone()
                if row is None:
                    return False
                staged = await _stage(
                    publisher, conn, "cognition.intention.updated", intention_id,
                    scope, {"intention_id": intention_id, "status": status},
                )
                if staged:
                    _, subject, stamped = staged
    if subject is not None:
        try:
            await publisher.emit(subject, stamped)
        except Exception:
            log.warning("intention update event emit failed", exc_info=True)
    return True


async def complete_intention(pool: Any, publisher: Any, scope: str, intention_id: str) -> bool:
    return await _close(pool, publisher, scope, intention_id, "done")


async def dismiss_intention(pool: Any, publisher: Any, scope: str, intention_id: str) -> bool:
    return await _close(pool, publisher, scope, intention_id, "dismissed")


async def snooze_intention(
    pool: Any, publisher: Any, scope: str, intention_id: str, until: datetime,
) -> bool:
    subject = stamped = None
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE intentions SET status = 'pending', surfaced_at = NULL, "
                    "snooze_until = %s "
                    "WHERE id = %s AND scope = %s AND status IN ('pending', 'surfaced') "
                    "RETURNING id",
                    (until, intention_id, scope),
                )
                row = await cur.fetchone()
                if row is None:
                    return False
                staged = await _stage(
                    publisher, conn, "cognition.intention.updated", intention_id,
                    scope, {"intention_id": intention_id, "status": "snoozed",
                            "snooze_until": until.isoformat()},
                )
                if staged:
                    _, subject, stamped = staged
    if subject is not None:
        try:
            await publisher.emit(subject, stamped)
        except Exception:
            log.warning("intention snooze event emit failed", exc_info=True)
    return True
