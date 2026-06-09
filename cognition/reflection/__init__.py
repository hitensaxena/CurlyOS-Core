"""Reflection engine — structured analysis of episodic memory.

Produces InsightReports (rpt_) with findings[], goal_deltas[], identity_candidates[].
Feeds identity engine with proposed identity facts.

Pipeline:
  1. Query recent episodes (time window + scope)
  2. Analyze patterns (recurring themes, shifted preferences, new goals)
  3. Track goal progress
  4. Extract identity candidates
  5. Write findings via add() at hypothesis epistemic status
  6. Feed identity candidates to propose_identity_fact()

Runs as Hermes cron: weekly (Monday 6am), monthly (1st 7am).

See: ~/hitenos-architecture/13-reflection-engine.md
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

from shared.types.ulid import mint
from shared.events import build_event

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


# ── DDL for reflection reports ──────────────────────────────────────────────

REFLECTION_DDL = """
CREATE TABLE IF NOT EXISTS reflection_reports (
  id                text        PRIMARY KEY,
  scope             text        NOT NULL,
  report_type       text        NOT NULL DEFAULT 'weekly',  -- weekly | monthly | manual
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

WEEKLY_ANALYSIS_PROMPT = """Analyze the following conversation episodes from the past week.

For each area, extract:
1. FINDINGS — new facts, shifted preferences, recurring patterns (with confidence 0.0-1.0)
2. GOAL_DELTAS — goals mentioned, progress made, blockers
3. IDENTITY_CANDIDATES — stable preferences/traits (predicate, object, confidence)

Rules:
- Findings are written at hypothesis status — never canonical without user confirmation
- Identity candidates need confidence ≥ 0.75 to be auto-promoted
- Be specific — "prefers Zed" ✅, "likes tools" ❌
- Don't repeat existing knowledge — only net-new observations

Recent episodes:
{episodes}

Current identity context:
{identity_context}

Respond as JSON:
{{
  "findings": [{{"statement": "...", "confidence": 0.8, "tags": ["..."]}}],
  "goal_deltas": [{{"goal": "...", "status": "on_track|blocked|new|completed", "detail": "..."}}],
  "identity_candidates": [{{"predicate": "...", "object": "...", "confidence": 0.8}}]
}}
"""


# ── Core reflection logic ──────────────────────────────────────────────────

async def run_reflection(
    pool: Any,
    publisher: Any,
    scope: str,
    report_type: str = "weekly",
    days_back: int = 7,
    llm_client: Any = None,
    llm_model: str = "gpt-4o-mini",
) -> dict:
    """Run a reflection pass over recent episodes.

    1. Fetch episodes from the time window
    2. Fetch current identity context
    3. If LLM available: run structured analysis
    4. If no LLM: run heuristic pattern detection
    5. Write findings + identity candidates
    6. Store the reflection report
    """
    from memory.governance import add, record_episode, list_episodes
    from identity import get_identity_context, propose_identity_fact

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=days_back)
    rpt_id = mint("rpt")

    # 1. Fetch recent episodes
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, content, ingested_at FROM episodes "
                "WHERE scope = %s AND ingested_at >= %s "
                "ORDER BY ingested_at DESC LIMIT 200",
                (scope, window_start),
            )
            episode_rows = await cur.fetchall()

    episodes_scanned = len(episode_rows)
    if episodes_scanned == 0:
        return {"rpt_id": rpt_id, "episodes_scanned": 0, "findings": 0, "identity_candidates": 0}

    # 2. Fetch identity context
    identity_ctx = await get_identity_context(pool, scope)

    # 3. Run analysis
    if llm_client is not None:
        findings, goal_deltas, identity_candidates = await _analyze_with_llm(
            llm_client, llm_model, episode_rows, identity_ctx
        )
    else:
        findings, goal_deltas, identity_candidates = _analyze_heuristic(
            episode_rows, identity_ctx
        )

    # 4. Write findings as hypothesis-status facts
    for f in findings:
        try:
            # Record analysis episode first (provenance)
            epi = await record_episode(pool, publisher, scope,
                content=f"[reflection] {f['statement']}",
                source_ref=f"reflection:{report_type}")
            # Add as hypothesis fact
            await add(pool, publisher, scope,
                statement=f["statement"],
                source_episode_id=epi["epi_id"],
                epistemic_status="hypothesis")
        except Exception as e:
            log.warning("Failed to write reflection finding: %s", e)

    # 5. Write identity candidates
    for ic in identity_candidates:
        try:
            # Find a source episode for the identity fact
            source_epi = episode_rows[0][0] if episode_rows else None
            if source_epi and ic.get("confidence", 0) >= 0.75:
                await propose_identity_fact(
                    pool, publisher, scope,
                    predicate=ic["predicate"],
                    object=ic["object"],
                    confidence=min(ic.get("confidence", 0.0), 0.7),  # inferences stay hypothesis
                    source_episode_id=source_epi,
                )
        except Exception as e:
            log.warning("Failed to write identity candidate: %s", e)

    # 6. Store report
    import json
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO reflection_reports "
                "(id, scope, report_type, time_window_start, time_window_end, "
                "episodes_scanned, findings, goal_deltas, identity_candidates, summary) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (rpt_id, scope, report_type, window_start, now,
                 episodes_scanned,
                 json.dumps(findings), json.dumps(goal_deltas),
                 json.dumps(identity_candidates),
                 f"{report_type} reflection: {len(findings)} findings, {len(identity_candidates)} identity candidates"),
            )

    return {
        "rpt_id": rpt_id,
        "report_type": report_type,
        "episodes_scanned": episodes_scanned,
        "findings": len(findings),
        "goal_deltas": len(goal_deltas),
        "identity_candidates": len(identity_candidates),
    }


async def _analyze_with_llm(llm_client, model, episodes, identity_ctx):
    """LLM-powered reflection analysis."""
    import json

    episodes_text = "\n".join(f"- [{r[2].isoformat()[:10]}] {r[1][:200]}" for r in episodes[:50])
    identity_text = json.dumps(identity_ctx, indent=2, default=str)

    prompt = WEEKLY_ANALYSIS_PROMPT.format(episodes=episodes_text, identity_context=identity_text)

    try:
        response = await llm_client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        data = json.loads(response.choices[0].message.content)
        return (
            data.get("findings", []),
            data.get("goal_deltas", []),
            data.get("identity_candidates", []),
        )
    except Exception as e:
        log.warning("LLM reflection failed: %s, using heuristic", e)
        return _analyze_heuristic(episodes, identity_ctx)


def _analyze_heuristic(episodes, identity_ctx):
    """Heuristic pattern detection — no LLM needed."""
    findings = []
    goal_deltas = []
    identity_candidates = []

    # Pattern: repeated mentions of a tool/project → identity candidate
    from collections import Counter
    tool_mentions = Counter()
    project_mentions = Counter()

    for epi_id, content, ingested_at in episodes:
        text = content.lower()
        # Detect tool/editor mentions
        for tool in ["zed", "vs code", "vim", "neovim", "cursor", "windsurf"]:
            if tool in text:
                tool_mentions[tool] += 1
        # Detect project mentions
        for proj in ["mintrix", "curlyos", "hitenos", "curly brackets"]:
            if proj in text:
                project_mentions[proj] += 1

    # If a tool is mentioned 3+ times, it's likely an identity fact
    for tool, count in tool_mentions.most_common(3):
        if count >= 2:
            identity_candidates.append({
                "predicate": "prefers_editor" if tool in ("zed", "vs code", "cursor") else "uses_tool",
                "object": tool.title(),
                "confidence": min(0.5 + count * 0.1, 0.95),
            })

    # If a project is mentioned 3+ times, it's a goal finding
    for proj, count in project_mentions.most_common(3):
        if count >= 2:
            findings.append({
                "statement": f"{proj.title()} is an active focus area (mentioned {count}x this period)",
                "confidence": min(0.6 + count * 0.05, 0.9),
                "tags": ["goals", "projects"],
            })

    return findings, goal_deltas, identity_candidates


# ── Weekly reflection (task API) ─────────────────────────────────────────────

async def run_weekly_reflection(
    pool: Any,
    publisher: Any,
    scope: str,
    window_days: int = 7,
    llm_client: Any = None,
    llm_model: str = "openai/gpt-4o-mini",
) -> dict:
    """Run a structured weekly reflection over recent episodes.

    1. SELECT episodes from the time window
    2. Extract recurring themes (noun phrases via regex)
    3. Track goal progress from memories WHERE kind='goal'
    4. Extract identity candidates (heuristic; LLM-augmented if llm_client given)
    5. INSERT reflection_report
    6. For each identity candidate: call identity.propose_identity_fact()

    When llm_client (an OpenAI-compatible async client) is provided, an LLM
    extracts sharper findings and identity candidates; on any failure it
    falls back to the heuristic results that are always computed.
    """
    import re
    import json
    from identity import propose_identity_fact as _propose_idf

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=window_days)
    rpt_id = mint("rpt")

    # 1. SELECT episodes from window
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, content, created_at FROM episodes "
                "WHERE scope = %s AND created_at > now() - make_interval(days => %s) "
                "ORDER BY created_at",
                (scope, window_days),
            )
            episode_rows = await cur.fetchall()

    episodes_scanned = len(episode_rows)
    if episodes_scanned == 0:
        return {"report_id": rpt_id, "findings_count": 0, "identity_candidates_count": 0}

    # 2. Extract recurring themes (proper noun phrases)
    noun_phrase_re = re.compile(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b')
    all_text = " ".join(r[1] for r in episode_rows)
    phrases = noun_phrase_re.findall(all_text)
    phrase_counts: dict[str, int] = {}
    for p in phrases:
        phrase_counts[p] = phrase_counts.get(p, 0) + 1
    recurring_themes = [
        {"phrase": phrase, "count": count}
        for phrase, count in sorted(phrase_counts.items(), key=lambda x: -x[1])
        if count >= 2 and (" " in phrase or phrase.lower() not in _ARTIFACT_WORDS)
    ][:20]

    # 3. Track goal progress: memories with 'goal' in statement
    goal_deltas: list[dict] = []
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, statement, valid_from, created_at FROM memories "
                "WHERE scope = %s AND valid_to IS NULL AND statement ILIKE %s "
                "ORDER BY created_at",
                (scope, "%goal%"),
            )
            goal_rows = await cur.fetchall()

    for gid, gstmt, gvf, gca in goal_rows:
        # Check if any recent episode mentions this goal
        goal_lower = gstmt.lower()
        mentioned = any(goal_lower[:20] in r[1].lower() for r in episode_rows)
        goal_deltas.append({
            "goal_id": gid,
            "statement": gstmt,
            "status": "active" if mentioned else "stale",
            "valid_from": gvf.isoformat() if gvf else None,
        })

    # 4. Extract identity candidates (patterns about the user, confidence >= 0.6)
    identity_candidates: list[dict] = []
    # Use heuristic analysis for identity candidates
    _, _, identity_candidates = _analyze_heuristic(episode_rows, {})
    # Filter to confidence >= 0.6
    identity_candidates = [ic for ic in identity_candidates if ic.get("confidence", 0) >= 0.6]

    # 4b. LLM-augmented extraction (sharper findings + identity candidates).
    # Falls back silently to the heuristic results above on any failure.
    llm_findings: list[dict] = []
    if llm_client is not None:
        try:
            from identity import get_identity_context as _get_ctx
            ident_ctx = await _get_ctx(pool, scope)
            lf, lgd, lic = await _analyze_with_llm(llm_client, llm_model, episode_rows, ident_ctx)
            seen_ic = {(c.get("predicate"), str(c.get("object")).lower()) for c in identity_candidates}
            for c in lic:
                if not (c.get("predicate") and c.get("object")):
                    continue
                key = (c.get("predicate"), str(c.get("object")).lower())
                if key not in seen_ic:
                    identity_candidates.append(c)
                    seen_ic.add(key)
            llm_findings = [f for f in lf if isinstance(f, dict) and f.get("statement")]
            for g in (lgd or []):
                if isinstance(g, dict):
                    goal_deltas.append({
                        "statement": str(g.get("goal", ""))[:120],
                        "status": g.get("status", "noted"),
                        "detail": g.get("detail", ""),
                        "source": "llm",
                    })
        except Exception as e:
            log.warning("LLM reflection analysis failed, using heuristic only: %s", e)

    # Build findings list (LLM findings first — they're the most specific)
    findings: list[dict] = list(llm_findings)
    for theme in recurring_themes[:10]:
        findings.append({
            "statement": f"Recurring theme: '{theme['phrase']}' (mentioned {theme['count']}x)",
            "confidence": min(0.5 + theme["count"] * 0.05, 0.9),
            "tags": ["theme"],
        })
    for gd in goal_deltas:
        if gd.get("source") == "llm":
            findings.append({
                "statement": f"Goal '{gd.get('statement', '')[:60]}' is {gd.get('status')}"
                             + (f" — {gd['detail'][:80]}" if gd.get("detail") else ""),
                "confidence": 0.7,
                "tags": ["goals", "llm"],
            })
        else:
            findings.append({
                "statement": f"Goal '{gd['statement'][:60]}' is {gd['status']}",
                "confidence": 0.7,
                "tags": ["goals"],
            })

    # 5. INSERT reflection_report
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO reflection_reports "
                "(id, scope, report_type, time_window_start, time_window_end, "
                "episodes_scanned, findings, goal_deltas, identity_candidates, summary) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (rpt_id, scope, "weekly", window_start, now,
                 episodes_scanned,
                 json.dumps(findings), json.dumps(goal_deltas),
                 json.dumps(identity_candidates),
                 f"Weekly reflection: {len(findings)} findings, {len(identity_candidates)} identity candidates"),
            )

    # 6. For each identity candidate: call identity.propose_identity_fact()
    for ic in identity_candidates:
        try:
            source_epi = episode_rows[0][0] if episode_rows else None
            if source_epi:
                await _propose_idf(
                    pool, publisher, scope,
                    predicate=ic["predicate"],
                    object=ic["object"],
                    confidence=min(ic.get("confidence", 0.0), 0.7),  # inferences stay hypothesis
                    source_episode_id=source_epi,
                )
        except Exception as e:
            log.warning("Failed to propose identity fact from reflection: %s", e)

    return {
        "report_id": rpt_id,
        "findings_count": len(findings),
        "identity_candidates_count": len(identity_candidates),
        "summary": f"Weekly reflection: {len(findings)} findings, {len(identity_candidates)} identity candidates",
    }


async def run_monthly_reflection(
    pool: Any,
    publisher: Any,
    scope: str,
    llm_client: Any = None,
    llm_model: str = "openai/gpt-4o-mini",
) -> dict:
    """Run a structured monthly reflection over the past 30 days.

    Same as weekly but with window_days=30 and deeper analysis
    including a mental_models review. LLM-augmented when llm_client is given.
    """
    import re
    import json
    from identity import propose_identity_fact as _propose_idf

    window_days = 30
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=window_days)
    rpt_id = mint("rpt")

    # 1. SELECT episodes from 30-day window
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, content, created_at FROM episodes "
                "WHERE scope = %s AND created_at > now() - make_interval(days => %s) "
                "ORDER BY created_at",
                (scope, window_days),
            )
            episode_rows = await cur.fetchall()

    episodes_scanned = len(episode_rows)
    if episodes_scanned == 0:
        return {"report_id": rpt_id, "findings_count": 0, "identity_candidates_count": 0, "summary": "Monthly reflection: no episodes found"}

    # 2. Extract recurring themes (proper noun phrases)
    noun_phrase_re = re.compile(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b')
    all_text = " ".join(r[1] for r in episode_rows)
    phrases = noun_phrase_re.findall(all_text)
    phrase_counts: dict[str, int] = {}
    for p in phrases:
        phrase_counts[p] = phrase_counts.get(p, 0) + 1
    recurring_themes = [
        {"phrase": phrase, "count": count}
        for phrase, count in sorted(phrase_counts.items(), key=lambda x: -x[1])
        if count >= 2 and (" " in phrase or phrase.lower() not in _ARTIFACT_WORDS)
    ][:20]

    # 2b. Deeper analysis: mental_models review
    mental_model_findings: list[dict] = []
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
        mental_model_findings.append({
            "model": mm_name,
            "confidence": mm_conf,
            "recently_referenced": mm_mentioned,
            "last_updated": mm_updated.isoformat() if mm_updated else None,
        })

    # 3. Track goal progress: canonical memories with 'goal' in statement
    goal_deltas: list[dict] = []
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, statement, valid_from, created_at FROM memories "
                "WHERE scope = %s AND valid_to IS NULL AND statement ILIKE %s "
                "ORDER BY created_at",
                (scope, "%goal%"),
            )
            goal_rows = await cur.fetchall()

    for gid, gstmt, gvf, gca in goal_rows:
        goal_lower = gstmt.lower()
        mentioned = any(goal_lower[:20] in r[1].lower() for r in episode_rows)
        goal_deltas.append({
            "goal_id": gid,
            "statement": gstmt,
            "status": "active" if mentioned else "stale",
            "valid_from": gvf.isoformat() if gvf else None,
        })

    # 4. Extract identity candidates (structured: prefers_editor, uses_tool, etc.)
    # Note: raw self-referential text dumps were removed — they polluted the
    # identity store with un-structured episode snippets. Only structured
    # heuristic candidates (predicate=object) are proposed.
    identity_candidates: list[dict] = []
    _, _, identity_candidates = _analyze_heuristic(episode_rows, {})
    identity_candidates = [ic for ic in identity_candidates if ic.get("confidence", 0) >= 0.6]

    # 4b. LLM-augmented extraction (sharper findings + identity candidates).
    llm_findings: list[dict] = []
    if llm_client is not None:
        try:
            from identity import get_identity_context as _get_ctx
            ident_ctx = await _get_ctx(pool, scope)
            lf, lgd, lic = await _analyze_with_llm(llm_client, llm_model, episode_rows, ident_ctx)
            seen_ic = {(c.get("predicate"), str(c.get("object")).lower()) for c in identity_candidates}
            for c in lic:
                if not (c.get("predicate") and c.get("object")):
                    continue
                key = (c.get("predicate"), str(c.get("object")).lower())
                if key not in seen_ic:
                    identity_candidates.append(c)
                    seen_ic.add(key)
            llm_findings = [f for f in lf if isinstance(f, dict) and f.get("statement")]
        except Exception as e:
            log.warning("LLM monthly reflection analysis failed, using heuristic only: %s", e)

    # Build findings list (LLM findings first — they're the most specific)
    findings: list[dict] = list(llm_findings)
    for theme in recurring_themes[:15]:
        findings.append({
            "statement": f"Recurring theme: '{theme['phrase']}' (mentioned {theme['count']}x)",
            "confidence": min(0.5 + theme["count"] * 0.05, 0.9),
            "tags": ["theme"],
        })
    for gd in goal_deltas:
        findings.append({
            "statement": f"Goal '{gd['statement'][:60]}' is {gd['status']}",
            "confidence": 0.7,
            "tags": ["goals"],
        })
    for mmf in mental_model_findings[:10]:
        findings.append({
            "statement": f"Mental model '{mmf['model']}' confidence={mmf['confidence']:.2f}, recently_referenced={mmf['recently_referenced']}",
            "confidence": 0.65,
            "tags": ["mental_models"],
        })

    # 5. INSERT reflection_report
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO reflection_reports "
                "(id, scope, report_type, time_window_start, time_window_end, "
                "episodes_scanned, findings, goal_deltas, identity_candidates, summary) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (rpt_id, scope, "monthly", window_start, now,
                 episodes_scanned,
                 json.dumps(findings), json.dumps(goal_deltas),
                 json.dumps(identity_candidates),
                 f"Monthly reflection: {len(findings)} findings, {len(identity_candidates)} identity candidates (30-day window)"),
            )

    # 6. For each identity candidate: call identity.propose_identity_fact()
    for ic in identity_candidates:
        try:
            source_epi = episode_rows[0][0] if episode_rows else None
            if source_epi:
                await _propose_idf(
                    pool, publisher, scope,
                    predicate=ic["predicate"],
                    object=ic["object"],
                    confidence=min(ic.get("confidence", 0.0), 0.7),  # inferences stay hypothesis
                    source_episode_id=source_epi,
                )
        except Exception as e:
            log.warning("Failed to propose identity fact from monthly reflection: %s", e)

    return {
        "report_id": rpt_id,
        "findings_count": len(findings),
        "identity_candidates_count": len(identity_candidates),
        "summary": f"Monthly reflection: {len(findings)} findings, {len(identity_candidates)} identity candidates",
    }


async def get_reflection_reports(
    pool: Any,
    scope: str,
    limit: int = 10,
) -> list[dict]:
    """SELECT reflection reports ordered by most recent."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, scope, report_type, time_window_start, time_window_end, "
                "episodes_scanned, findings, goal_deltas, identity_candidates, summary, created_at "
                "FROM reflection_reports "
                "WHERE scope = %s "
                "ORDER BY created_at DESC LIMIT %s",
                (scope, limit),
            )
            rows = await cur.fetchall()

    return [
        {
            "id": r[0],
            "scope": r[1],
            "report_type": r[2],
            "time_window_start": r[3].isoformat() if r[3] else None,
            "time_window_end": r[4].isoformat() if r[4] else None,
            "episodes_scanned": r[5],
            "findings": r[6],
            "goal_deltas": r[7],
            "identity_candidates": r[8],
            "summary": r[9],
            "created_at": r[10].isoformat() if r[10] else None,
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
                "FROM reflection_reports "
                "WHERE id = %s",
                (report_id,),
            )
            row = await cur.fetchone()

    if row is None:
        return None

    return {
        "id": row[0],
        "scope": row[1],
        "report_type": row[2],
        "time_window_start": row[3].isoformat() if row[3] else None,
        "time_window_end": row[4].isoformat() if row[4] else None,
        "episodes_scanned": row[5],
        "findings": row[6],
        "goal_deltas": row[7],
        "identity_candidates": row[8],
        "summary": row[9],
        "created_at": row[10].isoformat() if row[10] else None,
    }
