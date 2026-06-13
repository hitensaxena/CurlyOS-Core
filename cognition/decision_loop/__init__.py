"""Decision → Outcome → Lesson loop — the cognition layer that lets CurlyOS
*learn from its own decisions* rather than merely record them.

The four moves (see migrations/0007_decision_loop.sql for the schema):

    1. DECIDE   — a decision is written with a falsifiable prediction
                  (decisions.predicted_outcome + prediction_confidence).
    2. OUTCOME  — record_outcome(): what actually happened, scored against the
                  prediction via a Brier surprise term.
    3. LESSON   — distill_or_reinforce_lesson(): generalise a reusable lesson;
                  if a near-duplicate already exists, reinforce it instead of
                  spawning a clone (so confidence accrues with evidence).
    4. FEEDBACK — retrieve_lessons(): pull relevant lessons (embedding
                  similarity + optional condition gate) into a future decision.

    KG-mirror   — mirror_lesson_to_kg(): promote a lesson to a first-class
                  `Lesson` knowledge_entity and wire `applies_to` edges into the
                  graph, so lessons are traversable alongside everything else.

These are deliberately thin, synchronous psycopg helpers (matching
memory/consolidation/scheduler.py) that own only the DB mechanics + scoring.
Embeddings are passed in as 1024-dim lists — callers choose the embedder; the
distillation *text* (the lesson statement) is produced upstream by the
reflection/meta engines (LLM), not here.
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


# --- 2) OUTCOME -------------------------------------------------------------

def record_outcome(
    conn,
    *,
    scope: str,
    decision_id: str,
    summary: str,
    valence: str,
    embedding: Sequence[float],
    goal_id: Optional[str] = None,
    matched_prediction: Optional[bool] = None,
    metrics: Optional[dict[str, Any]] = None,
    evidence_refs: Optional[list[str]] = None,
    source_episode_id: Optional[str] = None,
) -> str:
    """Record what actually happened for a decision and score it against the
    prediction captured at decision time. Returns the new outcome id.

    Also backfills the decision's human-readable cache (decisions.outcome),
    links decisions.outcome_id, and stamps reviewed_at — so the existing
    scheduler review index (idx_decisions_review) stops surfacing it.
    """
    row = conn.execute(
        "SELECT prediction_confidence FROM decisions WHERE id = %s",
        (decision_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"decision {decision_id} not found")
    prediction_confidence = row[0]
    surprise = _brier_surprise(prediction_confidence, matched_prediction)

    out_id = mint("out")
    conn.execute(
        "INSERT INTO outcomes "
        "(id, scope, decision_id, goal_id, summary, valence, matched_prediction, "
        " surprise, metrics, evidence_refs, embedding, source_episode_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s)",
        (
            out_id, scope, decision_id, goal_id, summary, valence,
            matched_prediction, surprise, json.dumps(metrics or {}),
            evidence_refs or [], _vec(embedding), source_episode_id,
        ),
    )
    conn.execute(
        "UPDATE decisions SET outcome = %s, outcome_id = %s, reviewed_at = now() "
        "WHERE id = %s",
        (summary, out_id, decision_id),
    )
    return out_id


# --- 3) LESSON --------------------------------------------------------------

def distill_or_reinforce_lesson(
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
    """Create a new lesson, or reinforce the nearest existing one if it is
    semantically close enough (cosine >= sim_threshold).

    Returns (lesson_id, action) where action is "created" or "reinforced".
    Reinforcing bumps support_count + confidence and appends provenance, so a
    repeatedly-confirmed lesson grows trustworthy instead of fragmenting into
    near-duplicate rows.
    """
    vec = _vec(embedding)
    nearest = conn.execute(
        "SELECT id, 1 - (embedding <=> %s::vector) AS sim FROM lessons "
        "WHERE scope = %s AND valid_to IS NULL AND embedding IS NOT NULL "
        "ORDER BY embedding <=> %s::vector LIMIT 1",
        (vec, scope, vec),
    ).fetchone()

    if nearest is not None and nearest[1] is not None and nearest[1] >= sim_threshold:
        lesson_id = nearest[0]
        conn.execute(
            "UPDATE lessons SET "
            "  support_count = support_count + 1, "
            "  confidence = LEAST(1.0, confidence + %s), "
            "  derived_from_outcomes = ("
            "    SELECT ARRAY(SELECT DISTINCT unnest(derived_from_outcomes || %s::text[]))"
            "  ), "
            "  status = CASE WHEN status = 'provisional' AND support_count + 1 >= 3 "
            "                THEN 'validated' ELSE status END, "
            "  properties = properties || jsonb_build_object('last_reinforced_at', now()::text) "
            "WHERE id = %s",
            (confidence_step, derived_from_outcomes, lesson_id),
        )
        return lesson_id, "reinforced"

    lesson_id = mint("les")
    conn.execute(
        "INSERT INTO lessons "
        "(id, scope, statement, applies_when, conditions, derived_from_outcomes, "
        " applies_to_entities, updates_model, embedding, source_episode_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector, %s)",
        (
            lesson_id, scope, statement, applies_when,
            json.dumps(conditions or {}), derived_from_outcomes,
            applies_to_entities or [], updates_model, vec, source_episode_id,
        ),
    )
    return lesson_id, "created"


# --- 4) FEEDBACK ------------------------------------------------------------

def retrieve_lessons(
    conn,
    *,
    scope: str,
    query_embedding: Sequence[float],
    domain: Optional[str] = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Pull the lessons most relevant to a pending decision — the move that
    actually closes the loop. Ranks by embedding similarity; if `domain` is
    given, gates to lessons whose conditions match that domain (or are
    unconditioned). Retired/contradicted lessons are excluded.
    """
    vec = _vec(query_embedding)
    params: list[Any] = [vec, scope]
    domain_clause = ""
    if domain is not None:
        domain_clause = "AND (conditions->>'domain' = %s OR conditions = '{}'::jsonb) "
        params.append(domain)
    params.append(vec)
    params.append(limit)

    rows = conn.execute(
        "SELECT id, statement, confidence, support_count, status, "
        "       1 - (embedding <=> %s::vector) AS similarity "
        "FROM lessons "
        "WHERE scope = %s AND valid_to IS NULL AND embedding IS NOT NULL "
        "  AND status IN ('provisional','validated') "
        + domain_clause +
        "ORDER BY embedding <=> %s::vector LIMIT %s",
        tuple(params),
    ).fetchall()
    return [
        {
            "id": r[0], "statement": r[1], "confidence": r[2],
            "support_count": r[3], "status": r[4], "similarity": r[5],
        }
        for r in rows
    ]


# --- KG-mirror variant ------------------------------------------------------

def mirror_lesson_to_kg(
    conn,
    *,
    scope: str,
    lesson_id: str,
    rel_type: str = "applies_to",
    source_episode_id: Optional[str] = None,
) -> str:
    """Promote a lesson into the knowledge graph as a first-class `Lesson`
    entity, with `applies_to` edges to every entity the lesson is about — so
    lessons are reachable by ordinary graph traversal (k-hop, densify, bridge)
    alongside the rest of the KG.

    Idempotent: the minted entity id is cached on lessons.properties.kg_entity_id;
    a second call returns the same id without creating duplicates.
    """
    row = conn.execute(
        "SELECT statement, embedding, applies_to_entities, "
        "       properties->>'kg_entity_id' AS kg_entity_id "
        "FROM lessons WHERE id = %s AND valid_to IS NULL",
        (lesson_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"lesson {lesson_id} not found or invalidated")
    statement, embedding, applies_to_entities, existing = row[0], row[1], row[2], row[3]
    if existing:
        return existing  # already mirrored

    entity_id = mint("ent")
    name = statement if len(statement) <= 120 else statement[:117] + "..."
    props = {"lesson_id": lesson_id, "statement": statement}

    # `embedding` comes back as the pgvector text form ("[...]"); re-cast on write.
    conn.execute(
        "INSERT INTO knowledge_entities "
        "(id, scope, name, label, properties, embedding, epistemic_status, source_episode_id) "
        "VALUES (%s, %s, %s, 'Lesson', %s, %s::vector, 'provisional', %s)",
        (entity_id, scope, name, json.dumps(props),
         str(embedding) if embedding is not None else None, source_episode_id),
    )

    # Wire the lesson to the entities it applies to (both are knowledge_entities,
    # so they live in the same edge table). Outcome provenance stays on the
    # lesson row (out_ ids are not graph entities).
    for dst in (applies_to_entities or []):
        exists = conn.execute(
            "SELECT 1 FROM knowledge_entities WHERE id = %s AND valid_to IS NULL",
            (dst,),
        ).fetchone()
        if exists is None:
            continue  # skip dangling refs rather than fail the whole mirror
        conn.execute(
            "INSERT INTO knowledge_edges "
            "(id, src_entity_id, dst_entity_id, rel_type, properties, source_episode_id) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (mint("cor"), entity_id, dst, rel_type, json.dumps({}), source_episode_id),
        )

    conn.execute(
        "UPDATE lessons SET properties = properties || jsonb_build_object('kg_entity_id', %s::text) "
        "WHERE id = %s",
        (entity_id, lesson_id),
    )
    return entity_id


# ===========================================================================
# Async variants — for the live app path (orchestration/goals use a psycopg3
# async pool). Same SQL + scoring as the sync helpers above (which serve the
# sync consolidation/reflection world and the smoketests); they reuse the pure
# _vec / _brier_surprise so the logic stays single-sourced.
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
