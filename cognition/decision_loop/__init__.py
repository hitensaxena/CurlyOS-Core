"""Decision → Outcome → Lesson loop — the cognition layer that lets CurlyOS
*learn from its own decisions* rather than merely record them.

The four moves (see migrations/0007_decision_loop.sql for the schema):

    1. DECIDE   — a decision is written with a falsifiable prediction
                  (decisions.predicted_outcome + prediction_confidence).
    2. OUTCOME  — record_outcome_async(): what actually happened, scored against
                  the prediction via a Brier surprise term.
    3. LESSON   — distill_or_reinforce_lesson_async(): generalise a reusable
                  lesson; if a near-duplicate already exists, reinforce it
                  instead of spawning a clone (so confidence accrues with
                  evidence).
    4. FEEDBACK — retrieve_lessons_async(): pull relevant lessons (embedding
                  similarity + optional condition gate) into a future decision.

    KG-mirror   — mirror_lesson_to_kg_async(): promote a lesson to a first-class
                  `Lesson` knowledge_entity and wire `applies_to` edges into the
                  graph, so lessons are traversable alongside everything else.

These own only the DB mechanics + scoring; they take a psycopg3 async
connection. Embeddings are passed in as 1024-dim lists — callers choose the
embedder; the distillation *text* (the lesson statement) is produced upstream by
the reflection/meta engines (LLM), not here.
"""

from __future__ import annotations

import json
from typing import Any, Optional, Sequence

from shared.types.ulid import mint

EMBED_DIM = 1024


# --- helpers ----------------------------------------------------------------

def _vec(embedding: Sequence[float]) -> str:
    """Format a float vector as a pgvector literal (paired with `%s::vector`)."""
    if embedding is None:
        raise ValueError("embedding is required")
    if len(embedding) != EMBED_DIM:
        raise ValueError(f"embedding must be {EMBED_DIM}-dim, got {len(embedding)}")
    return "[" + ",".join(f"{float(x):.6f}" for x in embedding) + "]"


def _brier_surprise(confidence: Optional[float], matched: Optional[bool]) -> Optional[float]:
    """Brier score for a single binary prediction: (confidence - hit)^2.

    High surprise = the system was confident and wrong (or unconfident and
    right) — exactly the signal worth distilling a lesson from.
    """
    if confidence is None or matched is None:
        return None
    hit = 1.0 if matched else 0.0
    return (float(confidence) - hit) ** 2


# ===========================================================================
# The public API. Every caller — the REST routes, goals.review_decision and
# distillation — runs on the psycopg3 async pool, so there is one set of
# helpers here, not two. The pure `_vec` / `_brier_surprise` above stay
# dependency-free so the scoring can be unit-tested without a database.
# ===========================================================================

async def record_outcome_async(
    conn,
    *,
    scope: str,
    decision_id: str,
    summary: str,
    valence: str,
    embedding: Optional[Sequence[float]] = None,
    goal_id: Optional[str] = None,
    matched_prediction: Optional[bool] = None,
    metrics: Optional[dict[str, Any]] = None,
    evidence_refs: Optional[list[str]] = None,
    source_episode_id: Optional[str] = None,
) -> str:
    """Async record_outcome. `embedding` may be None (outcome still recorded
    and scored; it just won't be semantically retrievable)."""
    async with conn.cursor() as cur:
        await cur.execute("SELECT prediction_confidence FROM decisions WHERE id = %s",
                          (decision_id,))
        row = await cur.fetchone()
        if row is None:
            raise ValueError(f"decision {decision_id} not found")
        surprise = _brier_surprise(row[0], matched_prediction)

        out_id = mint("out")
        vec = _vec(embedding) if embedding is not None else None
        await cur.execute(
            "INSERT INTO outcomes "
            "(id, scope, decision_id, goal_id, summary, valence, matched_prediction, "
            " surprise, metrics, evidence_refs, embedding, source_episode_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s)",
            (out_id, scope, decision_id, goal_id, summary, valence, matched_prediction,
             surprise, json.dumps(metrics or {}), evidence_refs or [], vec, source_episode_id),
        )
        await cur.execute(
            "UPDATE decisions SET outcome = %s, outcome_id = %s, reviewed_at = now() WHERE id = %s",
            (summary, out_id, decision_id),
        )
    return out_id


async def distill_or_reinforce_lesson_async(
    conn,
    *,
    scope: str,
    statement: str,
    embedding: Sequence[float],
    derived_from_outcomes: list[str],
    applies_when: Optional[str] = None,
    conditions: Optional[dict[str, Any]] = None,
    applies_to_entities: Optional[list[str]] = None,
    updates_model: Optional[str] = None,
    source_episode_id: Optional[str] = None,
    sim_threshold: float = 0.85,
    confidence_step: float = 0.1,
) -> tuple[str, str]:
    """Async distill_or_reinforce_lesson. Returns (lesson_id, "created"|"reinforced")."""
    vec = _vec(embedding)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, 1 - (embedding <=> %s::vector) AS sim FROM lessons "
            "WHERE scope = %s AND valid_to IS NULL AND embedding IS NOT NULL "
            "ORDER BY embedding <=> %s::vector LIMIT 1",
            (vec, scope, vec),
        )
        nearest = await cur.fetchone()
        if nearest is not None and nearest[1] is not None and nearest[1] >= sim_threshold:
            lesson_id = nearest[0]
            await cur.execute(
                "UPDATE lessons SET "
                "  support_count = support_count + 1, "
                "  confidence = LEAST(1.0, confidence + %s), "
                "  derived_from_outcomes = ("
                "    SELECT ARRAY(SELECT DISTINCT unnest(derived_from_outcomes || %s::text[]))), "
                "  status = CASE WHEN status = 'provisional' AND support_count + 1 >= 3 "
                "                THEN 'validated' ELSE status END, "
                "  properties = properties || jsonb_build_object('last_reinforced_at', now()::text) "
                "WHERE id = %s",
                (confidence_step, derived_from_outcomes, lesson_id),
            )
            return lesson_id, "reinforced"

        lesson_id = mint("les")
        await cur.execute(
            "INSERT INTO lessons "
            "(id, scope, statement, applies_when, conditions, derived_from_outcomes, "
            " applies_to_entities, updates_model, embedding, source_episode_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s)",
            (lesson_id, scope, statement, applies_when, json.dumps(conditions or {}),
             derived_from_outcomes, applies_to_entities or [], updates_model, vec,
             source_episode_id),
        )
    return lesson_id, "created"


async def retrieve_lessons_async(
    conn,
    *,
    scope: str,
    query_embedding: Sequence[float],
    domain: Optional[str] = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Async retrieve_lessons — the feedback read used by Executive hydration."""
    vec = _vec(query_embedding)
    params: list[Any] = [vec, scope]
    domain_clause = ""
    if domain is not None:
        domain_clause = "AND (conditions->>'domain' = %s OR conditions = '{}'::jsonb) "
        params.append(domain)
    params.append(vec)
    params.append(limit)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, statement, confidence, support_count, status, "
            "       1 - (embedding <=> %s::vector) AS similarity "
            "FROM lessons WHERE scope = %s AND valid_to IS NULL AND embedding IS NOT NULL "
            "  AND status IN ('provisional','validated') "
            + domain_clause +
            "ORDER BY embedding <=> %s::vector LIMIT %s",
            tuple(params),
        )
        rows = await cur.fetchall()
    return [
        {"id": r[0], "statement": r[1], "confidence": r[2],
         "support_count": r[3], "status": r[4], "similarity": r[5]}
        for r in rows
    ]


async def mirror_lesson_to_kg_async(
    conn,
    *,
    scope: str,
    lesson_id: str,
    rel_type: str = "applies_to",
    source_episode_id: Optional[str] = None,
) -> str:
    """Async mirror_lesson_to_kg — idempotent (cached on lessons.properties.kg_entity_id)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT statement, embedding, applies_to_entities, properties->>'kg_entity_id' "
            "FROM lessons WHERE id = %s AND valid_to IS NULL",
            (lesson_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise ValueError(f"lesson {lesson_id} not found or invalidated")
        statement, embedding, applies_to_entities, existing = row[0], row[1], row[2], row[3]
        if existing:
            return existing

        entity_id = mint("ent")
        name = statement if len(statement) <= 120 else statement[:117] + "..."
        await cur.execute(
            "INSERT INTO knowledge_entities "
            "(id, scope, name, label, properties, embedding, epistemic_status, source_episode_id) "
            "VALUES (%s, %s, %s, 'Lesson', %s, %s::vector, 'provisional', %s)",
            (entity_id, scope, name, json.dumps({"lesson_id": lesson_id, "statement": statement}),
             str(embedding) if embedding is not None else None, source_episode_id),
        )
        for dst in (applies_to_entities or []):
            await cur.execute(
                "SELECT 1 FROM knowledge_entities WHERE id = %s AND valid_to IS NULL", (dst,))
            if await cur.fetchone() is None:
                continue
            await cur.execute(
                "INSERT INTO knowledge_edges "
                "(id, src_entity_id, dst_entity_id, rel_type, properties, source_episode_id) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (mint("cor"), entity_id, dst, rel_type, json.dumps({}), source_episode_id),
            )
        await cur.execute(
            "UPDATE lessons SET properties = properties || jsonb_build_object('kg_entity_id', %s::text) "
            "WHERE id = %s",
            (entity_id, lesson_id),
        )
    return entity_id


# ===========================================================================
# Automatic distillation — the reflection step. Turns high-surprise outcomes
# (the system was confidently wrong) into lessons without waiting for a human
# or agent to hand-write one. The LLM is an optional seam; a heuristic
# statement is always available so the loop runs even with no model.
# ===========================================================================

# Brier >= 0.25 means a >=0.5-confidence prediction missed (or a very confident
# one was somewhat off) — the threshold where surprise is worth a lesson.
DEFAULT_SURPRISE_THRESHOLD = 0.25


async def _distill_statement(
    llm_client, model, *, title, context, chosen, rationale, predicted, valence, summary,
) -> str:
    """Produce a one-sentence, generalizable lesson. LLM if available; otherwise
    a heuristic seed that still captures decision/prediction/actual."""
    heuristic = (
        f"On '{title}', the bet was '{predicted}' but it turned out {valence}: "
        f"{summary[:180]}. Revisit the assumption behind '{chosen}'."
    )
    if llm_client is None:
        return heuristic
    try:
        prompt = (
            "Distil ONE reusable lesson (a single sentence, generalizable and "
            "actionable) from a decision whose outcome surprised us. Capture the "
            "transferable principle, not the specifics.\n\n"
            f"Decision: {title}\nContext: {context or '-'}\nChose: {chosen}\n"
            f"Rationale: {rationale}\nPredicted: {predicted}\n"
            f"Actual ({valence}): {summary}\n\nLesson:"
        )
        resp = await llm_client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}],
            temperature=0.3, max_tokens=120,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text or heuristic
    except Exception:  # noqa: BLE001 — never let a model failure break reflection
        return heuristic


async def distill_lessons_from_outcomes(
    pool,
    *,
    scope: str,
    embedder: Any = None,
    llm_client: Any = None,
    llm_model: str = "gpt-4o-mini",
    surprise_threshold: float = DEFAULT_SURPRISE_THRESHOLD,
    limit: int = 20,
) -> dict[str, int]:
    """Reflection step: distil lessons from high-surprise, not-yet-distilled
    outcomes (reinforce-or-create), then mirror each into the knowledge graph.

    Idempotent: an outcome already referenced by some lesson's
    derived_from_outcomes is skipped, so repeated reflection passes converge.
    Requires an embedder (lessons must be embedded to dedup + retrieve); with
    none, it is a no-op.
    """
    result = {"outcomes_considered": 0, "lessons_created": 0, "lessons_reinforced": 0}
    if embedder is None:
        return result

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT o.id, o.summary, o.valence, "
                "       d.title, d.context, d.chosen, d.rationale, d.predicted_outcome "
                "FROM outcomes o JOIN decisions d ON d.id = o.decision_id "
                "WHERE o.scope = %s AND o.valid_to IS NULL AND o.surprise >= %s "
                "  AND NOT EXISTS (SELECT 1 FROM lessons l "
                "     WHERE l.scope = o.scope AND l.valid_to IS NULL "
                "       AND o.id = ANY(l.derived_from_outcomes)) "
                "ORDER BY o.surprise DESC LIMIT %s",
                (scope, surprise_threshold, limit),
            )
            candidates = await cur.fetchall()

    result["outcomes_considered"] = len(candidates)
    for (out_id, summary, valence, title, context, chosen, rationale, predicted) in candidates:
        statement = await _distill_statement(
            llm_client, llm_model, title=title, context=context, chosen=chosen,
            rationale=rationale, predicted=predicted or "(none recorded)",
            valence=valence, summary=summary,
        )
        embedding = (await embedder.embed([statement]))[0]
        async with pool.connection() as conn:
            _les_id, action = await distill_or_reinforce_lesson_async(
                conn, scope=scope, statement=statement, embedding=embedding,
                derived_from_outcomes=[out_id],
            )
            await mirror_lesson_to_kg_async(conn, scope=scope, lesson_id=_les_id)
        if action == "created":
            result["lessons_created"] += 1
        else:
            result["lessons_reinforced"] += 1
    return result


# ===========================================================================
# Confidence decay — so a lesson that stops being reinforced fades and
# eventually retires, instead of dominating retrieval forever. Reinforcement
# (distill_or_reinforce_lesson) stamps last_reinforced_at and bumps confidence,
# so an actively-confirmed lesson never decays; a neglected one does.
# ===========================================================================

DEFAULT_HALF_LIFE_DAYS = 30.0   # confidence halves per 30 idle days
DEFAULT_CONFIDENCE_FLOOR = 0.1  # decayed (not retired) lessons never drop below this
DEFAULT_RETIRE_BELOW = 0.05     # a single decay landing under this retires the lesson


async def decay_lesson_confidence(
    pool,
    *,
    scope: str,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    floor: float = DEFAULT_CONFIDENCE_FLOOR,
    retire_below: float = DEFAULT_RETIRE_BELOW,
) -> dict[str, int]:
    """Exponentially decay active lessons by idle time, anchored on the latest
    of {created_at, last_reinforced_at, last_decayed_at}. A lesson whose decayed
    confidence falls under `retire_below` is retired (excluded from retrieval);
    otherwise it floors at `floor`. Stamping last_decayed_at each run keeps this
    idempotent — running twice in a row barely moves anything.

    Returns {"decayed": n, "retired": m}.
    """
    params = {"scope": scope, "hl": half_life_days, "floor": floor, "retire": retire_below}
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "WITH anchored AS ("
                "  SELECT id, "
                "    GREATEST(created_at, "
                "      COALESCE((properties->>'last_reinforced_at')::timestamptz, created_at), "
                "      COALESCE((properties->>'last_decayed_at')::timestamptz, created_at)"
                "    ) AS anchor, "
                "    confidence * power(0.5, EXTRACT(EPOCH FROM (now() - GREATEST(created_at, "
                "      COALESCE((properties->>'last_reinforced_at')::timestamptz, created_at), "
                "      COALESCE((properties->>'last_decayed_at')::timestamptz, created_at)"
                "    ))) / 86400.0 / %(hl)s) AS raw_conf "
                "  FROM lessons "
                "  WHERE scope = %(scope)s AND valid_to IS NULL "
                "    AND status IN ('provisional','validated')"
                ") "
                "UPDATE lessons l SET "
                "  confidence = CASE WHEN a.raw_conf < %(retire)s THEN l.confidence "
                "                    ELSE GREATEST(%(floor)s, a.raw_conf) END, "
                "  status = CASE WHEN a.raw_conf < %(retire)s THEN 'retired' ELSE l.status END, "
                "  properties = l.properties || jsonb_build_object('last_decayed_at', now()::text) "
                "FROM anchored a "
                "WHERE l.id = a.id AND now() > a.anchor "
                "RETURNING l.status",
                params,
            )
            rows = await cur.fetchall()
    retired = sum(1 for r in rows if r[0] == "retired")
    return {"decayed": len(rows), "retired": retired}
