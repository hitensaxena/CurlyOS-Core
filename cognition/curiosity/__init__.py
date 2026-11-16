"""Curiosity: ask about gaps instead of inventing answers.

Three grounded sources, no LLM and no autonomous research:

- active goals with no recent capture mentioning their distinctive terms;
- recent knowledge entities with almost no graph connections;
- identity facts still sitting at hypothesis confidence.

Questions are stored once per (scope, basis, subject). The artist can answer or
dismiss them; neither action writes a fact.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from cognition.text import terms
from shared.events import build_event
from shared.scope import scope_parts
from shared.types.ulid import mint

log = logging.getLogger("curlyos.cognition.curiosity")

GOAL_WINDOW_DAYS = 14
MAX_PER_BASIS = 5

# Graph extraction sometimes creates "entities" out of record titles and ids,
# e.g. "127 reference link note (mem_01M7...)". Those are not concepts worth
# asking about.
_ID_PATTERN = re.compile(
    r"\b(?:mem|epi|int|cur|ctr|goal|dec|out|idf|ent|run|aln)_[A-Z0-9]{8,}"
)


async def _goal_gaps(pool: Any, scope: str) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, title, COALESCE(description, '') FROM goals "
                "WHERE scope = %s AND status = 'active' AND valid_to IS NULL",
                (scope,),
            )
            goals = await cur.fetchall()
            await cur.execute(
                "SELECT statement FROM memories "
                "WHERE scope = %s AND valid_to IS NULL "
                "AND valid_from >= now() - interval '14 days' "
                "ORDER BY valid_from DESC LIMIT 500",
                (scope,),
            )
            recent = " ".join(
                (row[0] or "").lower() for row in await cur.fetchall()
            )

    out: list[dict] = []
    for goal_id, title, description in goals:
        distinctive = terms([title, description])
        if not distinctive or any(term in recent for term in distinctive):
            continue
        out.append({
            "basis": "goal_gap",
            "subject": goal_id,
            "question": (
                f"Goal '{title}' has no recent captures in {GOAL_WINDOW_DAYS} days. "
                "What is its current state?"
            ),
            "evidence": {"goal_id": goal_id, "title": title,
                         "window_days": GOAL_WINDOW_DAYS},
        })
        if len(out) >= MAX_PER_BASIS:
            break
    return out


async def _entity_gaps(pool: Any, scope: str) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT e.id, e.name, "
                "  (SELECT count(*) FROM knowledge_edges ke WHERE ke.src_entity_id = e.id) "
                "  + (SELECT count(*) FROM knowledge_edges ke WHERE ke.dst_entity_id = e.id) "
                "  AS degree "
                "FROM knowledge_entities e "
                "WHERE e.scope = %s AND e.valid_to IS NULL "
                "ORDER BY e.created_at DESC LIMIT 300",
                (scope,),
            )
            rows = await cur.fetchall()

    out: list[dict] = []
    for entity_id, name, degree in rows:
        clean = (name or "").strip()
        if degree is None or degree > 1 or len(clean) < 3 or not re.search(r"[A-Za-z]", clean):
            continue
        if _ID_PATTERN.search(clean) or len(clean.split()) > 8:
            continue
        out.append({
            "basis": "entity_gap",
            "subject": entity_id,
            "question": (
                f"What do you know about '{clean}'? It has only {degree} "
                "graph connection" + ("" if degree == 1 else "s") + "."
            ),
            "evidence": {"entity_id": entity_id, "name": clean, "degree": int(degree)},
        })
        if len(out) >= MAX_PER_BASIS:
            break
    return out


async def _hypothesis_gaps(pool: Any, scope: str) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, predicate, object, confidence FROM identity_facts "
                "WHERE scope = %s AND valid_to IS NULL "
                "AND epistemic_status = 'hypothesis' "
                "ORDER BY valid_from ASC LIMIT %s",
                (scope, MAX_PER_BASIS),
            )
            rows = await cur.fetchall()

    return [
        {
            "basis": "hypothesis_gap",
            "subject": fact_id,
            "question": (
                f"Should the identity claim '{predicate} = {obj}' "
                f"(confidence {float(confidence):.2f}) be confirmed or dropped?"
            ),
            "evidence": {"identity_fact_id": fact_id, "predicate": predicate,
                         "object": obj, "confidence": float(confidence)},
        }
        for fact_id, predicate, obj, confidence in rows
    ]


async def scan_curiosity(
    pool: Any, publisher: Any, scope: str,
) -> dict:
    goal_gaps = await _goal_gaps(pool, scope)
    entity_gaps = await _entity_gaps(pool, scope)
    hypothesis_gaps = await _hypothesis_gaps(pool, scope)
    candidates = goal_gaps + entity_gaps + hypothesis_gaps

    inserted = 0
    subject = stamped = None
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                for candidate in candidates:
                    await cur.execute(
                        "INSERT INTO curiosity_questions "
                        "(id, scope, question, basis, subject, evidence) "
                        "VALUES (%s, %s, %s, %s, %s, %s::jsonb) "
                        "ON CONFLICT (scope, basis, subject) DO NOTHING "
                        "RETURNING id",
                        (
                            mint("cur"), scope, candidate["question"],
                            candidate["basis"], candidate["subject"],
                            json.dumps(candidate["evidence"]),
                        ),
                    )
                    if await cur.fetchone():
                        inserted += 1
                if inserted:
                    ev = build_event(
                        short_type="cognition.curiosity.detected",
                        subject=scope,
                        scope=scope_parts(scope),
                        data={"inserted": inserted,
                              "goal_gaps": len(goal_gaps),
                              "entity_gaps": len(entity_gaps),
                              "hypothesis_gaps": len(hypothesis_gaps)},
                        actor="agent:curiosity",
                        source="curlyos-core/cognition",
                    )
                    _stored, subject, stamped = await publisher.stage(ev, conn)

    if subject is not None:
        try:
            await publisher.emit(subject, stamped)
        except Exception:
            log.warning("curiosity detected event emit failed", exc_info=True)

    return {
        "scope": scope,
        "goal_gaps": len(goal_gaps),
        "entity_gaps": len(entity_gaps),
        "hypothesis_gaps": len(hypothesis_gaps),
        "inserted": inserted,
    }


async def list_questions(
    pool: Any, scope: str, status: str = "open", limit: int = 50,
) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, question, basis, subject, evidence, status, "
                "       created_at, resolved_at, resolved_by "
                "FROM curiosity_questions "
                "WHERE scope = %s AND status = %s "
                "ORDER BY created_at DESC LIMIT %s",
                (scope, status, limit),
            )
            return [
                {
                    "id": r[0], "question": r[1], "basis": r[2],
                    "subject": r[3], "evidence": r[4], "status": r[5],
                    "created_at": r[6], "resolved_at": r[7], "resolved_by": r[8],
                }
                for r in await cur.fetchall()
            ]


async def _resolve(
    pool: Any, publisher: Any, scope: str, question_id: str,
    status: str, resolved_by: str, note: str = "",
) -> bool:
    subject = stamped = None
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE curiosity_questions "
                    "SET status = %s, resolved_at = now(), resolved_by = %s, "
                    "    evidence = evidence || jsonb_build_object('note', %s::text) "
                    "WHERE id = %s AND scope = %s AND status = 'open' "
                    "RETURNING id",
                    (status, resolved_by, note, question_id, scope),
                )
                row = await cur.fetchone()
                if row is None:
                    return False
                ev = build_event(
                    short_type="cognition.curiosity.resolved",
                    subject=question_id,
                    scope=scope_parts(scope),
                    data={"question_id": question_id, "status": status,
                          "resolved_by": resolved_by},
                    actor=f"user:{resolved_by}",
                    source="curlyos-core/cognition",
                )
                _stored, subject, stamped = await publisher.stage(ev, conn)

    if subject is not None:
        try:
            await publisher.emit(subject, stamped)
        except Exception:
            log.warning("curiosity resolved event emit failed", exc_info=True)
    return True


async def dismiss_question(
    pool: Any, publisher: Any, scope: str, question_id: str, note: str = "",
) -> bool:
    return await _resolve(pool, publisher, scope, question_id, "dismissed", "user", note)


async def answer_question(
    pool: Any, publisher: Any, scope: str, question_id: str, note: str = "",
) -> bool:
    return await _resolve(pool, publisher, scope, question_id, "answered", "user", note)
