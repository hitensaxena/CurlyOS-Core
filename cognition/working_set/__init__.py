"""Working memory: a small salience-ranked set of memories kept warm.

The set is recomputed from recency, active goals and canonical identity. It is
advisory: recall uses it as a bounded boost, and only for candidates the query
already retrieved. Nothing is injected into unrelated queries, and pinned rows
are the only entries a refresh never overwrites.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from cognition.text import terms
from shared.events import build_event
from shared.scope import scope_parts

log = logging.getLogger("curlyos.cognition.working_set")

DEFAULT_SIZE = 50
DEFAULT_CANDIDATES = 300
DEFAULT_TTL_HOURS = 24
HALF_LIFE_DAYS = 14.0

class WorkingSetError(ValueError):
    pass


def compute_salience(
    statement: str,
    valid_from: datetime,
    goal_terms: set[str],
    identity_terms: set[str],
    *,
    attention_terms: set[str] | None = None,
    pinned: bool = False,
    now: datetime | None = None,
) -> float:
    """0..1 salience from recency, goals, identity and attention gaps."""
    if pinned:
        return 1.0
    now = now or datetime.now(timezone.utc)
    if valid_from.tzinfo is None:
        valid_from = valid_from.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - valid_from).total_seconds() / 86400.0)
    recency = 0.5 ** (age_days / HALF_LIFE_DAYS)

    text = (statement or "").lower()
    goal_hits = sum(1 for term in goal_terms if term in text)
    goal_score = min(1.0, goal_hits / max(1, min(len(goal_terms), 5)))
    identity_hits = sum(1 for term in identity_terms if term in text)
    identity_score = min(1.0, identity_hits / max(1, min(len(identity_terms), 5)))
    attention_terms = attention_terms or set()
    attention_hits = sum(1 for term in attention_terms if term in text)
    attention_score = min(1.0, attention_hits / max(1, min(len(attention_terms), 5)))

    return round(
        0.50 * recency
        + 0.20 * goal_score
        + 0.15 * identity_score
        + 0.15 * attention_score,
        4,
    )


async def refresh_working_set(
    pool: Any,
    publisher: Any,
    scope: str,
    *,
    size: int = DEFAULT_SIZE,
    candidates: int = DEFAULT_CANDIDATES,
    ttl_hours: int = DEFAULT_TTL_HOURS,
) -> dict:
    """Recompute the set. Pinned rows are preserved untouched."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT title, COALESCE(description, '') FROM goals "
                "WHERE scope = %s AND status = 'active' AND valid_to IS NULL",
                (scope,),
            )
            goal_terms = terms(f"{t} {d}" for t, d in await cur.fetchall())

            await cur.execute(
                "SELECT object FROM identity_facts "
                "WHERE scope = %s AND valid_to IS NULL "
                "AND epistemic_status = 'canonical' AND confidence >= 0.75",
                (scope,),
            )
            identity_terms = terms(row[0] for row in await cur.fetchall())

            # Attention's active gaps name a value or goal that is stated but
            # not being acted on; those terms make related memories warmer.
            await cur.execute(
                "SELECT description FROM alignment_signals "
                "WHERE scope = %s AND valid_to IS NULL",
                (scope,),
            )
            attention_terms = terms(
                subject
                for (description,) in await cur.fetchall()
                for subject in re.findall(r"'([^']+)'", description or "")
            )

            await cur.execute(
                "SELECT id, statement, valid_from FROM memories "
                "WHERE scope = %s AND valid_to IS NULL "
                "AND epistemic_status IN ('canonical', 'belief') "
                "AND valid_from >= now() - interval '30 days' "
                "ORDER BY valid_from DESC LIMIT %s",
                (scope, candidates),
            )
            rows = await cur.fetchall()

            await cur.execute(
                "SELECT memory_id FROM working_set WHERE scope = %s AND pinned",
                (scope,),
            )
            pinned = {row[0] for row in await cur.fetchall()}

    scored = [
        (
            mem_id,
            compute_salience(
                statement, valid_from, goal_terms, identity_terms,
                attention_terms=attention_terms,
                pinned=mem_id in pinned,
            ),
        )
        for mem_id, statement, valid_from in rows
    ]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    chosen = [(mem_id, sal) for mem_id, sal in scored[:size] if mem_id not in pinned]

    result = {
        "scope": scope,
        "candidates": len(rows),
        "selected": len(chosen),
        "pinned": len(pinned),
        "expired_deleted": 0,
        "goal_terms": len(goal_terms),
        "identity_terms": len(identity_terms),
        "attention_terms": len(attention_terms),
    }
    subject = stamped = None
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                if chosen:
                    await cur.executemany(
                        "INSERT INTO working_set "
                        "(scope, memory_id, salience, pinned, reason, added_at, refreshed_at, expires_at) "
                        "VALUES (%s, %s, %s, false, 'salience', now(), now(), "
                        "        now() + make_interval(hours => %s)) "
                        "ON CONFLICT (scope, memory_id) DO UPDATE "
                        "SET salience = EXCLUDED.salience, refreshed_at = now(), "
                        "    expires_at = EXCLUDED.expires_at, reason = EXCLUDED.reason "
                        "WHERE NOT working_set.pinned",
                        [(scope, mem_id, sal, ttl_hours) for mem_id, sal in chosen],
                    )
                await cur.execute(
                    "UPDATE working_set SET refreshed_at = now() "
                    "WHERE scope = %s AND pinned",
                    (scope,),
                )
                await cur.execute(
                    "DELETE FROM working_set WHERE scope = %s AND NOT pinned "
                    "AND expires_at IS NOT NULL AND expires_at < now()",
                    (scope,),
                )
                result["expired_deleted"] = cur.rowcount or 0
                try:
                    ev = build_event(
                        short_type="cognition.working_set.refreshed",
                        subject=scope,
                        scope=scope_parts(scope),
                        data=result,
                        actor="agent:working_set",
                        source="curlyos-core/cognition",
                    )
                    _stored, subject, stamped = await publisher.stage(ev, conn)
                except Exception:
                    # The set is already persisted; event failure must not fail it.
                    log.warning("working-set refresh event failed", exc_info=True)

    if subject is not None:
        try:
            await publisher.emit(subject, stamped)
        except Exception:
            log.warning("working-set event emit failed", exc_info=True)
    return result


async def get_working_set(pool: Any, scope: str, limit: int = 20) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT ws.memory_id, ws.salience, ws.pinned, ws.reason, "
                "       ws.refreshed_at, ws.expires_at, m.statement, m.kind, "
                "       m.source_episode_id, m.valid_from "
                "FROM working_set ws JOIN memories m ON m.id = ws.memory_id "
                "WHERE ws.scope = %s AND m.valid_to IS NULL "
                "AND (ws.expires_at IS NULL OR ws.expires_at > now()) "
                "ORDER BY ws.pinned DESC, ws.salience DESC LIMIT %s",
                (scope, limit),
            )
            return [
                {
                    "memory_id": row[0], "salience": float(row[1]), "pinned": row[2],
                    "reason": row[3], "refreshed_at": row[4], "expires_at": row[5],
                    "statement": row[6], "kind": row[7],
                    "source_episode_id": row[8], "valid_from": row[9],
                }
                for row in await cur.fetchall()
            ]


async def pin_memory(pool: Any, scope: str, memory_id: str, reason: str = "") -> str:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO working_set "
                "(scope, memory_id, salience, pinned, reason, added_at, refreshed_at, expires_at) "
                "SELECT %s, id, 1.0, true, %s, now(), now(), NULL FROM memories "
                "WHERE id = %s AND scope = %s AND valid_to IS NULL "
                "ON CONFLICT (scope, memory_id) DO UPDATE "
                "SET pinned = true, salience = 1.0, reason = EXCLUDED.reason, expires_at = NULL "
                "RETURNING memory_id",
                (scope, reason or "pinned", memory_id, scope),
            )
            row = await cur.fetchone()
    if row is None:
        raise WorkingSetError(f"memory not found in scope: {memory_id}")
    return row[0]


async def unpin_memory(pool: Any, scope: str, memory_id: str) -> bool:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE working_set SET pinned = false, "
                "expires_at = now() + make_interval(hours => %s) "
                "WHERE scope = %s AND memory_id = %s AND pinned",
                (DEFAULT_TTL_HOURS, scope, memory_id),
            )
            return (cur.rowcount or 0) > 0


async def working_set_boost(pool: Any, scope: str) -> dict[str, float]:
    """memory_id → salience for active working-set rows."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT memory_id, salience FROM working_set "
                "WHERE scope = %s AND (expires_at IS NULL OR expires_at > now())",
                (scope,),
            )
            return {row[0]: float(row[1]) for row in await cur.fetchall()}
