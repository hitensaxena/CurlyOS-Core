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
    """Detect value-action gaps: things Hiten values but isn't spending time on.

    Queries identity_facts for values/goals/preferences, then checks recent
    episode activity for keyword matches. Gaps become alignment_signals
    at hypothesis status.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            # Get stated values, goals, preferences, priorities from identity_facts
            await cur.execute(
                "SELECT id, predicate, object "
                "FROM identity_facts "
                "WHERE scope = %s AND valid_to IS NULL "
                "AND predicate IN (%s, %s, %s, %s)",
                (scope, "values", "goal", "prefers", "priority"),
            )
            identity_rows = await cur.fetchall()

            # Get recent episodes (last 30 days)
            await cur.execute(
                "SELECT id, content FROM episodes "
                "WHERE scope = %s AND ingested_at >= now() - interval '%s days' "
                "ORDER BY ingested_at DESC LIMIT 200",
                (scope, 30),
            )
            episode_rows = await cur.fetchall()

    if not identity_rows or not episode_rows:
        return []

    # Idempotency: supersede prior auto-generated (hypothesis) signals so cron
    # re-runs produce a fresh snapshot instead of unbounded duplicates.
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE alignment_signals SET valid_to = now() "
                "WHERE scope = %s AND valid_to IS NULL AND epistemic_status = 'hypothesis'",
                (scope,),
            )

    # Build recent activity text for keyword matching
    recent_text = " ".join(r[1].lower() for r in episode_rows if r[1])
    # Also build a set of individual words for matching
    recent_words = set(re.findall(r'\b[a-z]{3,}\b', recent_text))

    gaps = []
    for fact_id, predicate, obj in identity_rows:
        if not obj:
            continue

        # Extract keywords from the identity fact object
        obj_lower = str(obj).lower()
        obj_words = set(re.findall(r'\b[a-z]{3,}\b', obj_lower))

        # Check if any word from the value/goal appears in recent activity
        is_active = bool(obj_words & recent_words) or (obj_lower in recent_text)

        if not is_active:
            gap_id = mint("aln")
            description = (
                f"Stated '{predicate}' = '{obj}' but no related activity "
                f"in recent episodes"
            )
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "INSERT INTO alignment_signals "
                        "(id, scope, signal_type, description, severity, epistemic_status) "
                        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                        (gap_id, scope, "value_action_gap",
                         description, 0.6, "hypothesis"),
                    )
                    (aid,) = await cur.fetchone()
            gaps.append({
                "id": aid,
                "topic": str(obj),
                "type": "value_action_gap",
                "description": description,
                "identity_fact_id": fact_id,
            })

    return gaps


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
