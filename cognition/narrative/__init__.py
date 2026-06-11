"""Narrative engine — life chapters, themes, and turning points from bi-temporal events.

Tables: life_chapters (cha_), themes (thm_), theme_chapter_links

Key APIs:
  GET /narrative/chapters
  GET /narrative/themes
  POST /narrative/compose {query, since?, domain?}

The Narrator agent may run as a lens within the Reflection engine.

See: ~/hitenos-architecture/36-narrative-engine.md
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from shared.types.ulid import mint
from shared.llm import first_json

log = logging.getLogger("curlyos.narrative")


# ── DDL ─────────────────────────────────────────────────────────────────────

NARRATIVE_DDL = """
CREATE TABLE IF NOT EXISTS life_chapters (
  id                text        PRIMARY KEY,
  scope             text        NOT NULL,
  title             text        NOT NULL,
  summary           text,
  start_date        timestamptz NOT NULL,
  end_date          timestamptz,
  epistemic_status  text        NOT NULL DEFAULT 'hypothesis',
  valid_from        timestamptz NOT NULL DEFAULT now(),
  valid_to          timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS themes (
  id                text        PRIMARY KEY,
  scope             text        NOT NULL,
  name              text        NOT NULL,
  description       text,
  frequency         integer     NOT NULL DEFAULT 1,
  epistemic_status  text        NOT NULL DEFAULT 'hypothesis',
  valid_from        timestamptz NOT NULL DEFAULT now(),
  valid_to          timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS theme_chapter_links (
  theme_id          text        NOT NULL REFERENCES themes(id),
  chapter_id        text        NOT NULL REFERENCES life_chapters(id),
  PRIMARY KEY (theme_id, chapter_id)
);

CREATE INDEX IF NOT EXISTS idx_chapters_scope ON life_chapters (scope);
CREATE INDEX IF NOT EXISTS idx_themes_scope ON themes (scope);
"""


async def detect_chapters(pool: Any, scope: str) -> list[dict]:
    """Detect life chapters from episode clusters.

    Groups episodes by time proximity and topic similarity to identify
    distinct "chapters" of activity.
    """
    from memory.governance import list_episodes

    episodes = await list_episodes(pool, scope, limit=200)
    if len(episodes) < 3:
        return []

    # Simple chapter detection: group episodes within 3-day windows
    chapters = []
    current_chapter = None

    for epi in episodes:
        epi_time = epi.get("ingested_at")
        if isinstance(epi_time, str):
            try:
                epi_time = datetime.fromisoformat(epi_time.replace("Z", "+00:00"))
            except Exception:
                continue

        if current_chapter is None or (epi_time and current_chapter["end"] and
                                        (current_chapter["end"] - epi_time).days > 3):
            # Start new chapter
            cha_id = mint("cha")
            current_chapter = {
                "id": cha_id,
                "title": f"Chapter: {epi.get('content', '')[:40]}...",
                "episodes": [epi["id"]],
                "start": epi_time,
                "end": epi_time,
            }
            chapters.append(current_chapter)
        else:
            current_chapter["episodes"].append(epi["id"])
            if epi_time:
                current_chapter["end"] = epi_time

    return [{"id": c["id"], "title": c["title"], "episodes": len(c["episodes"])} for c in chapters]


async def extract_themes(pool: Any, scope: str) -> list[dict]:
    """Extract recurring themes from episode content.

    Uses keyword frequency analysis to identify topics that appear
    across multiple episodes.
    """
    from memory.governance import list_episodes

    episodes = await list_episodes(pool, scope, limit=200)
    if not episodes:
        return []

    # Extract meaningful keywords (nouns/proper nouns)
    stop_words = {"the", "a", "an", "is", "are", "was", "were", "be", "been",
                  "being", "have", "has", "had", "do", "does", "did", "will",
                  "would", "could", "should", "may", "might", "shall", "can",
                  "need", "dare", "ought", "used", "to", "of", "in", "for",
                  "on", "with", "at", "by", "from", "as", "into", "through",
                  "during", "before", "after", "above", "below", "between",
                  "out", "off", "over", "under", "again", "further", "then",
                  "once", "here", "there", "when", "where", "why", "how",
                  "all", "each", "every", "both", "few", "more", "most",
                  "other", "some", "such", "no", "nor", "not", "only", "own",
                  "same", "so", "than", "too", "very", "just", "because",
                  "but", "and", "or", "if", "while", "about", "up", "down",
                  "it", "its", "he", "she", "they", "them", "his", "her",
                  "their", "this", "that", "these", "those", "i", "me", "my",
                  "we", "our", "you", "your", "hiten", "s", "t", "don", "didn",
                  "doesn", "wasn", "weren", "won", "wouldn", "couldn", "shouldn"}

    word_freq = Counter()
    for epi in episodes:
        content = epi.get("content", "").lower()
        words = re.findall(r'\b[a-z]{3,}\b', content)
        for w in words:
            if w not in stop_words:
                word_freq[w] += 1

    # Themes = words appearing 3+ times
    themes = []
    for word, count in word_freq.most_common(10):
        if count >= 2:
            thm_id = mint("thm")
            themes.append({
                "id": thm_id,
                "name": word.title(),
                "description": f"Appears {count} times across {len(episodes)} episodes",
                "frequency": count,
            })

    return themes


async def compose_narrative(
    pool: Any,
    scope: str,
) -> dict:
    """Compose a narrative from themes and chapters.

    Returns {themes: [...], chapters: [...], summary: "Narrative summary text"}.
    """
    themes = await get_themes(pool, scope)
    chapters = await get_chapters(pool, scope)

    # Build a narrative summary from themes and chapters
    theme_names = [t["name"] for t in themes[:5]]
    chapter_titles = [c["title"] for c in chapters[:5]]

    parts = []
    if theme_names:
        parts.append(f"Key themes: {', '.join(theme_names)}.")
    if chapter_titles:
        parts.append(f"Recent chapters: {', '.join(chapter_titles)}.")
    parts.append(
        f"This narrative spans {len(chapters)} chapters across {len(themes)} identified themes."
    )
    summary = " ".join(parts) if parts else "No narrative data available yet."

    return {
        "themes": themes,
        "chapters": chapters,
        "summary": summary,
    }


async def get_chapters(pool: Any, scope: str) -> list[dict]:
    """Return life chapters (cha_) ordered chronologically."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, title, summary, start_date, end_date, epistemic_status "
                "FROM life_chapters WHERE scope = %s AND valid_to IS NULL ORDER BY start_date DESC",
                (scope,),
            )
            rows = await cur.fetchall()
    return [
        {"id": r[0], "title": r[1], "summary": r[2],
         "start": r[3].isoformat() if r[3] else None,
         "end": r[4].isoformat() if r[4] else None,
         "status": r[5]}
        for r in rows
    ]


async def get_themes(pool: Any, scope: str) -> list[dict]:
    """Return recurring themes (thm_) with frequency."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, name, description, frequency, epistemic_status "
                "FROM themes WHERE scope = %s AND valid_to IS NULL ORDER BY frequency DESC",
                (scope,),
            )
            rows = await cur.fetchall()
    return [
        {"id": r[0], "name": r[1], "description": r[2], "frequency": r[3], "status": r[4]}
        for r in rows
    ]


# ── Surface themes (task API) ───────────────────────────────────────────────

async def surface_themes(
    pool: Any,
    publisher: Any,
    scope: str,
    min_frequency: int = 2,
) -> list[dict]:
    """Extract recurring themes from episodes and INSERT into themes table.

    Uses noun phrase frequency analysis. Clusters similar phrases.
    All themes inserted at epistemic_status='hypothesis'.
    """
    import re
    from collections import Counter

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, content, ingested_at FROM episodes "
                "WHERE scope = %s "
                "ORDER BY ingested_at DESC LIMIT 200",
                (scope,),
            )
            episode_rows = await cur.fetchall()

    if not episode_rows:
        return []

    # Idempotency: supersede prior auto-generated (hypothesis) themes so cron
    # re-runs produce a fresh current snapshot instead of unbounded duplicates.
    # invalidate-not-delete: history is preserved via valid_to.
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE themes SET valid_to = now() "
                "WHERE scope = %s AND valid_to IS NULL AND epistemic_status = 'hypothesis'",
                (scope,),
            )

    # Conversation-artifact words to exclude — episodes are formatted as
    # "[turn N] User: ... Assistant: ...", so these capitalized tokens are
    # structural noise, not meaningful themes.
    artifact_words = {
        "user", "assistant", "turn", "session", "system", "hiten", "the",
        "what", "can", "here", "this", "that", "you", "your", "and", "but",
        "for", "with", "from", "have", "will", "would", "should", "could",
        "note", "based", "done", "okay", "yes", "sure", "let", "now", "also",
        "morning", "good", "i", "im", "ive", "id",
    }

    # Extract proper noun phrases
    noun_phrase_re = re.compile(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b')
    all_text = " ".join(r[1] for r in episode_rows)
    phrases = noun_phrase_re.findall(all_text)

    # Count frequency
    phrase_counts: Counter[str] = Counter(phrases)

    # Filter by min_frequency and cluster similar (case-insensitive dedup),
    # dropping single-word conversation artifacts.
    seen: dict[str, str] = {}  # lowercase -> original case
    for phrase, count in phrase_counts.most_common():
        if count < min_frequency:
            continue
        lower = phrase.lower()
        # Skip single-word artifacts; multi-word phrases are kept (more specific)
        if " " not in lower and lower in artifact_words:
            continue
        if lower not in seen:
            seen[lower] = phrase

    themes: list[dict] = []
    for lower, original in seen.items():
        count = phrase_counts[original]
        thm_id = mint("thm")
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO themes (id, scope, name, description, frequency, epistemic_status) "
                    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                    (thm_id, scope, original,
                     f"Appears {count} times across {len(episode_rows)} episodes",
                     count, "hypothesis"),
                )
                (tid,) = await cur.fetchone()
        themes.append({
            "id": tid,
            "name": original,
            "description": f"Appears {count} times across {len(episode_rows)} episodes",
            "frequency": count,
            "epistemic_status": "hypothesis",
        })

    return themes


# ── Compose chapters (task API) ─────────────────────────────────────────────

async def compose_chapters(
    pool: Any,
    publisher: Any,
    scope: str,
    llm_client: Any = None,
    llm_model: str = "",
) -> list[dict]:
    """Detect turning points from episodes and INSERT life_chapters.

    Orders episodes by ingested_at, detects significant topic shifts as chapter
    boundaries, then SYNTHESIZES each chapter's title + summary (LLM when
    available, else a clean keyword fallback) — never a raw episode/transcript
    line. All chapters at epistemic_status='hypothesis'.
    """
    import re
    import json as _json
    from collections import Counter

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, content, ingested_at FROM episodes "
                "WHERE scope = %s "
                "ORDER BY ingested_at ASC LIMIT 200",
                (scope,),
            )
            episode_rows = await cur.fetchall()

    if len(episode_rows) < 2:
        return []

    # Idempotency: supersede prior auto-generated (hypothesis) chapters so cron
    # re-runs produce a fresh snapshot instead of unbounded duplicates.
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE life_chapters SET valid_to = now() "
                "WHERE scope = %s AND valid_to IS NULL AND epistemic_status = 'hypothesis'",
                (scope,),
            )

    # Extract top keywords per episode to detect topic shifts
    stop_words = {"the", "a", "an", "is", "are", "was", "were", "be", "been",
                  "have", "has", "had", "do", "does", "did", "will", "would",
                  "could", "should", "may", "might", "shall", "can", "to",
                  "of", "in", "for", "on", "with", "at", "by", "from", "as",
                  "into", "through", "during", "before", "after", "and", "but",
                  "or", "not", "it", "its", "this", "that", "i", "me", "my",
                  "we", "our", "you", "your", "he", "she", "they", "them"}

    def top_keywords(content: str, n: int = 5) -> set[str]:
        words = re.findall(r'\b[a-z]{3,}\b', content.lower())
        counts = Counter(w for w in words if w not in stop_words)
        return {w for w, _ in counts.most_common(n)}

    # Build keyword sets per episode
    episode_keywords: list[tuple[str, str, set[str]]] = []
    for eid, content, ingested_at in episode_rows:
        episode_keywords.append((eid, content, top_keywords(content)))

    # Segment episodes at topic shifts: a boundary is a significant Jaccard
    # distance between consecutive episodes, but only once the current segment
    # has >=3 episodes (so chapters are meaningful, not noisy singletons).
    segments: list[dict] = []
    cur_start = episode_rows[0][2]  # ingested_at
    cur_ids: list[str] = [episode_rows[0][0]]
    cur_contents: list[str] = [episode_rows[0][1]]

    for i in range(1, len(episode_keywords)):
        prev_kw = episode_keywords[i - 1][2]
        curr_kw = episode_keywords[i][2]
        intersection = prev_kw & curr_kw
        union = prev_kw | curr_kw
        similarity = len(intersection) / len(union) if union else 1.0

        if similarity < 0.3 and len(cur_ids) >= 3:
            segments.append({"start": cur_start, "ids": cur_ids, "contents": cur_contents})
            cur_start = episode_rows[i][2]
            cur_ids = [episode_rows[i][0]]
            cur_contents = [episode_rows[i][1]]
        else:
            cur_ids.append(episode_rows[i][0])
            cur_contents.append(episode_rows[i][1])

    if cur_ids:
        segments.append({"start": cur_start, "ids": cur_ids, "contents": cur_contents})

    # Title/summary synthesis — a chapter title is a THEME, never raw episode text.
    def _keyword_title(contents: list[str]) -> str:
        joined = " ".join(contents).lower()
        words = re.findall(r"\b[a-z]{4,}\b", joined)
        counts = Counter(w for w in words if w not in stop_words)
        top = [w.title() for w, _ in counts.most_common(3)]
        return ", ".join(top) if top else "Untitled chapter"

    async def _synth(contents: list[str]) -> tuple[str, str]:
        kw_title = _keyword_title(contents)
        if not llm_client:
            return kw_title, f"A period of {len(contents)} entries about {kw_title.lower()}."
        sample = "\n".join(c[:200] for c in contents[:12])
        prompt = (
            "These are journal/conversation entries from one stretch of someone's life. "
            'Return STRICT JSON {"title": ..., "summary": ...}. '
            "title = a 2-6 word thematic chapter title for what this period was ABOUT "
            "(e.g. 'Building CurlyOS', 'Health reset'). summary = one plain sentence. "
            "Synthesize the theme; do NOT copy any entry verbatim and do NOT include "
            "speaker tags ('User:'/'Assistant:'), '[turn ...]', timestamps, or 'Session ...'.\n\n"
            f"Entries:\n{sample}"
        )
        try:
            resp = await llm_client.chat.completions.create(
                model=llm_model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.4,
                max_tokens=160,
                response_format={"type": "json_object"},
            )
            data = first_json(resp.choices[0].message.content, default={})
            title = (str(data.get("title") or "")).strip()[:80] or kw_title
            summary = (str(data.get("summary") or "")).strip()[:300] or f"A period about {kw_title.lower()}."
            return title, summary
        except Exception:
            return kw_title, f"A period of {len(contents)} entries about {kw_title.lower()}."

    chapters: list[dict] = []
    for seg in segments:
        title, summary = await _synth(seg["contents"])
        cha_id = mint("cha")
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO life_chapters "
                    "(id, scope, title, summary, start_date, epistemic_status) "
                    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                    (cha_id, scope, title, summary, seg["start"], "hypothesis"),
                )
                (cid,) = await cur.fetchone()
        chapters.append({
            "id": cid,
            "title": title,
            "episodes_count": len(seg["ids"]),
            "start_date": seg["start"].isoformat() if seg["start"] else None,
            "episodes": list(seg["ids"]),
        })

    return chapters
