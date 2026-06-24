"""Reflection engine — structured analysis of episodic memory.

Produces InsightReports (rpt_) with findings[], goal_deltas[], identity_candidates[].
Feeds identity engine with proposed identity facts.

Three cadences:
  daily   — lightweight, heuristic-only, no identity sync (no LLM cost)
  weekly  — standard depth, LLM-augmented, identity + goal sync
  monthly — deep, includes mental model review, identity + goal sync

Pipeline:
  1. Query recent episodes (time window + scope)
  2. Analyze patterns (recurring themes, shifted preferences, new goals)
  3. Track goal progress
  4. Extract identity candidates
  5. Write findings via add() at hypothesis epistemic status
  6. Feed identity candidates to propose_identity_fact()

See: ~/hitenos-architecture/13-reflection-engine.md
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Literal

from shared.types.ulid import mint
from shared.llm import first_json

log = logging.getLogger("curlyos.reflection")

# Conversation-artifact words to exclude from noun-phrase theme findings —
# episodes are formatted as "[turn N] User: ... Assistant: ...", so these
# capitalized tokens are structural noise, not meaningful themes.
_ARTIFACT_WORDS = {
    "user", "assistant", "turn", "session", "system", "hiten", "the", "what",
    "can", "here", "this", "that", "you", "your", "and", "but", "for", "with",
    "from", "have", "will", "would", "should", "could", "note", "based", "done",
    "okay", "yes", "sure", "let", "now", "also", "morning", "good", "no", "test",
    "fixed", "run", "set", "add", "use", "got",
}

ReportType = Literal["daily", "weekly", "monthly"]

# ── Cadence config ────────────────────────────────────────────────────────────

_CADENCE: dict[ReportType, dict] = {
    "daily":   {"window_days": 1,  "episode_limit": 50,  "themes_limit": 0,   "mental_model_review": False, "identity_sync": False, "llm_recommended": False},
    "weekly":  {"window_days": 7,  "episode_limit": 200, "themes_limit": 10,  "mental_model_review": False, "identity_sync": True,  "llm_recommended": True},
    "monthly": {"window_days": 30, "episode_limit": 500, "themes_limit": 15,  "mental_model_review": True,  "identity_sync": True,  "llm_recommended": True},
}


# ── DDL for reflection reports ──────────────────────────────────────────────

REFLECTION_DDL = """
CREATE TABLE IF NOT EXISTS reflection_reports (
  id                text        PRIMARY KEY,
  scope             text        NOT NULL,
  report_type       text        NOT NULL DEFAULT 'weekly',  -- daily | weekly | monthly | manual
  time_window_start timestamptz NOT NULL,
  time_window_end   timestamptz NOT NULL,
  episodes_scanned  integer     NOT NULL DEFAULT 0,
  findings          jsonb       NOT NULL DEFAULT '[]',
  goal_deltas       jsonb       NOT NULL DEFAULT '[]',
  identity_candidates jsonb     NOT NULL DEFAULT '[]',
  summary           text,
  created_at        timestamptz NOT NULL DEFAULT now()
);
"""


# ── Reflection prompts ─────────────────────────────────────────────────────

DAILY_ANALYSIS_PROMPT = """Analyze the following conversation episodes from today. The user is Hiten.

Extract:
1. FINDINGS — what happened today, what was the main focus, any notable patterns or shifts (confidence 0.0-1.0).
2. GOAL_DELTAS — goals mentioned, progress made, blockers.
3. MOOD_ENERGY — inferred mood and energy level for today.

Keep findings concise — this is a daily snapshot, not deep analysis.

No identity extraction needed (daily is too short for durable traits).

Recent episodes:
{episodes}

Respond as JSON:
{{
  "findings": [{{"statement": "...", "confidence": 0.8, "tags": ["..."]}}],
  "goal_deltas": [{{"goal": "...", "status": "on_track|blocked|new|completed", "detail": "..."}}],
  "mood_energy": {{"mood": "focused|tired|anxious|energetic|calm|...", "valence": 0.5, "energy": 0.5}}
}}
"""

WEEKLY_ANALYSIS_PROMPT = """Analyze the following conversation episodes from the past week. The user is Hiten.

Extract:
1. FINDINGS — net-new facts, shifted preferences, or recurring patterns (confidence 0.0-1.0).
2. GOAL_DELTAS — goals mentioned, progress made, blockers.
3. IDENTITY_CANDIDATES — durable facts about WHO HITEN IS (stable traits, preferences, roles, values, habits) as (predicate, object, confidence).

Strict rules for IDENTITY_CANDIDATES:
- predicate: a short snake_case attribute — e.g. occupation, prefers_editor, works_on, values, sleep_schedule, dietary_preference, personality_trait. Do NOT invent meta-predicates like "expressed_self_reference", "said", "asked", or "mentioned".
- object: a SHORT concrete value, a few words at most (a name, tool, preference, trait). NEVER a sentence, a task/request, or copied conversation text. Never include "[turn", "User:", "Assistant:", or any transcript fragment.
- Only STABLE facts true across time — NOT one-off tasks or requests Hiten made in this conversation (e.g. "asked to set up a cron job" is NOT identity).
- Good: {{"predicate": "prefers_editor", "object": "Zed", "confidence": 0.9}}
- Bad:  {{"predicate": "expressed_self_reference", "object": "[turn 2] User: can you setup a cron job ..."}}
- Findings and identity are hypotheses; confidence ≥ 0.75 auto-promotes identity to canonical. Only net-new observations — don't repeat the existing identity context.

Recent episodes:
{episodes}

Current identity context:
{identity_context}

{graph_context}

Respond as JSON:
{{
  "findings": [{{"statement": "...", "confidence": 0.8, "tags": ["..."]}}],
  "goal_deltas": [{{"goal": "...", "status": "on_track|blocked|new|completed", "detail": "..."}}],
  "identity_candidates": [{{"predicate": "...", "object": "...", "confidence": 0.8}}]
}}
"""

MONTHLY_ANALYSIS_PROMPT = """Analyze the following conversation episodes from the past month. The user is Hiten.

Extract:
1. FINDINGS — net-new facts, shifted preferences, major patterns, or trends (confidence 0.0-1.0).
2. GOAL_DELTAS — goals mentioned, progress made, blockers.
3. IDENTITY_CANDIDATES — durable facts about WHO HITEN IS (stable traits, preferences, roles, values, habits) as (predicate, object, confidence).
4. MENTAL_MODEL_REVIEW — which mental models were used, which should be created or updated.

Strict rules for IDENTITY_CANDIDATES:
- predicate: a short snake_case attribute — e.g. occupation, prefers_editor, works_on, values, sleep_schedule, dietary_preference, personality_trait. Do NOT invent meta-predicates like "expressed_self_reference", "said", "asked", or "mentioned".
- object: a SHORT concrete value, a few words at most (a name, tool, preference, trait). NEVER a sentence, a task/request, or copied conversation text.
- Only STABLE facts true across time — NOT one-off tasks or requests.

{identity_context}

{graph_context}

{mental_model_context}

Respond as JSON:
{{
  "findings": [{{"statement": "...", "confidence": 0.8, "tags": ["..."]}}],
  "goal_deltas": [{{"goal": "...", "status": "on_track|blocked|new|completed", "detail": "..."}}],
  "identity_candidates": [{{"predicate": "...", "object": "...", "confidence": 0.8}}],
  "mental_model_review": [{{"model": "...", "action": "update|create|retire", "reason": "..."}}]
}}
"""


# ── Core reflection logic ──────────────────────────────────────────────────

async def _fetch_goal_targets(pool: Any, scope: str) -> list[tuple]:
    """Goal texts to track progress against, as (id, text, valid_from, created_at).

    Prefers first-class goals (Phase-G `goals` table: title + success criteria,
    goal_ ids) so deltas land back on real goal rows; falls back to the legacy
    ILIKE-over-memories scan when no goal rows exist or the table is absent."""
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, title || COALESCE(' — ' || success_criteria, ''), "
                    "valid_from, valid_from FROM goals "
                    "WHERE scope = %s AND status = 'active' AND valid_to IS NULL "
                    "ORDER BY priority DESC, valid_from",
                    (scope,),
                )
                rows = await cur.fetchall()
        if rows:
            return rows
    except Exception:  # noqa: BLE001 — older DBs lack the table; fall back
        pass
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, statement, valid_from, created_at FROM memories "
                "WHERE scope = %s AND valid_to IS NULL AND statement ILIKE %s "
                "ORDER BY created_at",
                (scope, "%goal%"),
            )
            return await cur.fetchall()


async def run_reflection(
    pool: Any,
    publisher: Any,
    scope: str,
    report_type: ReportType = "weekly",
    llm_client: Any = None,
    llm_model: str = "gpt-4o-mini",
) -> dict:
    """Run a reflection pass over recent episodes.

    Unified entry point for all three cadences. Branches internally by
    report_type for episode query window, LLM prompt depth, and post-processing
    (identity sync, mental model review, mood extraction).

    When llm_client is None and the cadence recommends one (weekly/monthly),
    falls back to heuristic-only analysis.

    Returns dict with rpt_id, episodes_scanned, findings count, etc.
    """
    import json
    from memory.governance import add, record_episode
    from identity import get_identity_context

    cfg = _CADENCE.get(report_type, _CADENCE["weekly"])
    window_days = cfg["window_days"]
    episode_limit = cfg["episode_limit"]
    themes_limit = cfg["themes_limit"]
    do_mental_model_review = cfg["mental_model_review"]
    do_identity_sync = cfg["identity_sync"]

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=window_days)
    rpt_id = mint("rpt")

    # 1. Fetch episodes from the time window
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, content, created_at FROM episodes "
                "WHERE scope = %s AND created_at >= %s "
                "ORDER BY created_at DESC LIMIT %s",
                (scope, window_start, episode_limit),
            )
            episode_rows = await cur.fetchall()

    episodes_scanned = len(episode_rows)
    if episodes_scanned == 0:
        return {
            "rpt_id": rpt_id, "report_type": report_type,
            "episodes_scanned": 0, "findings": 0,
            "goal_deltas": 0, "identity_candidates": 0,
        }

    # 2. Fetch identity context + knowledge-graph context (for weekly/monthly)
    identity_ctx = await get_identity_context(pool, scope) if do_identity_sync else {}
    graph_ctx = ""
    mental_model_ctx = ""
    if llm_client is not None and report_type in ("weekly", "monthly"):
        from knowledge.graph import graph_context as _graph_ctx
        graph_ctx = await _graph_ctx(pool, scope)
        if do_mental_model_review:
            try:
                from cognition.meta import mental_model_context as _mm_ctx
                mental_model_ctx = await _mm_ctx(pool, scope)
            except Exception:
                mental_model_ctx = ""

    # 3. Run analysis
    if llm_client is not None and (cfg["llm_recommended"] or report_type == "daily"):
        if report_type == "daily":
            findings, goal_deltas, identity_candidates, mood_energy = (
                await _analyze_daily_llm(llm_client, llm_model, episode_rows)
            )
        elif report_type == "monthly":
            findings, goal_deltas, identity_candidates = await _analyze_monthly_llm(
                llm_client, llm_model, episode_rows, identity_ctx, graph_ctx, mental_model_ctx,
            )
        else:
            findings, goal_deltas, identity_candidates = await _analyze_with_llm(
                llm_client, llm_model, episode_rows, identity_ctx, graph_ctx,
            )
    else:
        findings, goal_deltas, identity_candidates = _analyze_heuristic(episode_rows, identity_ctx)

    # 4. Recurring themes from the knowledge graph (weekly/monthly)
    recurring_themes = []
    if themes_limit > 0:
        recurring_themes = await _kg_recurring_themes(pool, scope, limit=themes_limit)

    # 5. Track goal progress
    all_goal_deltas: list[dict] = list(goal_deltas)
    goal_rows = await _fetch_goal_targets(pool, scope)
    for gid, gstmt, gvf, _gca in goal_rows:
        goal_lower = gstmt.lower()
        mentioned = any(goal_lower[:20] in r[1].lower() for r in episode_rows)
        # Don't duplicate if LLM already reported on this goal
        if not any(d.get("goal_id") == gid for d in all_goal_deltas):
            all_goal_deltas.append({
                "goal_id": gid,
                "statement": gstmt,
                "status": "active" if mentioned else "stale",
                "valid_from": gvf.isoformat() if gvf else None,
            })

    # 6. Build findings list (LLM findings first, then themes + goals)
    all_findings: list[dict] = list(findings)
    for theme in recurring_themes:
        all_findings.append({
            "statement": f"Recurring theme: {theme['phrase']} (connected to {theme['count']} things in the knowledge graph)",
            "confidence": min(0.5 + theme["count"] * 0.05, 0.9),
            "tags": ["theme"],
        })
    for gd in all_goal_deltas:
        all_findings.append({
            "statement": f"Goal '{gd.get('statement', gd.get('goal', ''))[:60]}' is {gd.get('status', 'unknown')}",
            "confidence": 0.7,
            "tags": ["goals"],
        })

    # 7. Mental model review findings (monthly only)
    if do_mental_model_review:
        mental_model_findings = await _mental_model_review(pool, scope, episode_rows)
        all_findings.extend(mental_model_findings)

    # 8. Write findings as hypothesis-status facts
    for f in all_findings:
        try:
            epi = await record_episode(pool, publisher, scope,
                content=f"[reflection] {f['statement']}",
                source_ref=f"reflection:{report_type}")
            await add(pool, publisher, scope,
                statement=f["statement"],
                source_episode_id=epi["epi_id"],
                epistemic_status="hypothesis")
        except Exception as e:
            log.warning("Failed to write reflection finding: %s", e)

    # 9. Write identity candidates (weekly/monthly only)
    identity_promoted = 0
    identity_skipped = 0
    if do_identity_sync and identity_candidates:
        from identity import propose_identity_fact as _propose_idf
        for ic in identity_candidates:
            try:
                source_epi = episode_rows[0][0] if episode_rows else None
                if source_epi and ic.get("confidence", 0) >= 0.75:
                    await _propose_idf(
                        pool, publisher, scope,
                        predicate=ic["predicate"],
                        object=ic["object"],
                        confidence=min(ic.get("confidence", 0.0), 0.7),
                        source_episode_id=source_epi,
                    )
                    identity_promoted += 1
            except Exception as e:
                identity_skipped += 1
                log.warning("Failed to write identity candidate: %s", e)

    # 10. Store report
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO reflection_reports "
                "(id, scope, report_type, time_window_start, time_window_end, "
                "episodes_scanned, findings, goal_deltas, identity_candidates, summary) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (rpt_id, scope, report_type, window_start, now,
                 episodes_scanned,
                 json.dumps(all_findings), json.dumps(all_goal_deltas),
                 json.dumps(identity_candidates),
                 f"{report_type.title()} reflection: {len(all_findings)} findings, "
                 f"{len(identity_candidates)} identity candidates"),
            )

    return {
        "rpt_id": rpt_id,
        "report_type": report_type,
        "episodes_scanned": episodes_scanned,
        "findings": len(all_findings),
        "goal_deltas": len(all_goal_deltas),
        "identity_candidates": len(identity_candidates),
        "identity_promoted": identity_promoted,
        "identity_skipped": identity_skipped,
    }


# ── Backward-compatible wrappers ──────────────────────────────────────────────

async def run_weekly_reflection(
    pool: Any, publisher: Any, scope: str,
    window_days: int = 7, llm_client: Any = None,
    llm_model: str = "openai/gpt-4o-mini",
) -> dict:
    """Backward-compatible wrapper — runs a weekly reflection."""
    return await run_reflection(
        pool, publisher, scope, report_type="weekly",
        llm_client=llm_client, llm_model=llm_model,
    )


async def run_monthly_reflection(
    pool: Any, publisher: Any, scope: str,
    llm_client: Any = None, llm_model: str = "openai/gpt-4o-mini",
) -> dict:
    """Backward-compatible wrapper — runs a monthly reflection."""
    return await run_reflection(
        pool, publisher, scope, report_type="monthly",
        llm_client=llm_client, llm_model=llm_model,
    )


async def run_daily_reflection(
    pool: Any, publisher: Any, scope: str,
    llm_client: Any = None, llm_model: str = "openai/gpt-4o-mini",
) -> dict:
    """Run a lightweight daily reflection (heuristic-only by default, LLM off)."""
    return await run_reflection(
        pool, publisher, scope, report_type="daily",
        llm_client=llm_client, llm_model=llm_model,
    )


# ── LLM analysis helpers ──────────────────────────────────────────────────────

async def _analyze_with_llm(llm_client, model, episodes, identity_ctx, graph_ctx=""):
    """LLM-powered weekly reflection analysis."""
    import json

    episodes_text = "\n".join(f"- [{r[2].isoformat()[:10]}] {r[1][:200]}" for r in episodes[:50])
    identity_text = json.dumps(identity_ctx, indent=2, default=str)

    prompt = WEEKLY_ANALYSIS_PROMPT.format(
        episodes=episodes_text, identity_context=identity_text,
        graph_context=graph_ctx or "(knowledge graph empty)",
    )

    try:
        response = await llm_client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        data = first_json(response.choices[0].message.content, default={})
        return (
            data.get("findings", []),
            data.get("goal_deltas", []),
            data.get("identity_candidates", []),
        )
    except Exception as e:
        log.warning("LLM reflection failed: %s, using heuristic", e)
        return _analyze_heuristic(episodes, identity_ctx)


async def _analyze_daily_llm(llm_client, model, episodes):
    """LLM-powered daily reflection — lighter, no identity extraction."""
    import json

    episodes_text = "\n".join(f"- [{r[2].isoformat()[:10]}] {r[1][:200]}" for r in episodes[:20])

    prompt = DAILY_ANALYSIS_PROMPT.format(episodes=episodes_text)

    try:
        response = await llm_client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        data = first_json(response.choices[0].message.content, default={})
        findings = data.get("findings", [])
        goal_deltas = data.get("goal_deltas", [])
        identity_candidates = []
        # If mood_energy was extracted, add it as a finding
        mood_energy = data.get("mood_energy")
        if mood_energy and isinstance(mood_energy, dict):
            findings.append({
                "statement": f"Mood: {mood_energy.get('mood', 'neutral')} "
                             f"(valence={mood_energy.get('valence', 0.5):.1f}, "
                             f"energy={mood_energy.get('energy', 0.5):.1f})",
                "confidence": 0.6,
                "tags": ["mood"],
            })
        return findings, goal_deltas, identity_candidates, mood_energy
    except Exception as e:
        log.warning("LLM daily reflection failed: %s, using heuristic", e)
        f, gd, ic = _analyze_heuristic(episodes, {})
        return f, gd, ic, None


async def _analyze_monthly_llm(llm_client, model, episodes, identity_ctx, graph_ctx="", mental_model_ctx=""):
    """LLM-powered monthly reflection — includes mental model review."""
    import json

    episodes_text = "\n".join(f"- [{r[2].isoformat()[:10]}] {r[1][:200]}" for r in episodes[:80])
    identity_text = json.dumps(identity_ctx, indent=2, default=str)

    prompt = MONTHLY_ANALYSIS_PROMPT.format(
        episodes=episodes_text, identity_context=identity_text,
        graph_context=graph_ctx or "(knowledge graph empty)",
        mental_model_context=mental_model_ctx or "(no mental models recorded)",
    )

    try:
        response = await llm_client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        data = first_json(response.choices[0].message.content, default={})
        return (
            data.get("findings", []),
            data.get("goal_deltas", []),
            data.get("identity_candidates", []),
        )
    except Exception as e:
        log.warning("LLM monthly reflection failed: %s, using heuristic", e)
        return _analyze_heuristic(episodes, identity_ctx)


def _analyze_heuristic(episodes, identity_ctx):
    """Heuristic pattern detection — no LLM needed."""
    findings = []
    goal_deltas = []
    identity_candidates = []

    from collections import Counter
    tool_mentions = Counter()
    project_mentions = Counter()

    for _epi_id, content, _ingested_at in episodes:
        text = content.lower()
        for tool in ["zed", "vs code", "vim", "neovim", "cursor", "windsurf"]:
            if tool in text:
                tool_mentions[tool] += 1
        for proj in ["mintrix", "curlyos", "hitenos", "curly brackets"]:
            if proj in text:
                project_mentions[proj] += 1

    for tool, count in tool_mentions.most_common(3):
        if count >= 2:
            identity_candidates.append({
                "predicate": "prefers_editor" if tool in ("zed", "vs code", "cursor") else "uses_tool",
                "object": tool.title(),
                "confidence": min(0.5 + count * 0.1, 0.95),
            })

    for proj, count in project_mentions.most_common(3):
        if count >= 2:
            findings.append({
                "statement": f"{proj.title()} is an active focus area (mentioned {count}x this period)",
                "confidence": min(0.6 + count * 0.05, 0.9),
                "tags": ["goals", "projects"],
            })

    return findings, goal_deltas, identity_candidates


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _kg_recurring_themes(pool: Any, scope: str, limit: int = 15) -> list[dict]:
    """Recurring themes = the most-connected entities in the current knowledge
    graph (clean/typed/deduped). Returns [{"phrase","count"}] where count is graph degree."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "WITH deg AS ("
                "  SELECT eid, count(*) d FROM ("
                "    SELECT src_entity_id eid FROM knowledge_edges WHERE valid_to IS NULL "
                "    UNION ALL SELECT dst_entity_id FROM knowledge_edges WHERE valid_to IS NULL"
                "  ) x GROUP BY eid) "
                "SELECT e.name, COALESCE(d.d, 0) FROM knowledge_entities e "
                "LEFT JOIN deg d ON d.eid = e.id "
                "WHERE e.scope = %s AND e.valid_to IS NULL AND lower(e.name) <> 'hiten' "
                "AND COALESCE(d.d, 0) >= 2 ORDER BY COALESCE(d.d, 0) DESC, e.created_at ASC LIMIT %s",
                (scope, limit),
            )
            return [{"phrase": n, "count": d} for n, d in await cur.fetchall()]


async def _mental_model_review(pool: Any, scope: str, episode_rows: list) -> list[dict]:
    """Check which mental models were recently referenced — for monthly reflection."""
    findings: list[dict] = []
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT name, confidence, created_at FROM mental_models "
                    "WHERE scope = %s AND valid_to IS NULL "
                    "ORDER BY created_at DESC LIMIT 50",
                    (scope,),
                )
                mm_rows = await cur.fetchall()

        for mm_name, mm_conf, mm_updated in mm_rows:
            mm_mentioned = any(mm_name.lower() in r[1].lower() for r in episode_rows)
            findings.append({
                "statement": f"Mental model '{mm_name}' confidence={mm_conf:.2f}, "
                             f"{'referenced' if mm_mentioned else 'not referenced'} this month",
                "confidence": 0.65,
                "tags": ["mental_models"],
            })
    except Exception as e:
        log.warning("Mental model review failed: %s", e)
    return findings


# ── Report retrieval ──────────────────────────────────────────────────────────

async def get_reflection_reports(
    pool: Any,
    scope: str,
    limit: int = 10,
    report_type: str | None = None,
) -> list[dict]:
    """SELECT reflection reports ordered by most recent, optionally filtered by type."""
    query = (
        "SELECT id, scope, report_type, time_window_start, time_window_end, "
        "episodes_scanned, findings, goal_deltas, identity_candidates, summary, created_at "
        "FROM reflection_reports WHERE scope = %s"
    )
    params: list[Any] = [scope]
    if report_type:
        query += " AND report_type = %s"
        params.append(report_type)
    query += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, params)
            rows = await cur.fetchall()

    return [
        {
            "id": r[0], "scope": r[1], "report_type": r[2],
            "time_window_start": r[3].isoformat() if r[3] else None,
            "time_window_end": r[4].isoformat() if r[4] else None,
            "episodes_scanned": r[5], "findings": r[6],
            "goal_deltas": r[7], "identity_candidates": r[8],
            "summary": r[9], "created_at": r[10].isoformat() if r[10] else None,
        }
        for r in rows
    ]


async def get_report_detail(
    pool: Any,
    report_id: str,
) -> dict | None:
    """SELECT single reflection report with all JSONB fields expanded."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, scope, report_type, time_window_start, time_window_end, "
                "episodes_scanned, findings, goal_deltas, identity_candidates, summary, created_at "
                "FROM reflection_reports WHERE id = %s",
                (report_id,),
            )
            row = await cur.fetchone()

    if row is None:
        return None

    return {
        "id": row[0], "scope": row[1], "report_type": row[2],
        "time_window_start": row[3].isoformat() if row[3] else None,
        "time_window_end": row[4].isoformat() if row[4] else None,
        "episodes_scanned": row[5], "findings": row[6],
        "goal_deltas": row[7], "identity_candidates": row[8],
        "summary": row[9], "created_at": row[10].isoformat() if row[10] else None,
    }
