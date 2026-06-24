"""Attention engine — model attention as the scarcest personal resource.

Tables: alignment_signals (aln_), materialized views (attention_allocation_weekly, focus_heatmap_grid)

Key APIs:
  GET /attention/allocation?window=7d
  GET /attention/heatmap?window=30d
  GET /attention/alignment-signals?status=hypothesis
  GET /attention/cognitive-load?window=14d
  GET /attention/neglected?min_goal_priority=high

Hard dep on Activity Telemetry (38) for raw sensing data.
Without telemetry, derives attention signals from episode content analysis.

See: ~/hitenos-architecture/37-attention-engine.md
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, timezone, timedelta
from typing import Any

from shared.types.ulid import mint

log = logging.getLogger("curlyos.attention")


# ── DDL ─────────────────────────────────────────────────────────────────────

ATTENTION_DDL = """
CREATE TABLE IF NOT EXISTS alignment_signals (
  id                text        PRIMARY KEY,
  scope             text        NOT NULL,
  signal_type       text        NOT NULL,  -- value_action_gap | fulfillment | regret
  description       text        NOT NULL,
  severity          real        NOT NULL DEFAULT 0.5,  -- 0.0-1.0
  epistemic_status  text        NOT NULL DEFAULT 'hypothesis',
  valid_from        timestamptz NOT NULL DEFAULT now(),
  valid_to          timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_aln_scope ON alignment_signals (scope, signal_type) WHERE valid_to IS NULL;
"""


async def get_allocation(pool: Any, scope: str, window_days: int = 7) -> dict:
    """Return attention allocation breakdown by category for the time window.

    Derives categories from episode content analysis (since we don't have
    activity telemetry yet). Categories: work, creative, health, social, learning, admin.
    Returns {categories: {category: {count, percentage, trend}}, total_episodes, window_days}.
    """
    from datetime import timedelta as _td
    cutoff = datetime.now(timezone.utc) - _td(days=window_days)
    half_cutoff = datetime.now(timezone.utc) - _td(days=window_days // 2)

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, content, ingested_at FROM episodes "
                "WHERE scope = %s AND ingested_at >= %s "
                "ORDER BY ingested_at ASC LIMIT 200",
                (scope, cutoff),
            )
            episode_rows = await cur.fetchall()

    # Split into first half and second half of window for trend detection
    first_half: list = []
    second_half: list = []
    for row in episode_rows:
        ingested_at = row[2]
        if ingested_at < half_cutoff:
            first_half.append(row)
        else:
            second_half.append(row)

    category_keywords: dict[str, list[str]] = {
        "work": ["code", "build", "fix", "deploy", "debug", "develop", "program", "test", "release"],
        "creative": ["design", "art", "music", "write", "draw", "paint", "compose", "sketch"],
        "health": ["exercise", "sleep", "meditate", "workout", "run", "walk", "yoga", "stretch"],
        "social": ["meet", "call", "chat", "party", "dinner", "lunch", "hangout", "visit"],
        "learning": ["read", "study", "learn", "course", "tutorial", "lecture", "book", "research"],
        "admin": ["email", "schedule", "plan", "organize", "budget", "invoice", "calendar", "task"],
    }

    def count_categories(rows) -> Counter[str]:
        cats: Counter[str] = Counter()
        for _, content, _ in rows:
            text = content.lower()
            for cat, keywords in category_keywords.items():
                if any(kw in text for kw in keywords):
                    cats[cat] += 1
        return cats

    first_counts = count_categories(first_half)
    second_counts = count_categories(second_half)

    # All categories that appeared in either half
    all_cats = set(first_counts.keys()) | set(second_counts.keys())
    total_all = sum(second_counts.values()) or 1

    categories: dict[str, dict] = {}
    for cat in all_cats:
        first_c = first_counts.get(cat, 0)
        second_c = second_counts.get(cat, 0)
        if first_c + second_c == 0:
            trend = "stable"
        elif second_c > first_c:
            trend = "increasing"
        elif second_c < first_c:
            trend = "decreasing"
        else:
            trend = "stable"
        categories[cat] = {
            "count": first_c + second_c,
            "percentage": round((first_c + second_c) / max(len(episode_rows), 1) * 100, 1),
            "trend": trend,
        }

    return {
        "categories": categories,
        "total_episodes": len(episode_rows),
        "window_days": window_days,
    }


async def get_heatmap(pool: Any, scope: str, window_days: int = 30) -> list[list[float]]:
    """Return hour×day focus heatmap grid (24 hours × 7 days).

    Without activity telemetry, returns a uniform grid.
    With telemetry, this would aggregate focus_logs into the grid.
    """
    # TODO: Implement with activity telemetry data
    return [[0.0] * 7 for _ in range(24)]


async def detect_alignment_gaps(
    pool: Any,
    publisher: Any,
    scope: str,
) -> list[dict]:
    """Value/goal–action gaps grounded in the knowledge graph + genuine activity.

    A stated goal or value is "aligned" if it shows up in the knowledge graph
    (entity names) OR in recent genuine captures (journal/voice/hermes — not the
    bulk import). Goals/values with NO such presence become alignment_signals.
    """
    # Distinctive terms only — generic fillers/verbs recur in any activity text and
    # would mask real under-attention; only specific concept words should drive the match.
    STOP = {"with", "that", "this", "make", "have", "your", "from", "into",
            "need", "want", "goal", "goals", "case", "more", "most", "very", "much",
            "many", "some", "they", "them", "will", "what", "when", "here", "there",
            "find", "create", "build", "work", "start", "keep", "take", "plan",
            "doing", "using", "based", "thing", "things", "stuff", "good", "five",
            "only", "also", "just", "like", "well", "about", "over", "such", "than",
            "then", "been", "were", "would", "could", "should", "next"}

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, title FROM goals WHERE scope = %s AND valid_to IS NULL "
                "AND status = 'active'", (scope,))
            goals = await cur.fetchall()
            await cur.execute(
                "SELECT id, predicate, object FROM identity_facts "
                "WHERE scope = %s AND valid_to IS NULL "
                "AND (predicate ILIKE '%%value%%' OR predicate IN "
                "('builds', 'focus', 'priority', 'primary_project', 'cares_about', 'exercise'))",
                (scope,))
            values = await cur.fetchall()
            # Alignment is about BEHAVIOUR: match goals/values against recent
            # genuine activity (captures, excluding the bulk import), NOT the full
            # historical graph — a goal can sit in the graph yet get no real
            # attention. Absence here = "stated but not being acted on".
            await cur.execute(
                "SELECT content FROM episodes WHERE scope = %s "
                "AND coalesce(source_ref,'') NOT ILIKE 'brain:%%' "
                "AND coalesce(source_ref,'') NOT ILIKE 'mind:%%' "
                "ORDER BY ingested_at DESC LIMIT 200", (scope,))
            activity = " ".join((r[0] or "").lower() for r in await cur.fetchall())

    haystack = activity

    # Idempotency: supersede prior hypothesis signals → fresh snapshot.
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE alignment_signals SET valid_to = now() "
                "WHERE scope = %s AND valid_to IS NULL AND epistemic_status = 'hypothesis'",
                (scope,))

    def _kw(s: str) -> set[str]:
        return {w for w in re.findall(r'\b[a-z]{4,}\b', (s or "").lower())} - STOP

    # A goal/value is "getting attention" if its distinctive terms recur in recent
    # activity. Low recurrence = stated priority barely being acted on (a soft gap),
    # which is far more useful than binary presence on a broad conversational corpus.
    LOW_ATTENTION = 8
    targets = ([("goal", g[0], g[1], 0.7) for g in goals]
               + [("value", v[0], str(v[2]), 0.5) for v in values])
    gaps = []
    for kind, ref_id, label, base_sev in targets:
        kws = _kw(label)
        if not kws:
            continue
        mentions = max(
            (len(re.findall(r'\b' + re.escape(k) + r'\b', haystack)) for k in kws),
            default=0)
        if mentions >= LOW_ATTENTION:
            continue  # actively getting attention → aligned
        severity = round(min(base_sev + (LOW_ATTENTION - mentions) * 0.1, 0.95), 2)
        gap_id = mint("aln")
        plural = "s" if mentions != 1 else ""
        desc = (f"{kind.title()} '{label}' gets little recent attention "
                f"({mentions} mention{plural}) — stated but barely being acted on")
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO alignment_signals "
                    "(id, scope, signal_type, description, severity, epistemic_status) "
                    "VALUES (%s, %s, %s, %s, %s, 'hypothesis') RETURNING id",
                    (gap_id, scope, f"{kind}_action_gap", desc, severity))
                (aid,) = await cur.fetchone()
        gaps.append({"id": aid, "topic": label, "type": f"{kind}_action_gap",
                     "description": desc, "severity": severity, "mentions": mentions,
                     "ref_id": ref_id})
    return gaps


async def get_focus_areas(pool: Any, scope: str, limit: int = 12) -> list[dict]:
    """Where cognitive mass sits: the most-connected knowledge-graph entities
    (excluding the central person), with type. This is honest attention signal —
    derived from graph structure, not fake activity telemetry."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "WITH deg AS (SELECT eid, count(*) d FROM ("
                "  SELECT src_entity_id eid FROM knowledge_edges WHERE valid_to IS NULL "
                "  UNION ALL SELECT dst_entity_id FROM knowledge_edges WHERE valid_to IS NULL"
                ") x GROUP BY eid) "
                "SELECT e.name, e.label, COALESCE(d.d, 0) FROM knowledge_entities e "
                "LEFT JOIN deg d ON d.eid = e.id "
                "WHERE e.scope = %s AND e.valid_to IS NULL AND lower(e.name) <> 'hiten' "
                "ORDER BY COALESCE(d.d, 0) DESC, e.created_at ASC LIMIT %s",
                (scope, limit))
            return [{"name": n, "label": lab, "weight": d} for n, lab, d in await cur.fetchall()]


async def get_neglected_entities(pool: Any, scope: str, min_degree: int = 5,
                                 limit: int = 10) -> list[dict]:
    """High-degree entities (well-established in the graph) absent from recent
    genuine captures — relationships/projects/topics drifting out of attention."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT content FROM episodes WHERE scope = %s "
                "AND coalesce(source_ref,'') NOT ILIKE 'brain:%%' "
                "AND coalesce(source_ref,'') NOT ILIKE 'mind:%%' "
                "ORDER BY ingested_at DESC LIMIT 200", (scope,))
            activity = " ".join((r[0] or "").lower() for r in await cur.fetchall())
            await cur.execute(
                "WITH deg AS (SELECT eid, count(*) d FROM ("
                "  SELECT src_entity_id eid FROM knowledge_edges WHERE valid_to IS NULL "
                "  UNION ALL SELECT dst_entity_id FROM knowledge_edges WHERE valid_to IS NULL"
                ") x GROUP BY eid) "
                "SELECT e.name, e.label, COALESCE(d.d, 0) FROM knowledge_entities e "
                "LEFT JOIN deg d ON d.eid = e.id "
                "WHERE e.scope = %s AND e.valid_to IS NULL AND lower(e.name) <> 'hiten' "
                "AND COALESCE(d.d, 0) >= %s ORDER BY COALESCE(d.d, 0) DESC LIMIT 40",
                (scope, min_degree))
            ents = await cur.fetchall()
    out = [{"name": n, "label": lab, "weight": d}
           for n, lab, d in ents if n.lower() not in activity]
    return out[:limit]


async def cognitive_breadth(pool: Any, scope: str) -> dict:
    """KG-based breadth: how many distinct entity types are active + the spread
    (vs concentration in a few). An honest 'how scattered is cognition' metric."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT label, count(*) FROM knowledge_entities "
                "WHERE scope = %s AND valid_to IS NULL GROUP BY label ORDER BY 2 DESC",
                (scope,))
            by_label = await cur.fetchall()
    total = sum(c for _, c in by_label) or 1
    top = by_label[0][1] if by_label else 0
    return {
        "total_entities": total,
        "distinct_types": len(by_label),
        "by_type": {lab: c for lab, c in by_label},
        "concentration": round(top / total, 2),
    }


async def get_neglected_opportunities(
    pool: Any,
    scope: str,
    min_priority: str = "high",
) -> list[dict]:
    """Return high-priority goals with low attention allocation (attention gap)."""
    # Cross-reference identity goals with recent episode activity
    from identity import get_identity_context

    identity = await get_identity_context(pool, scope)
    goals = {k: v for k, v in identity.items() if "goal" in k or "project" in k}

    # Check which goals have recent activity
    from memory.governance import list_episodes
    episodes = await list_episodes(pool, scope, limit=50)
    recent_text = " ".join(e.get("content", "").lower() for e in episodes)

    neglected = []
    for pred, info in goals.items():
        obj = info.get("object", "").lower()
        if obj and obj not in recent_text:
            neglected.append({
                "predicate": pred,
                "object": info.get("object"),
                "confidence": info.get("confidence"),
                "last_active": "unknown",
            })

    return neglected


async def estimate_cognitive_load(
    pool: Any,
    scope: str,
    window_days: int = 14,
) -> dict:
    """Estimate cognitive load from episode density and topic switching.

    Analyzes episode frequency (density) and topic diversity (switching)
    to produce a 0-1 load score.

    Returns {score: 0-1, breakdown: {density, topic_switching, episode_count}}.
    """
    from datetime import timedelta as _td
    import re
    from collections import Counter

    cutoff = datetime.now(timezone.utc) - _td(days=window_days)

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, content, ingested_at FROM episodes "
                "WHERE scope = %s AND ingested_at >= %s "
                "ORDER BY ingested_at ASC LIMIT 200",
                (scope, cutoff),
            )
            episode_rows = await cur.fetchall()

    episode_count = len(episode_rows)
    if episode_count == 0:
        return {"score": 0.0, "breakdown": {"density": 0.0, "topic_switching": 0.0, "episode_count": 0}}

    # Density: episodes per day (saturation at 10/day = score 1.0)
    days = max(window_days, 1)
    density_score = min(episode_count / (days * 10), 1.0)

    # Topic switching: count distinct topics across episodes
    stop_words = {"the", "a", "an", "is", "are", "was", "were", "be", "been",
                  "have", "has", "had", "do", "does", "did", "will", "would",
                  "could", "should", "to", "of", "in", "for", "on", "with",
                  "at", "by", "from", "as", "and", "but", "or", "not", "it",
                  "its", "this", "that", "i", "me", "my", "we", "our", "you",
                  "your", "he", "she", "they", "them", "his", "her", "their"}

    def top_words(content: str, n: int = 5) -> set[str]:
        ws = re.findall(r'\b[a-z]{3,}\b', content.lower())
        counts = Counter(w for w in ws if w not in stop_words)
        return {w for w, _ in counts.most_common(n)}

    if episode_count >= 2:
        switches = 0
        prev = top_words(episode_rows[0][1])
        for i in range(1, episode_count):
            curr = top_words(episode_rows[i][1])
            union = prev | curr
            intersection = prev & curr
            similarity = len(intersection) / len(union) if union else 1.0
            if similarity < 0.3:
                switches += 1
            prev = curr
        switching_score = min(switches / max(episode_count - 1, 1), 1.0)
    else:
        switching_score = 0.0

    # Combined score: weighted average
    score = round(density_score * 0.4 + switching_score * 0.6, 3)

    return {
        "score": score,
        "breakdown": {
            "density": round(density_score, 3),
            "topic_switching": round(switching_score, 3),
            "episode_count": episode_count,
            "window_days": window_days,
        },
    }


# ── Mood log DDL ──────────────────────────────────────────────────────────────

MOOD_DDL = """
CREATE TABLE IF NOT EXISTS mood_log (
  id                text        PRIMARY KEY,
  scope             text        NOT NULL,
  mood              text        NOT NULL,
  valence           real        NOT NULL DEFAULT 0.0,  -- -1.0 to 1.0
  energy            real        NOT NULL DEFAULT 0.5,  -- 0.0 to 1.0
  context           text,
  source            text        NOT NULL DEFAULT 'inference',  -- explicit | inference
  source_episode_id text,
  logged_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_mood_scope_time ON mood_log (scope, logged_at DESC);
"""


# ── Mood CRUD ─────────────────────────────────────────────────────────────────

async def log_mood(
    pool: Any,
    scope: str,
    mood: str,
    valence: float,
    energy: float,
    context: str | None = None,
    source: str = "explicit",
    source_episode_id: str | None = None,
) -> dict:
    """Record an explicit mood entry."""
    entry_id = mint("moo")
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO mood_log (id, scope, mood, valence, energy, context, source, source_episode_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id, logged_at",
                (entry_id, scope, mood, valence, energy, context, source, source_episode_id),
            )
            row = await cur.fetchone()
    return {"id": row[0], "mood": mood, "valence": valence, "energy": energy, "logged_at": row[1].isoformat() if row[1] else None}


async def get_mood_history(
    pool: Any,
    scope: str,
    days: int = 30,
    limit: int = 100,
) -> dict:
    """Return mood timeline with rolling averages.

    Returns {entries: [...], summary: {avg_valence, avg_energy, dominant_mood, trend}}.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, mood, valence, energy, context, source, logged_at "
                "FROM mood_log WHERE scope = %s AND logged_at >= %s "
                "ORDER BY logged_at DESC LIMIT %s",
                (scope, cutoff, limit),
            )
            rows = await cur.fetchall()

    if not rows:
        return {"entries": [], "summary": {"avg_valence": 0.0, "avg_energy": 0.0, "dominant_mood": None, "trend": "stable"}}

    entries = []
    valence_sum = 0.0
    energy_sum = 0.0
    mood_counts: Counter[str] = Counter()
    for r in rows:
        mood_val = str(r[1]) if r[1] else "neutral"
        valence_sum += float(r[2] or 0.0)
        energy_sum += float(r[3] or 0.5)
        mood_counts[mood_val] += 1
        entries.append({
            "id": r[0], "mood": mood_val, "valence": float(r[2] or 0.0),
            "energy": float(r[3] or 0.5), "context": r[4], "source": r[5],
            "logged_at": r[6].isoformat() if r[6] else None,
        })

    n = len(entries)
    avg_valence = round(valence_sum / n, 3)
    avg_energy = round(energy_sum / n, 3)
    dominant = mood_counts.most_common(1)[0][0] if mood_counts else None

    # Simple trend: compare first half vs second half of the window
    mid = n // 2
    recent_avg = valence_sum / n
    older_avg = valence_sum / n  # simplified - more sophisticated split across time
    if mid > 0:
        recent = sum(e["valence"] for e in entries[:mid]) / mid
        older = sum(e["valence"] for e in entries[mid:]) / max(n - mid, 1)
        trend = "improving" if recent > older + 0.1 else "declining" if recent < older - 0.1 else "stable"
    else:
        trend = "stable"

    return {
        "entries": entries,
        "summary": {
            "avg_valence": avg_valence,
            "avg_energy": avg_energy,
            "dominant_mood": dominant,
            "trend": trend,
            "total_entries": n,
        },
    }


async def extract_mood_from_episode(
    pool: Any,
    scope: str,
    llm_client: Any = None,
) -> dict:
    """Analyze the most recent episode for mood/energy signals.

    With LLM: uses the model to infer mood from latest episode content.
    Without LLM: heuristic keyword-based valence detection.
    Stores result in mood_log at source='inference'.
    Returns the mood entry dict or None if no recent episodes.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, content, ingested_at FROM episodes "
                "WHERE scope = %s ORDER BY ingested_at DESC LIMIT 1",
                (scope,),
            )
            row = await cur.fetchone()

    if row is None:
        return {"mood": None, "valence": 0.0, "energy": 0.5}

    epi_id, content, ingested_at = row

    if llm_client is not None:
        try:
            prompt = (
                "From the following journal/conversation entry, infer the person's mood "
                "and energy level. Return JSON: {\"mood\": \"...\", \"valence\": 0.0-1.0, "
                "\"energy\": 0.0-1.0}\n\nEntry:\n" + (content or "")[:1000]
            )
            response = await llm_client.chat.completions.create(
                model="openai/gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.2,
            )
            from shared.llm import first_json
            data = first_json(response.choices[0].message.content, default={})
            mood = str(data.get("mood", "neutral"))[:30]
            valence = max(-1.0, min(1.0, float(data.get("valence", 0.0))))
            energy = max(0.0, min(1.0, float(data.get("energy", 0.5))))
        except Exception as e:
            log.warning("LLM mood extraction failed: %s", e)
            mood, valence, energy = _heuristic_mood(content)
    else:
        mood, valence, energy = _heuristic_mood(content or "")

    result = await log_mood(pool, scope, mood, valence, energy,
                            source="inference", source_episode_id=epi_id)
    return result


def _heuristic_mood(text: str) -> tuple[str, float, float]:
    """Keyword-based mood/energy heuristic — no LLM needed."""
    text_lower = text.lower()

    # Positives and negatives
    positive_words = ["good", "great", "awesome", "excellent", "happy", "love", "wonderful",
                      "productive", "accomplished", "proud", "excited", "amazing", "nice",
                      "well", "better", "best", "progress", "solved", "fixed", "done"]
    negative_words = ["bad", "terrible", "awful", "sad", "angry", "frustrated", "stuck",
                      "tired", "exhausted", "stressed", "anxious", "worried", "upset",
                      "hate", "fail", "failed", "broken", "wrong", "hard", "difficult"]

    # Energy words
    high_energy = ["energetic", "active", "focused", "productive", "busy", "running",
                   "fast", "quick", "motivated", "excited", "intense"]
    low_energy = ["tired", "exhausted", "sleepy", "lazy", "slow", "drained",
                  "fatigued", "rested", "nap", "rest", "break", "relax"]

    pos_count = sum(1 for w in positive_words if w in text_lower)
    neg_count = sum(1 for w in negative_words if w in text_lower)
    high_count = sum(1 for w in high_energy if w in text_lower)
    low_count = sum(1 for w in low_energy if w in text_lower)

    # Valence: -1 to 1
    total = pos_count + neg_count
    valence = round((pos_count - neg_count) / max(total, 1), 2) if total > 0 else 0.0
    valence = max(-1.0, min(1.0, valence))

    # Energy: 0 to 1
    energy_total = high_count + low_count
    energy = round(0.5 + (high_count - low_count) / max(energy_total, 1) * 0.4, 2) if energy_total > 0 else 0.5
    energy = max(0.0, min(1.0, energy))

    # Mood label
    if valence >= 0.3 and energy >= 0.6:
        mood = "energetic"
    elif valence >= 0.3 and energy < 0.4:
        mood = "calm"
    elif valence <= -0.3 and energy >= 0.6:
        mood = "anxious"
    elif valence <= -0.3 and energy < 0.4:
        mood = "tired"
    elif valence <= -0.3:
        mood = "sad"
    elif energy >= 0.7:
        mood = "focused"
    elif energy < 0.3:
        mood = "sleepy"
    else:
        mood = "neutral"

    return mood, valence, energy


# ── Health signals ────────────────────────────────────────────────────────────

_HEALTH_KEYWORDS = {
    "sleep": ["sleep", "slept", "insomnia", "night", "bed", "rested", "nap", "tired"],
    "exercise": ["exercise", "workout", "run", "walk", "gym", "yoga", "stretch", "fitness", "sport"],
    "sick": ["sick", "ill", "fever", "cold", "flu", "headache", "pain", "doctor", "hospital", "medicine"],
    "food": ["eat", "ate", "food", "meal", "diet", "hungry", "lunch", "dinner", "breakfast", "cook"],
    "mental_health": ["meditate", "meditation", "therapy", "anxiety", "stress", "calm", "mindful", "breath"],
}

_MOOD_CONDITIONS = {
    "anxious": "high_anxiety", "sad": "low_mood", "tired": "low_energy",
    "energetic": "high_energy", "calm": "balanced",
}


async def health_signals(
    pool: Any,
    scope: str,
    days: int = 14,
) -> dict:
    """Derive health indicators from recent episode content.

    Returns a dict with keyword-based counts for sleep, exercise, sickness,
    food/nutrition, and mental health mentions, plus recent mood if available.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT content FROM episodes WHERE scope = %s AND ingested_at >= %s "
                "ORDER BY ingested_at DESC LIMIT 200",
                (scope, cutoff),
            )
            rows = await cur.fetchall()

    signals: dict[str, dict] = {}
    for category, keywords in _HEALTH_KEYWORDS.items():
        count = sum(1 for (content,) in rows if any(kw in (content or "").lower() for kw in keywords))
        signals[category] = {"mentions": count, "active": count > 0}

    # Add recent mood summary
    try:
        mood_data = await get_mood_history(pool, scope, days=days, limit=50)
        signals["mood_summary"] = mood_data.get("summary", {})
    except Exception:
        signals["mood_summary"] = {"avg_valence": 0.0, "avg_energy": 0.5, "dominant_mood": None}

    signals["window_days"] = days
    signals["total_episodes_scanned"] = len(rows)
    return signals
