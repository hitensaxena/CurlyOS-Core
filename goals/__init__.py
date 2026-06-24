"""Goal Operating System — goals, decisions, opportunities.

The frozen architecture's "What matters?" mind (curlyos-final/03 §3), realized
as three tables and this module — no new engine. Conventions match the rest of
curlyos-core: psycopg3 pool, ULID ids, events staged in the same transaction
as the write, invalidate-not-delete for goals.

Wiring (who reads/writes what):
  * goals      — created here (webapp/API/agent tool); progress updated by
                 reflection's goal-delta sync; read by Executive hydration.
  * decisions  — recorded at decision time with rationale + reversibility +
                 review_at; the scheduler nudges when review is due; outcome
                 filled at review; meta-cognition links its dau_ audit later.
  * opportunities — detected by the discovery scan (Phase X) or manually;
                 triaged accepted/rejected; accepting can mint a goal.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
import logging
from typing import Any

from agent.pdp_gate import scope_parts
from shared.events import build_event
from shared.types.ulid import mint

log = logging.getLogger("curlyos-core.goals")


async def _stage_and_emit(publisher: Any, conn: Any, short_type: str, subject: str,
                          scope_text: str, data: dict) -> None:
    ev = build_event(
        short_type=short_type, subject=subject, scope=scope_parts(scope_text),
        data=data, actor="system", source="curlyos-core/goals",
    )
    await publisher.stage(ev, conn)


# ── goals ─────────────────────────────────────────────────────────────────────

async def create_goal(
    pool: Any, publisher: Any, scope: str, *,
    title: str,
    description: str | None = None,
    horizon: str | None = None,
    parent_id: str | None = None,
    priority: int = 0,
    success_criteria: str | None = None,
    identity_refs: list[str] | None = None,
    project_refs: list[str] | None = None,
    source_episode_id: str | None = None,
) -> dict:
    goal_id = mint("goal")
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO goals (id, scope, parent_id, title, description, horizon, "
                "priority, success_criteria, identity_refs, project_refs, source_episode_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id, title, status, progress",
                (goal_id, scope, parent_id, title, description, horizon, priority,
                 success_criteria, identity_refs or [], project_refs or [], source_episode_id),
            )
            row = await cur.fetchone()
        await _stage_and_emit(publisher, conn, "goal.created", goal_id, scope,
                              {"goal_id": goal_id, "title": title, "horizon": horizon,
                               "parent_id": parent_id})
    return {"id": row[0], "title": row[1], "status": row[2], "progress": row[3]}


_GOAL_MUTABLE = {"title", "description", "horizon", "status", "priority",
                 "success_criteria", "progress", "parent_id"}


async def update_goal(pool: Any, publisher: Any, scope: str, goal_id: str,
                      changes: dict[str, Any]) -> dict:
    """Patch a current goal. Only whitelisted fields; status transitions are
    plain updates (achieved/abandoned keep the row current — history of edits
    is in the event log; invalidate() is for goals that were wrong, not done)."""
    fields = {k: v for k, v in changes.items() if k in _GOAL_MUTABLE and v is not None}
    if not fields:
        raise ValueError("no updatable fields in changes")
    sets = ", ".join(f"{k} = %s" for k in fields)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE goals SET {sets} WHERE id = %s AND scope = %s AND valid_to IS NULL "
                "RETURNING id, title, status, progress",
                (*fields.values(), goal_id, scope),
            )
            row = await cur.fetchone()
            if row is None:
                raise ValueError(f"goal {goal_id!r} not found (or invalidated)")
        await _stage_and_emit(publisher, conn, "goal.updated", goal_id, scope,
                              {"goal_id": goal_id, "changes": {k: str(v)[:200] for k, v in fields.items()}})
    return {"id": row[0], "title": row[1], "status": row[2], "progress": row[3]}


async def set_goal_reflection(pool: Any, goal_id: str, scope: str, delta: dict) -> bool:
    """Record the latest reflection delta on the goal (properties.last_reflection);
    a 'completed' delta also drives progress to 1.0. Event-free by design — the
    reflection report is already the event-bearing artifact."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE goals SET properties = properties || %s::jsonb, "
                "progress = CASE WHEN %s = 'completed' THEN 1.0 ELSE progress END "
                "WHERE id = %s AND scope = %s AND valid_to IS NULL",
                (json.dumps({"last_reflection": delta}), str(delta.get("status", "")),
                 goal_id, scope),
            )
            return cur.rowcount == 1


async def invalidate_goal(pool: Any, publisher: Any, scope: str, goal_id: str,
                          reason: str = "") -> dict:
    """For goals that were WRONG (mis-entered, superseded) — not for completed
    ones (set status='achieved' via update_goal instead)."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE goals SET valid_to = now() "
                "WHERE id = %s AND scope = %s AND valid_to IS NULL RETURNING id",
                (goal_id, scope),
            )
            if await cur.fetchone() is None:
                raise ValueError(f"goal {goal_id!r} not found (or already invalidated)")
        await _stage_and_emit(publisher, conn, "goal.invalidated", goal_id, scope,
                              {"goal_id": goal_id, "reason": reason[:300]})
    return {"id": goal_id, "invalidated": True}


async def list_goals(pool: Any, scope: str, *, status: str | None = None,
                     include_invalidated: bool = False) -> list[dict]:
    where = ["scope = %s"]
    params: list[Any] = [scope]
    if not include_invalidated:
        where.append("valid_to IS NULL")
    if status:
        where.append("status = %s")
        params.append(status)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, parent_id, title, description, horizon, status, priority, "
                "identity_refs, project_refs, success_criteria, progress, properties, "
                "valid_from, valid_to FROM goals "
                f"WHERE {' AND '.join(where)} ORDER BY priority DESC, valid_from",
                params,
            )
            rows = await cur.fetchall()
    return [_goal_row(r) for r in rows]


def _goal_row(r) -> dict:
    return {
        "id": r[0], "parent_id": r[1], "title": r[2], "description": r[3],
        "horizon": r[4], "status": r[5], "priority": r[6],
        "identity_refs": list(r[7] or []), "project_refs": list(r[8] or []),
        "success_criteria": r[9], "progress": r[10],
        "properties": r[11] if isinstance(r[11], dict) else {},
        "valid_from": r[12].isoformat() if r[12] else None,
        "valid_to": r[13].isoformat() if r[13] else None,
    }


async def get_goal(pool: Any, scope: str, goal_id: str) -> dict:
    """Goal + children + its decisions (the /goals/[id] payload)."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, parent_id, title, description, horizon, status, priority, "
                "identity_refs, project_refs, success_criteria, progress, properties, "
                "valid_from, valid_to FROM goals WHERE id = %s AND scope = %s",
                (goal_id, scope),
            )
            row = await cur.fetchone()
            if row is None:
                raise ValueError(f"goal {goal_id!r} not found")
            await cur.execute(
                "SELECT id, title, status, progress FROM goals "
                "WHERE parent_id = %s AND valid_to IS NULL ORDER BY priority DESC",
                (goal_id,),
            )
            children = await cur.fetchall()
            await cur.execute(
                "SELECT id, title, chosen, reversibility, review_at, outcome, decided_at "
                "FROM decisions WHERE goal_id = %s ORDER BY decided_at DESC LIMIT 50",
                (goal_id,),
            )
            decisions = await cur.fetchall()
    goal = _goal_row(row)
    goal["children"] = [{"id": c[0], "title": c[1], "status": c[2], "progress": c[3]}
                        for c in children]
    goal["decisions"] = [
        {"id": d[0], "title": d[1], "chosen": d[2], "reversibility": d[3],
         "review_at": d[4].isoformat() if d[4] else None, "outcome": d[5],
         "decided_at": d[6].isoformat() if d[6] else None}
        for d in decisions
    ]
    return goal


# ── decisions ─────────────────────────────────────────────────────────────────

async def record_decision(
    pool: Any, publisher: Any, scope: str, *,
    title: str,
    chosen: str,
    rationale: str,
    context: str | None = None,
    options_considered: list | None = None,
    reversibility: str | None = None,
    goal_id: str | None = None,
    review_at: str | None = None,           # ISO timestamp or None
    predicted_outcome: str | None = None,   # the falsifiable bet — scored at review
    prediction_confidence: float | None = None,  # 0..1
    source_episode_id: str | None = None,
) -> dict:
    dec_id = mint("dec")
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO decisions (id, scope, title, context, options_considered, "
                "chosen, rationale, reversibility, goal_id, review_at, "
                "predicted_outcome, prediction_confidence, source_episode_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "RETURNING id, decided_at",
                (dec_id, scope, title, context, json.dumps(options_considered or []),
                 chosen, rationale, reversibility, goal_id, review_at,
                 predicted_outcome, prediction_confidence, source_episode_id),
            )
            row = await cur.fetchone()
        await _stage_and_emit(publisher, conn, "decision.recorded", dec_id, scope,
                              {"dec_id": dec_id, "title": title, "chosen": chosen[:200],
                               "reversibility": reversibility, "goal_id": goal_id})
    return {"id": row[0], "title": title, "decided_at": row[1].isoformat() if row[1] else None}


async def review_decision(pool: Any, publisher: Any, scope: str, dec_id: str, *,
                          outcome: str,
                          valence: str = "mixed",
                          matched_prediction: bool | None = None,
                          lesson: str | None = None,
                          applies_to_entities: list[str] | None = None,
                          embedder: Any = None) -> dict:
    """Review a decision and close the loop: record a structured, Brier-scored
    outcome (cognition.decision_loop), and — if a distilled `lesson` is supplied
    — reinforce-or-create it and mirror it into the knowledge graph.

    `decisions.outcome` is still kept as the human-readable cache. `embedder`
    (any object with async .embed([...])) makes the outcome/lesson semantically
    retrievable; if None, they are recorded but not embedded.
    """
    from cognition.decision_loop import (
        record_outcome_async, distill_or_reinforce_lesson_async, mirror_lesson_to_kg_async,
    )

    async def _embed(text: str):
        if embedder is None:
            return None
        return (await embedder.embed([text]))[0]

    result: dict = {"id": dec_id, "outcome": outcome}
    async with pool.connection() as conn:
        # Existence + scope check (also keeps the flat cache in sync).
        async with conn.cursor() as cur:
            await cur.execute("SELECT id FROM decisions WHERE id = %s AND scope = %s",
                              (dec_id, scope))
            if await cur.fetchone() is None:
                raise ValueError(f"decision {dec_id!r} not found")

        out_id = await record_outcome_async(
            conn, scope=scope, decision_id=dec_id, summary=outcome, valence=valence,
            embedding=await _embed(outcome), matched_prediction=matched_prediction,
        )
        result["outcome_id"] = out_id

        if lesson:
            les_id, action = await distill_or_reinforce_lesson_async(
                conn, scope=scope, statement=lesson, embedding=await _embed(lesson),
                derived_from_outcomes=[out_id], applies_to_entities=applies_to_entities or [],
            )
            ent_id = await mirror_lesson_to_kg_async(conn, scope=scope, lesson_id=les_id)
            result["lesson_id"] = les_id
            result["lesson_action"] = action
            result["lesson_entity_id"] = ent_id

        await _stage_and_emit(publisher, conn, "decision.reviewed", dec_id, scope,
                              {"dec_id": dec_id, "outcome": outcome[:300],
                               "valence": valence, "outcome_id": out_id})
    return result


async def list_decisions(pool: Any, scope: str, *, due_for_review: bool = False,
                         limit: int = 100) -> list[dict]:
    where = "scope = %s"
    if due_for_review:
        where += " AND outcome IS NULL AND review_at IS NOT NULL AND review_at <= now()"
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, title, context, options_considered, chosen, rationale, "
                "reversibility, goal_id, review_at, outcome, audit_id, decided_at, reviewed_at "
                f"FROM decisions WHERE {where} ORDER BY decided_at DESC LIMIT %s",
                (scope, limit),
            )
            rows = await cur.fetchall()
    return [
        {"id": r[0], "title": r[1], "context": r[2],
         "options_considered": r[3] if isinstance(r[3], list) else [],
         "chosen": r[4], "rationale": r[5], "reversibility": r[6], "goal_id": r[7],
         "review_at": r[8].isoformat() if r[8] else None, "outcome": r[9],
         "audit_id": r[10],
         "decided_at": r[11].isoformat() if r[11] else None,
         "reviewed_at": r[12].isoformat() if r[12] else None}
        for r in rows
    ]


# ── opportunities ─────────────────────────────────────────────────────────────

async def create_opportunity(
    pool: Any, publisher: Any, scope: str, *,
    title: str,
    description: str,
    source: str = "manual",
    evidence_refs: list[str] | None = None,
    novelty: float | None = None,
    value_est: float | None = None,
    feasibility: float | None = None,
) -> dict:
    opp_id = mint("opp")
    parts = [x for x in (novelty, value_est, feasibility) if x is not None]
    score = round(sum(parts) / len(parts), 4) if parts else None
    status = "scored" if score is not None else "detected"
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO opportunities (id, scope, title, description, source, "
                "evidence_refs, novelty, value_est, feasibility, score, status) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (opp_id, scope, title, description, source, evidence_refs or [],
                 novelty, value_est, feasibility, score, status),
            )
            await cur.fetchone()
        await _stage_and_emit(publisher, conn, "opportunity.detected", opp_id, scope,
                              {"opp_id": opp_id, "title": title, "source": source,
                               "score": score})
    return {"id": opp_id, "title": title, "status": status, "score": score}


async def resolve_opportunity(pool: Any, publisher: Any, scope: str, opp_id: str, *,
                              accept: bool, resolution: str) -> dict:
    """Accept (resolution = the goal_/prj_ id it became) or reject (= reason)."""
    status = "accepted" if accept else "rejected"
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE opportunities SET status = %s, resolution = %s, resolved_at = now() "
                "WHERE id = %s AND scope = %s AND status IN ('detected','scored') "
                "RETURNING id",
                (status, resolution, opp_id, scope),
            )
            if await cur.fetchone() is None:
                raise ValueError(f"opportunity {opp_id!r} not found or already resolved")
        await _stage_and_emit(publisher, conn, "opportunity.resolved", opp_id, scope,
                              {"opp_id": opp_id, "status": status, "resolution": resolution[:300]})
    return {"id": opp_id, "status": status, "resolution": resolution}


async def list_opportunities(pool: Any, scope: str, *, status: str | None = None,
                             limit: int = 100) -> list[dict]:
    where = "scope = %s"
    params: list[Any] = [scope]
    if status:
        where += " AND status = %s"
        params.append(status)
    params.append(limit)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, title, description, source, evidence_refs, novelty, value_est, "
                "feasibility, score, status, resolution, detected_at, resolved_at "
                f"FROM opportunities WHERE {where} ORDER BY detected_at DESC LIMIT %s",
                params,
            )
            rows = await cur.fetchall()
    return [
        {"id": r[0], "title": r[1], "description": r[2], "source": r[3],
         "evidence_refs": list(r[4] or []), "novelty": r[5], "value_est": r[6],
         "feasibility": r[7], "score": r[8], "status": r[9], "resolution": r[10],
         "detected_at": r[11].isoformat() if r[11] else None,
         "resolved_at": r[12].isoformat() if r[12] else None}
        for r in rows
    ]


# ── Goal derivation from memories ─────────────────────────────────────────────

async def derive_goals_from_memories(
    pool: Any, publisher: Any, scope: str,
    llm_client: Any = None, llm_model: str = "gpt-4o-mini",
) -> dict:
    """Scan episodes + identity for recurring themes → propose as goal candidates."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=30)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, content, created_at FROM episodes "
                "WHERE scope = %s AND created_at >= %s ORDER BY created_at DESC LIMIT 200",
                (scope, cutoff),
            )
            episode_rows = await cur.fetchall()
            await cur.execute(
                "SELECT predicate, object, confidence FROM identity_facts "
                "WHERE scope = %s AND valid_to IS NULL ORDER BY confidence DESC LIMIT 30",
                (scope,),
            )
            identity_rows = await cur.fetchall()
            await cur.execute(
                "SELECT id, title FROM goals WHERE scope = %s AND status = 'active' "
                "AND valid_to IS NULL", (scope,),
            )
            existing = {r[1].lower() for r in await cur.fetchall()}

    candidates: list[dict] = []
    if llm_client is not None:
        try:
            episodes_text = "\n".join(
                f"- [{r[2].isoformat()[:10]}] {r[1][:200]}" for r in episode_rows[:60]
            )
            identity_text = "\n".join(
                f"- {r[0]}: {r[1]} (conf {r[2]:.1f})" for r in identity_rows[:20]
            )
            existing_text = "; ".join(sorted(existing)[:10]) if existing else "(none)"
            prompt = (
                "From the user's recent episodes and identity below, identify 1-4 "
                "LATENT GOALS — recurring themes/projects not yet formalized. "
                "Skip active goals: " + existing_text + "\n\n"
                "Return JSON: {\"goals\": [{\"title\":\"...\",\"description\":\"...\","
                "\"horizon\":\"month|quarter|year\"}]}\n\n"
                f"EPISODES:\n{episodes_text}\n\nIDENTITY:\n{identity_text}"
            )
            response = await llm_client.chat.completions.create(
                model=llm_model, messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}, temperature=0.3,
            )
            from shared.llm import first_json
            data = first_json(response.choices[0].message.content, default={})
            raw = data if isinstance(data, list) else data.get("goals", [])
            for c in (raw or []):
                if isinstance(c, dict) and str(c.get("title", "")).strip():
                    candidates.append(c)
        except Exception as e:
            log.warning("LLM goal derivation failed: %s", e)

    if not candidates:
        from collections import Counter
        mentions: Counter[str] = Counter()
        for _, content, _ in episode_rows:
            text = (content or "").lower()
            for kw in ["build", "create", "learn", "start", "improve", "launch", "write", "make"]:
                if kw in text:
                    idx = text.find(kw)
                    snippet = text[idx:idx + 60].strip()
                    if snippet and snippet != " ":
                        mentions[snippet] += 1
        for snippet, count in mentions.most_common(4):
            if count >= 3 and snippet.lower() not in existing:
                candidates.append({
                    "title": snippet[:80].title(),
                    "description": f"Mentioned {count}x in recent episodes.",
                    "horizon": "month",
                })

    created = []
    for c in candidates:
        title = str(c.get("title", ""))[:300]
        if title.lower() in existing:
            continue
        goal = await create_goal(
            pool, publisher, scope, title=title,
            description=str(c.get("description", ""))[:4000],
            horizon=c.get("horizon") if c.get("horizon") in ("month", "quarter", "year") else "month",
        )
        created.append(goal)
        existing.add(title.lower())
    return {"candidates_found": len(candidates), "goals_created": len(created), "goals": created}


# ── Decision extraction from memories ─────────────────────────────────────────

async def extract_decisions_from_memories(
    pool: Any, publisher: Any, scope: str, *,
    auto_record: bool = False,
    llm_client: Any = None, llm_model: str = "gpt-4o-mini",
) -> dict:
    """Scan recent episodes for decision points. Auto-records if auto_record=True."""
    import re as _re
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=14)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, content, created_at FROM episodes "
                "WHERE scope = %s AND created_at >= %s ORDER BY created_at DESC LIMIT 100",
                (scope, cutoff),
            )
            episode_rows = await cur.fetchall()
    if not episode_rows:
        return {"proposed": 0, "recorded": 0, "episodes_scanned": 0}

    proposals: list[dict] = []
    if llm_client is not None:
        try:
            episodes_text = "\n".join(
                f"- [{r[2].isoformat()[:10]}] {r[1][:300]}" for r in episode_rows[:40]
            )
            prompt = (
                "Extract concrete DECISIONS from the entries. "
                "Return JSON: {\"decisions\": [{\"title\":\"...\",\"chosen\":\"...\","
                "\"rationale\":\"...\",\"reversibility\":\"reversible|costly|one_way\"}]}\n\n"
                + episodes_text
            )
            response = await llm_client.chat.completions.create(
                model=llm_model, messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"}, temperature=0.2,
            )
            from shared.llm import first_json
            data = first_json(response.choices[0].message.content, default={})
            raw = data if isinstance(data, list) else data.get("decisions", [])
            for d in raw:
                if isinstance(d, dict) and d.get("title") and d.get("chosen"):
                    proposals.append(d)
        except Exception as e:
            log.warning("LLM decision extraction failed: %s", e)

    if not proposals:
        for epi_id, content, _ in episode_rows:
            text = content or ""
            for match in _re.finditer(
                r"(?:decided|chose|picked|went with|switched to|committed to)\s+(.+?)(?:\.|,|;|$)",
                text, _re.IGNORECASE,
            ):
                choice = match.group(1).strip()
                if len(choice) < 5:
                    continue
                proposals.append({
                    "title": choice[:80], "chosen": choice[:200],
                    "rationale": "", "reversibility": "reversible",
                })

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT title FROM decisions WHERE scope = %s", (scope,),
            )
            existing_titles = {r[0].lower() for r in await cur.fetchall()}

    recorded = 0
    for p in proposals:
        if p["title"].lower() in existing_titles:
            continue
        existing_titles.add(p["title"].lower())
        if auto_record:
            try:
                await record_decision(
                    pool, publisher, scope,
                    title=p["title"], chosen=p["chosen"],
                    rationale=p.get("rationale", ""),
                    reversibility=p.get("reversibility", "reversible"),
                )
                recorded += 1
            except Exception as e:
                log.warning("Failed to record decision %r: %s", p.get("title"), e)
    return {"proposed": len(proposals), "recorded": recorded,
            "proposals": proposals[:5], "episodes_scanned": len(episode_rows)}
