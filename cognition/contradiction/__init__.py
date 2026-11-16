"""Contradiction detection and approval-gated belief revision.

The scan proposes; it never edits a memory or identity fact. Two sources:

1. Identity predicates with more than one active canonical value (legacy or
   out-of-band writes). Deterministic, no LLM.
2. Recent memories whose embeddings are similar but not duplicates, classified
   by a fast LLM as duplicate / contradiction / refinement / unrelated.

A human calls resolve with an action. Only then is anything invalidated.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from shared.events import build_event
from shared.llm import json_records
from shared.scope import scope_parts
from shared.types.ulid import mint

log = logging.getLogger("curlyos.cognition.contradiction")

RESOLVE_ACTIONS = ("supersede_left", "supersede_right", "keep_both", "dismiss")

_CLASSIFY_SYSTEM = (
    "You compare pairs of personal facts from one person's memory. "
    "Return ONLY a JSON array. For each pair return "
    '{"index": <int>, "relation": "duplicate"|"contradiction"|"refinement"|"unrelated", '
    '"reason": "<one short sentence>"}. '
    "contradiction means both cannot be true at the same time; "
    "refinement means one adds detail without conflicting; "
    "duplicate means they state the same fact; unrelated means no meaningful relation. "
    "Judge only the two texts. Do not use outside knowledge."
)


class ContradictionError(ValueError):
    pass


def _classify_prompt(pairs: list[tuple[str, str]]) -> str:
    lines = ["Classify these pairs:"]
    for i, (left, right) in enumerate(pairs, 1):
        lines.append(f"{i}. A: {left[:400]}")
        lines.append(f"   B: {right[:400]}")
    return "\n".join(lines)


async def detect_identity_conflicts(pool: Any, scope: str) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT predicate, "
                "       array_agg(id ORDER BY valid_from DESC) AS ids, "
                "       array_agg(object ORDER BY valid_from DESC) AS objects "
                "FROM identity_facts "
                "WHERE scope = %s AND valid_to IS NULL AND epistemic_status = 'canonical' "
                "GROUP BY predicate HAVING count(*) > 1",
                (scope,),
            )
            rows = await cur.fetchall()

    candidates: list[dict] = []
    for predicate, ids, objects in rows:
        keeper = ids[0]
        for victim in ids[1:]:
            candidates.append({
                "scope": scope,
                "kind": "identity_identity",
                "left_id": victim,
                "right_id": keeper,
                "similarity": None,
                "reason": f"multiple active canonical values for predicate {predicate!r}",
                "proposed_action": "supersede_left",
                "evidence": {
                    "predicate": predicate,
                    "values": list(objects[:5]),
                },
            })
    return candidates


async def detect_memory_conflicts(
    pool: Any,
    scope: str,
    *,
    llm_client: Any = None,
    llm_model: str = "",
    candidate_limit: int = 200,
    similarity_floor: float = 0.80,
    similarity_ceiling: float = 0.97,
    max_pairs: int = 30,
    batch_size: int = 8,
) -> tuple[list[dict], int, bool]:
    """Return (candidates, pair_count, skipped_no_llm)."""
    if llm_client is None:
        return [], 0, True

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "WITH recent AS ("
                "  SELECT id, statement, embedding FROM memories "
                "  WHERE scope = %s AND valid_to IS NULL AND embedding IS NOT NULL "
                "  AND epistemic_status IN ('canonical', 'belief') "
                "  ORDER BY created_at DESC LIMIT %s"
                ") "
                "SELECT m1.id, m1.statement, m2.id, m2.statement, "
                "       1 - (m1.embedding <=> m2.embedding) AS sim "
                "FROM recent m1 JOIN recent m2 ON m1.id < m2.id "
                "WHERE 1 - (m1.embedding <=> m2.embedding) BETWEEN %s AND %s "
                "ORDER BY sim DESC LIMIT %s",
                (scope, candidate_limit, similarity_floor, similarity_ceiling, max_pairs),
            )
            pairs = await cur.fetchall()

    if not pairs:
        return [], 0, False

    candidates: list[dict] = []
    for start in range(0, len(pairs), batch_size):
        batch = pairs[start:start + batch_size]
        prompts = [(row[1], row[3]) for row in batch]
        try:
            resp = await llm_client.chat.completions.create(
                model=llm_model,
                messages=[
                    {"role": "system", "content": _CLASSIFY_SYSTEM},
                    {"role": "user", "content": _classify_prompt(prompts)},
                ],
                temperature=0.0,
                max_tokens=256 + 128 * len(batch),
            )
            records = json_records(resp.choices[0].message.content)
        except Exception:
            log.warning("contradiction classifier failed for a batch", exc_info=True)
            continue

        by_index = {}
        for record in records:
            try:
                by_index[int(record.get("index"))] = record
            except (TypeError, ValueError):
                continue

        for offset, (left_id, left_stmt, right_id, right_stmt, sim) in enumerate(batch, 1):
            record = by_index.get(offset)
            if not record or record.get("relation") != "contradiction":
                continue
            candidates.append({
                "scope": scope,
                "kind": "memory_memory",
                "left_id": left_id,
                "right_id": right_id,
                "similarity": float(sim),
                "reason": str(record.get("reason") or "classifier marked these as contradictory")[:500],
                "proposed_action": "supersede_left",
                "evidence": {
                    "left": left_stmt[:500],
                    "right": right_stmt[:500],
                    "relation": "contradiction",
                },
            })
    return candidates, len(pairs), False


async def scan(
    pool: Any,
    publisher: Any,
    scope: str,
    *,
    llm_client: Any = None,
    llm_model: str = "",
    candidate_limit: int = 200,
    similarity_floor: float = 0.80,
    similarity_ceiling: float = 0.97,
    max_pairs: int = 30,
    batch_size: int = 8,
) -> dict:
    identity = await detect_identity_conflicts(pool, scope)
    memory, pair_count, skipped_no_llm = await detect_memory_conflicts(
        pool, scope,
        llm_client=llm_client, llm_model=llm_model,
        candidate_limit=candidate_limit,
        similarity_floor=similarity_floor,
        similarity_ceiling=similarity_ceiling,
        max_pairs=max_pairs, batch_size=batch_size,
    )
    candidates = identity + memory

    inserted = 0
    subject = stamped = None
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                for candidate in candidates:
                    await cur.execute(
                        "INSERT INTO contradiction_candidates "
                        "(id, scope, kind, left_id, right_id, similarity, reason, "
                        " proposed_action, evidence) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb) "
                        "ON CONFLICT (scope, kind, left_id, right_id) "
                        "WHERE status = 'open' DO NOTHING "
                        "RETURNING id",
                        (
                            mint("ctr"), candidate["scope"], candidate["kind"],
                            candidate["left_id"], candidate["right_id"],
                            candidate["similarity"], candidate["reason"],
                            candidate["proposed_action"],
                            json.dumps(candidate["evidence"]),
                        ),
                    )
                    if await cur.fetchone():
                        inserted += 1
                if inserted:
                    ev = build_event(
                        short_type="cognition.contradiction.detected",
                        subject=scope,
                        scope=scope_parts(scope),
                        data={"inserted": inserted, "identity": len(identity),
                              "memory": len(memory), "pairs": pair_count},
                        actor="agent:contradiction",
                        source="curlyos-core/cognition",
                    )
                    _stored, subject, stamped = await publisher.stage(ev, conn)

    if subject is not None:
        try:
            await publisher.emit(subject, stamped)
        except Exception:
            log.warning("contradiction detected event emit failed", exc_info=True)

    return {
        "scope": scope,
        "identity_conflicts": len(identity),
        "memory_conflicts": len(memory),
        "pairs_examined": pair_count,
        "inserted": inserted,
        "skipped_no_llm": skipped_no_llm,
    }


async def list_candidates(
    pool: Any, scope: str, status: str = "open", limit: int = 50,
) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT c.id, c.kind, c.left_id, c.right_id, c.similarity, c.reason, "
                "       c.proposed_action, c.status, c.evidence, c.created_at, "
                "       COALESCE(ml.statement, il.object) AS left_text, "
                "       COALESCE(mr.statement, ir.object) AS right_text, "
                "       il.predicate "
                "FROM contradiction_candidates c "
                "LEFT JOIN memories ml ON ml.id = c.left_id AND c.kind = 'memory_memory' "
                "LEFT JOIN memories mr ON mr.id = c.right_id AND c.kind = 'memory_memory' "
                "LEFT JOIN identity_facts il ON il.id = c.left_id AND c.kind = 'identity_identity' "
                "LEFT JOIN identity_facts ir ON ir.id = c.right_id AND c.kind = 'identity_identity' "
                "WHERE c.scope = %s AND c.status = %s "
                "ORDER BY c.created_at DESC LIMIT %s",
                (scope, status, limit),
            )
            return [
                {
                    "id": r[0], "kind": r[1], "left_id": r[2], "right_id": r[3],
                    "similarity": r[4], "reason": r[5], "proposed_action": r[6],
                    "status": r[7], "evidence": r[8], "created_at": r[9],
                    "left_text": r[10], "right_text": r[11], "predicate": r[12],
                }
                for r in await cur.fetchall()
            ]


async def resolve_candidate(
    pool: Any,
    publisher: Any,
    scope: str,
    candidate_id: str,
    action: str,
    resolved_by: str = "user",
) -> dict:
    if action not in RESOLVE_ACTIONS:
        raise ContradictionError(f"action must be one of {RESOLVE_ACTIONS}")

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT kind, left_id, right_id, status FROM contradiction_candidates "
                "WHERE id = %s AND scope = %s",
                (candidate_id, scope),
            )
            row = await cur.fetchone()
    if row is None:
        raise ContradictionError(f"candidate not found: {candidate_id}")
    kind, left_id, right_id, status = row
    if status != "open":
        raise ContradictionError(f"candidate is already {status}")

    victim = keeper = None
    if action in ("supersede_left", "supersede_right"):
        victim, keeper = (
            (left_id, right_id) if action == "supersede_left" else (right_id, left_id)
        )
        if kind == "memory_memory":
            from memory.governance import invalidate

            await invalidate(
                pool, publisher, scope, victim,
                superseded_by=keeper, reason="contradiction_resolution",
            )
        else:
            from identity import invalidate_identity_fact

            await invalidate_identity_fact(
                pool, publisher, scope, victim, superseded_by=keeper,
            )

    new_status = "dismissed" if action == "dismiss" else "resolved"
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE contradiction_candidates "
                    "SET status = %s, resolved_at = now(), resolved_by = %s "
                    "WHERE id = %s AND scope = %s AND status = 'open'",
                    (new_status, resolved_by, candidate_id, scope),
                )
                ev = build_event(
                    short_type="cognition.contradiction.resolved",
                    subject=candidate_id,
                    scope=scope_parts(scope),
                    data={"candidate_id": candidate_id, "action": action,
                          "victim": victim, "keeper": keeper, "status": new_status},
                    actor=f"user:{resolved_by}",
                    source="curlyos-core/cognition",
                )
                await publisher.stage(ev, conn)

    return {
        "id": candidate_id, "status": new_status, "action": action,
        "victim": victim, "keeper": keeper,
    }
