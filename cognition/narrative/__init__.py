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
from datetime import datetime
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

# ── Themes ───────────────────────────────────────────────────────────────────

# Names that describe the corpus rather than the person: Hiten is the subject of
# every note, and "User"/"assistant" are speaker labels, not themes.
_HUB_NAMES = ["hiten", "hiten saxena", "user", "assistant"]

_THEME_PROMPT = """You are naming the recurring themes in one person's life and work, from evidence in their personal knowledge system.

Frequent entities (name — type — connections in the graph):
{entities}

Recent notes:
{notes}

Return STRICT JSON only:
{{"themes": [{{"name": "2-5 words", "description": "one concrete sentence", "entities": ["names from the list above"]}}]}}

Rules:
- 6 to 10 themes.
- A theme is a domain of activity or concern ("live techno performance", "memory architecture"), never a single entity and never a person's name.
- Each theme needs at least two supporting entities or notes.
- No markdown, no commentary."""


async def _theme_inputs(pool: Any, scope: str, lookback_days: int, limit: int):
    """Two independent signals: the graph's most-connected entities, and what
    the person has actually been writing lately."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "WITH deg AS ("
                "  SELECT eid, count(*) d FROM ("
                "    SELECT src_entity_id eid FROM knowledge_edges WHERE valid_to IS NULL "
                "    UNION ALL SELECT dst_entity_id FROM knowledge_edges WHERE valid_to IS NULL"
                "  ) x GROUP BY eid) "
                "SELECT e.name, e.label, COALESCE(d.d, 0) AS deg FROM knowledge_entities e "
                "LEFT JOIN deg d ON d.eid = e.id "
                "WHERE e.scope = %s AND e.valid_to IS NULL "
                "  AND lower(e.name) <> ALL(%s::text[]) AND COALESCE(d.d, 0) >= 2 "
                "ORDER BY deg DESC NULLS LAST, e.created_at ASC LIMIT %s",
                (scope, _HUB_NAMES, limit),
            )
            entities = await cur.fetchall()
            await cur.execute(
                "SELECT statement FROM memories "
                "WHERE scope = %s AND valid_to IS NULL AND NOT is_chatter "
                "  AND created_at >= now() - make_interval(days => %s) "
                "ORDER BY created_at DESC LIMIT 40",
                (scope, lookback_days),
            )
            notes = [r[0] for r in await cur.fetchall() if r[0]]
    return entities, notes


async def _llm_themes(llm_client: Any, llm_model: str, entities, notes) -> list[dict]:
    ent_lines = "\n".join(
        f"- {name} — {label or 'thing'} — {deg} connections" for name, label, deg in entities
    )
    note_lines = "\n".join(f"- {n[:180]}" for n in notes)
    prompt = _THEME_PROMPT.format(entities=ent_lines, notes=note_lines or "- (none)")
    resp = await llm_client.chat.completions.create(
        model=llm_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=3000,  # reasoning models need room past reasoning_content
        response_format={"type": "json_object"},
    )
    data = first_json(resp.choices[0].message.content, default={})
    out: list[dict] = []
    for t in (data.get("themes") or []):
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "").strip()[:80]
        desc = str(t.get("description") or "").strip()[:300]
        ents = [str(e).strip()[:80] for e in (t.get("entities") or []) if str(e).strip()][:12]
        if not name or not desc:
            continue
        out.append({"name": name, "description": desc,
                    "frequency": max(1, len(ents)), "entities": ents})
    return out


async def surface_themes(
    pool: Any,
    publisher: Any,
    scope: str,
    *,
    llm_client: Any = None,
    llm_model: str = "",
    max_themes: int = 10,
    lookback_days: int = 30,
) -> list[dict]:
    """Name the recurring themes of the current period and INSERT into `themes`.

    The previous version ranked graph entities by degree and called the top of
    the list a theme — "Hiten Saxena; connected to 4076 things" — which is why
    the table read as a leaderboard. A theme is a domain the person keeps
    returning to, so it is derived by an LLM from two signals that can disagree
    (what the graph connects most, and what was written lately) and stored with
    the entities that support it.

    Failure is explicit: with no LLM or no usable output, nothing is written and
    the previous themes stay valid. (Superseding first and failing after left
    the table emptier than when it started.)
    """
    entities, notes = await _theme_inputs(pool, scope, lookback_days, 60)
    if not entities and not notes:
        return []
    if llm_client is None:
        log.warning("themes: no LLM client for %s — keeping the existing themes", scope)
        return []
    try:
        themes = await _llm_themes(llm_client, llm_model, entities, notes)
    except Exception as exc:  # noqa: BLE001 — a failed run must not wipe the table
        log.warning("themes: synthesis failed for %s: %s", scope, exc)
        return []
    if not themes:
        log.warning("themes: LLM returned no usable themes for %s — keeping the existing set", scope)
        return []

    themes = themes[:max_themes]
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE themes SET valid_to = now() "
                "WHERE scope = %s AND valid_to IS NULL AND epistemic_status = 'hypothesis'",
                (scope,),
            )
            for t in themes:
                await cur.execute(
                    "INSERT INTO themes (id, scope, name, description, frequency, epistemic_status) "
                    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                    (mint("thm"), scope, t["name"], t["description"],
                     t["frequency"], "hypothesis"),
                )
                (t["id"],) = await cur.fetchone()
    return themes


# ── Compose chapters (task API) ─────────────────────────────────────────────

CHAPTER_WINDOW_DAYS = 30     # a chapter is a stretch of life, not a week
CHAPTER_MIN_EPISODES = 3     # quieter stretches are not chapters at all
CHAPTER_SPLIT_OVER = 400     # a denser stretch than this is split at topic shifts
CHAPTER_MERGE_BELOW = 20     # a 1-day sliver at a window edge joins its neighbour
CHAPTER_MAX_PARTS = 3
CHAPTER_CONTENT_CHARS = 800  # enough for keywords and a sample; keeps the scan light


def _keywords(content: str, n: int = 8) -> set[str]:
    stop = {"the", "and", "for", "that", "with", "this", "from", "have", "were",
            "what", "when", "your", "about", "there", "would", "could", "should",
            "into", "than", "then", "them", "they", "been", "being", "does",
            "did", "can", "will", "not", "you", "our", "its", "are", "was",
            "has", "had", "but", "all", "any", "how", "why", "who", "use",
            "used", "using", "also", "just", "like", "one", "two", "out",
            "get", "got", "make", "made", "now", "new", "see", "way"}
    words = re.findall(r"\b[a-z]{4,}\b", (content or "").lower())
    return {w for w, _ in Counter(w for w in words if w not in stop).most_common(n)}


def _jaccard(a: set[str], b: set[str]) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 1.0


def _time_windows(rows) -> list[dict]:
    """Contiguous windows over the WHOLE timeline, anchored at the first entry.

    The old code read `ORDER BY ingested_at ASC LIMIT 200`, so every run
    described the oldest 200 episodes — June 2026 forever — no matter what had
    happened since.
    """
    segments: list[dict] = []
    if not rows:
        return segments
    start = rows[0][2]
    cur = {"start": start, "end": start, "ids": [], "contents": [], "times": []}
    for rid, content, ts in rows:
        if cur["ids"] and (ts - start).days >= CHAPTER_WINDOW_DAYS:
            segments.append(cur)
            start = ts
            cur = {"start": ts, "end": ts, "ids": [], "contents": [], "times": []}
        cur["ids"].append(rid)
        cur["contents"].append(content or "")
        cur["times"].append(ts)
        cur["end"] = ts
    if cur["ids"]:
        segments.append(cur)
    return segments


def _split_topic(seg: dict) -> list[dict]:
    """Split an oversized window into balanced parts, at the nearest topic shift.

    Cuts used to be placed only where the topic genuinely shifted, which left
    the bulk of a dense stretch (1,958 of 1,990 entries) as a single part and
    two slivers at its start. Parts are sized first, then each boundary is
    nudged within a small window to the least-similar adjacent pair.
    """
    n = len(seg["ids"])
    if n <= CHAPTER_SPLIT_OVER or CHAPTER_MAX_PARTS <= 1:
        return [seg]
    parts = min(CHAPTER_MAX_PARTS, -(-n // CHAPTER_SPLIT_OVER))  # ceil
    kws = [_keywords(c) for c in seg["contents"]]
    reach = max(5, n // (10 * parts))
    bounds = [0]
    for k in range(1, parts):
        target = round(n * k / parts)
        lo = max(bounds[-1] + 1, target - reach)
        hi = min(n - 1, target + reach)
        best, best_sim = target, None
        for i in range(lo, hi + 1):
            sim = _jaccard(kws[i - 1], kws[i])
            if best_sim is None or sim < best_sim:
                best, best_sim = i, sim
        bounds.append(best)
    bounds.append(n)
    out = []
    for a, b in zip(bounds, bounds[1:]):
        if b - a < 4:
            continue
        out.append({
            "start": seg["times"][a], "end": seg["times"][b - 1],
            "ids": seg["ids"][a:b], "contents": seg["contents"][a:b],
            "times": seg["times"][a:b],
        })
    return out or [seg]


def _merge_small(segments: list[dict]) -> list[dict]:
    """Fold window-edge slivers into the chapter before them.

    Anchoring windows on the first entry leaves short stubs whenever a busy
    stretch resumes just before a boundary ("11 Sep 2026 — 18 entries"). An
    18-entry day is not a chapter, so it joins its neighbour instead.
    """
    out: list[dict] = []
    for seg in segments:
        if out and len(seg["ids"]) < CHAPTER_MERGE_BELOW:
            prev = out[-1]
            for key in ("ids", "contents", "times"):
                prev[key].extend(seg[key])
            prev["end"] = seg["end"]
        else:
            out.append({**seg, "ids": list(seg["ids"]),
                        "contents": list(seg["contents"]), "times": list(seg["times"])})
    return out


def _date_range(start, end) -> str:
    try:
        if start and end and start.date() == end.date():
            return f"{start:%d %b %Y}"
        if start and end:
            return f"{start:%d %b} – {end:%d %b %Y}"
    except AttributeError:
        pass
    return "undated period"


def _looks_like_keyword_list(title: str) -> bool:
    """Reject "Tasks, Task, Flow" — the old mechanical fallback in disguise."""
    return bool(re.fullmatch(r"[A-Z][a-z]+, [A-Z][a-z]+, [A-Z][a-z]+", title.strip()))


async def _chapter_text(seg: dict, graph_ctx: str, llm_client: Any, llm_model: str):
    """Return (title, summary, synthesised). A degraded chapter says so: it is
    dated, not dressed up as a theme, and the caller logs the count."""
    span = _date_range(seg["start"], seg["end"])
    count = len(seg["ids"])
    fallback = (span, f"{count} entries between {span}.", False)
    if llm_client is None:
        return fallback
    sample = "\n".join(c[:200] for c in seg["contents"][:12])
    prompt = (
        f"These are entries from one stretch of someone's life ({span}, {count} entries). "
        'Return STRICT JSON {"title": ..., "summary": ...}. '
        "title = a 2-6 word thematic phrase for what this period was ABOUT "
        "(e.g. 'Building CurlyOS', 'Health reset'). It must be a phrase, never a "
        "comma-separated list of keywords. "
        "summary = one plain sentence. "
        "Synthesize the theme; do NOT copy any entry verbatim and do NOT include "
        "speaker tags ('User:'/'Assistant:'), '[turn ...]', timestamps, or 'Session ...'.\n\n"
        + (f"Current knowledge-graph context:\n{graph_ctx}\n\n" if graph_ctx else "")
        + f"Entries:\n{sample}"
    )
    try:
        resp = await llm_client.chat.completions.create(
            model=llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=3000,  # deep-tier reasoning models need room past reasoning_content
            response_format={"type": "json_object"},
        )
        data = first_json(resp.choices[0].message.content, default={})
        title = str(data.get("title") or "").strip()[:80]
        summary = str(data.get("summary") or "").strip()[:300]
        if not title or _looks_like_keyword_list(title):
            raise ValueError(f"unusable title {title!r}")
        return title, summary or span, True
    except Exception as exc:  # noqa: BLE001 — but never silent
        log.warning("chapters: synthesis failed for %s (%s) — using the date range", span, exc)
        return fallback


async def compose_chapters(
    pool: Any,
    publisher: Any,
    scope: str,
    llm_client: Any = None,
    llm_model: str = "",
) -> list[dict]:
    """Segment the whole episode timeline into chapters and name each one.

    Two things were wrong before: the query read the oldest 200 episodes (so
    every run re-described June 2026 whatever the present was), and `end_date`
    was never written. Chapters are now contiguous windows over the full
    timeline, each with a real end, sub-split at topic shifts only when a window
    is large, and titled by the LLM with an honest dated fallback.
    """
    from knowledge.graph import graph_context as _graph_ctx

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, left(content, %s) AS content, ingested_at FROM episodes "
                "WHERE scope = %s ORDER BY ingested_at ASC, id ASC",
                (CHAPTER_CONTENT_CHARS, scope),
            )
            rows = await cur.fetchall()
    if len(rows) < CHAPTER_MIN_EPISODES:
        return []

    segments: list[dict] = []
    for window in _time_windows(rows):
        if len(window["ids"]) < CHAPTER_MIN_EPISODES:
            continue
        segments.extend(_split_topic(window))
    segments = _merge_small(segments)
    if not segments:
        return []

    graph_ctx = await _graph_ctx(pool, scope, top_n=20) if llm_client else ""
    chapters: list[dict] = []
    degraded = 0
    for seg in segments:
        title, summary, synthesised = await _chapter_text(seg, graph_ctx, llm_client, llm_model)
        degraded += 0 if synthesised else 1
        chapters.append({
            "id": mint("cha"),
            "title": title,
            "summary": summary,
            "start_date": seg["start"],
            "end_date": seg["end"],
            "episodes_count": len(seg["ids"]),
            "episodes": list(seg["ids"]),
        })

    # Supersede only once the new set exists: a crash mid-generation must not
    # leave the narrative empty.
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE life_chapters SET valid_to = now() "
                "WHERE scope = %s AND valid_to IS NULL AND epistemic_status = 'hypothesis'",
                (scope,),
            )
            for c in chapters:
                await cur.execute(
                    "INSERT INTO life_chapters "
                    "(id, scope, title, summary, start_date, end_date, epistemic_status) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (c["id"], scope, c["title"], c["summary"],
                     c["start_date"], c["end_date"], "hypothesis"),
                )
                (c["id"],) = await cur.fetchone()

    if degraded:
        log.warning("chapters: %d of %d fell back to a date range (LLM unavailable "
                    "or returned junk)", degraded, len(chapters))
    return chapters
