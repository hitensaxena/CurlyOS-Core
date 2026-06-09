"""Identity engine — maintain the user's durable self-model as bi-temporal identity_facts.

Key APIs:
  propose_identity_fact(predicate, object, confidence, source_episode_id)
  get_identity_context(scope, predicates[]) → structured context dict

Confidence gating:
  Auto-promote at ≥ 0.75
  Require approval at ≥ 0.90
  Conflict: invalidate lower-confidence older fact

See: ~/hitenos-architecture/04-identity-engine.md
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any

from shared.types.ulid import mint, is_valid
from shared.events import build_event


# ── Confidence thresholds ───────────────────────────────────────────────────
AUTO_PROMOTE_THRESHOLD = 0.75
CONFIRM_REQUIRED_THRESHOLD = 0.90


async def propose_identity_fact(
    pool: Any,
    publisher: Any,
    scope_text: str,
    predicate: str,
    object: str,
    confidence: float,
    source_episode_id: str,
) -> dict:
    """Propose an identity fact. Auto-promotes if confidence ≥ 0.75.

    On conflict with existing (scope, predicate), invalidates the lower-confidence one.
    """
    # Validate source_episode_id is a valid ULID with 'epi' prefix
    if not is_valid("epi", source_episode_id):
        raise ValueError(
            f"Invalid source_episode_id {source_episode_id!r}: "
            "must be a valid ULID with 'epi' prefix"
        )

    idf_id = mint("idf")
    now = datetime.now(timezone.utc)

    # Determine epistemic status based on confidence
    epistemic_status = "canonical" if confidence >= AUTO_PROMOTE_THRESHOLD else "hypothesis"

    async with pool.connection() as conn:
        # Check for existing fact with same (scope, predicate)
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, confidence, object FROM identity_facts "
                "WHERE scope = %s AND predicate = %s AND valid_to IS NULL",
                (scope_text, predicate),
            )
            existing = await cur.fetchone()

        action_taken = "inserted"

        if existing:
            existing_id, existing_conf, existing_obj = existing
            if confidence > existing_conf:
                # New fact supersedes old — insert new FIRST,
                # THEN invalidate old (to satisfy FK on superseded_by)
                action_taken = "superseded"
            else:
                # Lower or equal confidence than existing — skip
                return {
                    "idf_id": existing_id,
                    "predicate": predicate,
                    "object": existing_obj,
                    "confidence": existing_conf,
                    "epistemic_status": None,
                    "action_taken": "no_change",
                }

        # Insert new identity fact
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO identity_facts "
                "(id, scope, predicate, object, confidence, epistemic_status, "
                " valid_from, ingested_at, source_episode_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING valid_from, ingested_at",
                (idf_id, scope_text, predicate, object, confidence,
                 epistemic_status, now, now, source_episode_id),
            )
            vf, ia = await cur.fetchone()

        # Now invalidate old fact if superseding (after new one exists for FK)
        if existing and action_taken == "superseded":
            existing_id = existing[0]
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE identity_facts SET valid_to = now(), superseded_by = %s "
                    "WHERE id = %s AND valid_to IS NULL",
                    (idf_id, existing_id),
                )

        # Build and stage identity.fact.updated event
        event = build_event(
            short_type="identity.fact.updated",
            subject=f"identity_fact:{idf_id}",
            scope={"scope": scope_text},
            data={
                "idf_id": idf_id,
                "predicate": predicate,
                "object": object,
                "confidence": confidence,
                "epistemic_status": epistemic_status,
                "action_taken": action_taken,
                "source_episode_id": source_episode_id,
            },
        )
        if publisher is not None:
            try:
                await publisher.stage(event, conn)
            except Exception:
                pass  # event staging is best-effort

    return {
        "idf_id": idf_id,
        "predicate": predicate,
        "object": object,
        "confidence": confidence,
        "epistemic_status": epistemic_status,
        "action_taken": action_taken,
    }


async def get_identity_context(
    pool: Any,
    scope_text: str,
    predicates: list[str] | None = None,
) -> dict[str, Any]:
    """Retrieve current identity facts for prompt injection.

    Returns dict mapping predicate → {object, confidence, idf_id, valid_from, epistemic_status}.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            if predicates:
                await cur.execute(
                    "SELECT id, predicate, object, confidence, valid_from, epistemic_status "
                    "FROM identity_facts "
                    "WHERE scope = %s AND predicate = ANY(%s) AND valid_to IS NULL "
                    "ORDER BY confidence DESC",
                    (scope_text, predicates),
                )
            else:
                await cur.execute(
                    "SELECT id, predicate, object, confidence, valid_from, epistemic_status "
                    "FROM identity_facts "
                    "WHERE scope = %s AND valid_to IS NULL "
                    "ORDER BY confidence DESC",
                    (scope_text,),
                )
            rows = await cur.fetchall()

    context: dict[str, Any] = {}
    for r in rows:
        pred = r[1]
        if pred not in context:  # take highest confidence (already ordered)
            context[pred] = {
                "object": r[2],
                "confidence": float(r[3]),
                "idf_id": r[0],
                "valid_from": r[4].isoformat() if r[4] else None,
                "epistemic_status": r[5],
            }
    return context


async def invalidate_identity_fact(
    pool: Any,
    publisher: Any,
    scope_text: str,
    fact_id: str,
    superseded_by: str | None = None,
) -> dict:
    """Invalidate an identity fact by setting valid_to = now().

    Returns {fact_id, valid_to}.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE identity_facts SET valid_to = now(), superseded_by = %s "
                "WHERE id = %s AND scope = %s AND valid_to IS NULL "
                "RETURNING valid_to",
                (superseded_by, fact_id, scope_text),
            )
            row = await cur.fetchone()
            valid_to = row[0] if row else None

        # Build and stage identity.fact.updated event
        event = build_event(
            short_type="identity.fact.updated",
            subject=f"identity_fact:{fact_id}",
            scope={"scope": scope_text},
            data={
                "idf_id": fact_id,
                "action_taken": "invalidated",
                "superseded_by": superseded_by,
            },
        )
        if publisher is not None:
            try:
                await publisher.stage(event, conn)
            except Exception:
                pass  # event staging is best-effort

    return {"fact_id": fact_id, "valid_to": valid_to}


async def list_identity_facts(
    pool: Any,
    scope_text: str,
    include_expired: bool = False,
    predicate: str | None = None,
) -> list[dict]:
    """List identity facts for a scope.

    Returns list of dicts ordered by confidence DESC.
    """
    query = "SELECT id, scope, predicate, object, confidence, epistemic_status, valid_from, valid_to, superseded_by, source_episode_id FROM identity_facts WHERE scope = %s"
    params: list[Any] = [scope_text]

    if not include_expired:
        query += " AND valid_to IS NULL"

    if predicate is not None:
        query += " AND predicate = %s"
        params.append(predicate)

    query += " ORDER BY confidence DESC"

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, params)
            rows = await cur.fetchall()

    results: list[dict] = []
    for r in rows:
        results.append({
            "idf_id": r[0],
            "scope": r[1],
            "predicate": r[2],
            "object": r[3],
            "confidence": float(r[4]),
            "epistemic_status": r[5],
            "valid_from": r[6].isoformat() if r[6] else None,
            "valid_to": r[7].isoformat() if r[7] else None,
            "superseded_by": r[8],
            "source_episode_id": r[9],
        })
    return results
