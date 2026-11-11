#!/usr/bin/env python3
"""Import an Instagram data export into CurlyOS as dated, deterministic digests.

The export is two archives (Instagram splits large exports): an activity
archive with ~476 JSON files, and a messages archive with only photo/video
attachments. This importer reads **JSON members straight out of the zips** —
nothing is extracted to disk, and media is never touched (there is no vision
pipeline to read it).

Three stages, each independently re-runnable:

    signals   parse the archives into a normalised record store (cached)
    digests   bucket records into dated, deterministic digest texts
    write     POST each digest to /api/ingest (episode + memory + KG extraction)

Why digests and not raw rows: 5,742 likes and 83,428 messages are not
episodes. A digest is one paragraph per period ("2024: 812 posts liked, top
hashtags techno 61 / philosophy 38, sample captions …"), which the extraction
model can read and the recall layer can retrieve. Raw rosters and message
bodies stay in the archive.

Idempotency lives in `instagram_digests` (migration 0022): a digest that
already reached the API is skipped, a failed one is retried, and nothing is
rewritten unless --rewrite is passed.

Usage (from the repo root, env sourced):

    set -a; . ./.env; set +a
    .venv/bin/python -I deploy/import_instagram_export.py \\
        --activity ~/Downloads/instagram-crazymage21-2026-11-11-UT3slwyH.zip \\
        --stage signals
    .venv/bin/python -I deploy/import_instagram_export.py \\
        --stage digests --dry-run          # read these before writing
    .venv/bin/python -I deploy/import_instagram_export.py \\
        --stage write --pace 10

Safety: the archives are untrusted data. They are opened read-only through
zipfile, their members are parsed as JSON only (never executed), and every
path that reaches SQL or HTTP comes from this file, not from the archive.
"""
from __future__ import annotations

import argparse
import collections
import json
import logging
import os
import re
import sys
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("instagram-import")

SCOPE_DEFAULT = "user:usr_hiten"
EXPORT_TAG = "20261111"          # stamped into every source_ref
MAX_DIGEST_CHARS = 3200          # extraction reads this; recall stores it too
SAMPLE_LINES = 8                 # quoted examples per period digest
CACHE_VERSION = 2

# ── text ─────────────────────────────────────────────────────────────────────

def fix_text(value: Any) -> str:
    """Repair the export's mojibake.

    Instagram writes UTF-8 bytes through a latin-1 encoder, so an emoji
    arrives as 'Ã°Å¸â€˜â€¹'. Round-tripping through latin-1 restores it. Strings
    that are already valid (anything outside latin-1, e.g. '₹' or an emoji
    that survived) raise and are returned untouched.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def clean(value: Any, limit: int = 240) -> str:
    """One-line text: mojibake repaired, whitespace collapsed, truncated."""
    text = re.sub(r"\s+", " ", fix_text(value)).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


_HASHTAG_RE = re.compile(r"#(\w{2,40})")


def hashtags_of(*texts: str) -> list[str]:
    tags: list[str] = []
    for t in texts:
        tags.extend(m.group(1).lower() for m in _HASHTAG_RE.finditer(fix_text(t or "")))
    return tags


def date_of(ts: Any) -> date | None:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).date()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def iso(ts: Any) -> str | None:
    d = date_of(ts)
    return d.isoformat() if d else None


# ── export shapes ────────────────────────────────────────────────────────────

def parse_labels(entry: dict) -> tuple[dict[str, str], dict[str, list[dict]]]:
    """Split Instagram's label_values into flat labels and titled groups.

    Two shapes occur. Flat:  {"label": "Caption", "value": "…"}.
    Grouped:  {"title": "Owner", "dict": [{"label": "Username", "value": "x"}]}.
    Older entries also nest a group inside a group, so this recurses.
    """
    flat: dict[str, str] = {}
    groups: dict[str, list[dict]] = {}

    def walk(items: Iterable[Any], title: str = "") -> None:
        for item in items or []:
            if not isinstance(item, dict):
                continue
            if "label" in item:
                if title:
                    groups.setdefault(title, []).append(
                        {"label": str(item["label"]), "value": fix_text(item.get("value"))})
                else:
                    flat.setdefault(str(item["label"]), fix_text(item.get("value")))
            elif "title" in item:
                # A group may itself be nested under an untitled wrapper
                # (Owner → "" → [Username…]), so the parent title is sticky.
                walk(item.get("dict") or [], str(item["title"]) or title)

    walk(entry.get("label_values") or [])
    smap = entry.get("string_map_data") or {}
    for key, val in smap.items():
        if isinstance(val, dict):
            if val.get("value"):
                flat.setdefault(key, fix_text(val.get("value")))
            if val.get("timestamp"):
                flat.setdefault(key + " (ts)", str(val["timestamp"]))
    return flat, groups


def owner_of(groups: dict[str, list[dict]]) -> str:
    for item in groups.get("Owner") or []:
        if item.get("label") == "Username" and item.get("value"):
            return str(item["value"])
    return ""


def tags_of(groups: dict[str, list[dict]]) -> list[str]:
    return [str(i["value"]).lower() for i in (groups.get("Hashtags") or [])
            if i.get("value")]


def entry_ts(entry: dict, flat: dict[str, str]) -> int | None:
    for key in ("timestamp",):
        if entry.get(key):
            return int(entry[key])
    for candidate in ("Time (ts)", "Update time (ts)", "Creation timestamp (ts)",
                      "Change date (ts)", "Update time"):
        if flat.get(candidate):
            try:
                return int(flat[candidate])
            except ValueError:
                continue
    return None


# ── archive reading ──────────────────────────────────────────────────────────

class Archive:
    """Read-only view over one export zip."""

    def __init__(self, path: Path):
        self.path = path
        self.zip = zipfile.ZipFile(path)

    def names(self, needle: str, endswith: str = ".json") -> list[str]:
        needle = needle.lower()
        return [n for n in self.zip.namelist()
                if needle in n.lower() and n.endswith(endswith)
                and "/media/" not in n]

    def load(self, name: str) -> Any:
        with self.zip.open(name) as f:
            return json.load(f)


def _iter_entries(data: Any) -> Iterable[dict]:
    """Instagram files are a list, or a dict of one or more lists."""
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                yield item
    elif isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        yield item


def _posts_signal(arc: Archive, needles: list[str], kind: str) -> list[dict]:
    """likes / saved / story_likes share one shape: ts + caption + owner."""
    out = []
    for name in needles:
        for n in arc.names(name):
            for entry in _iter_entries(arc.load(n)):
                flat, groups = parse_labels(entry)
                ts = entry_ts(entry, flat)
                if ts is None:
                    continue
                caption = flat.get("Caption") or flat.get("Title") or ""
                out.append({
                    "kind": kind,
                    "ts": ts,
                    "actor": owner_of(groups) or flat.get("Media Owner", ""),
                    "text": clean(caption, 320),
                    "tags": tags_of(groups) + hashtags_of(caption),
                    "meta": {"url": flat.get("URL", "")},
                })
    return out


def _comments_signal(arc: Archive) -> list[dict]:
    out = []
    for needle in ("comments/post_comments", "comments/reels_comments"):
        for n in arc.names(needle):
            for entry in _iter_entries(arc.load(n)):
                flat, _ = parse_labels(entry)
                ts = entry_ts(entry, flat)
                if ts is None:
                    continue
                out.append({
                    "kind": "comments",
                    "ts": ts,
                    "actor": flat.get("Media Owner", ""),
                    "text": clean(flat.get("Comment", ""), 240),
                    "tags": [],
                    "meta": {},
                })
    return out


def _story_interactions_signal(arc: Archive) -> list[dict]:
    out = []
    for needle, kind, key in (
        ("story_interactions/polls", "polls", "Question"),
        ("story_interactions/quizzes", "quizzes", "Question"),
        ("story_interactions/questions", "questions", "Answer"),
        ("story_interactions/emoji_sliders", "emoji_sliders", "Emoji"),
    ):
        for n in arc.names(needle):
            for entry in _iter_entries(arc.load(n)):
                flat, groups = parse_labels(entry)
                ts = entry_ts(entry, flat)
                if ts is None:
                    continue
                options = [flat[f"Option {i}"] for i in range(1, 5)
                           if flat.get(f"Option {i}")]
                text = flat.get(key) or flat.get("Question") or ""
                if kind == "polls" and options:
                    text = f"{text} → {', '.join(clean(o, 60) for o in options)}"
                if kind == "quizzes" and flat.get("Answer"):
                    text = f"{text} → {flat['Answer']}"
                out.append({
                    "kind": kind,
                    "ts": ts,
                    "actor": owner_of(groups) or flat.get("Username", ""),
                    "text": clean(text, 240),
                    "tags": [],
                    "meta": {},
                })
    return out


def _searches_signal(arc: Archive) -> list[dict]:
    out = []
    for n in arc.names("recent_searches/"):
        for entry in _iter_entries(arc.load(n)):
            flat, _ = parse_labels(entry)
            ts = entry_ts(entry, flat)
            # `recent_searches.json` carries label_values; the per-type files
            # (searches_user/hashtag/keyword) carry `title` + string_list_data.
            query = (flat.get("Search query") or flat.get("Name") or flat.get("Search")
                     or entry.get("title") or "")
            if not query:
                continue
            if not ts:
                for item in entry.get("string_list_data") or []:
                    ts = item.get("timestamp") or ts
            out.append({
                "kind": "searches",
                "ts": ts or 0,
                "actor": "",
                "text": clean(query, 120),
                "tags": [],
                "meta": {"file": n.rsplit("/", 1)[-1]},
            })
    return out


def _connections_signal(arc: Archive) -> list[dict]:
    """Followers / following / close friends / churn — counts, not rosters."""
    out = []
    wanted = {
        "following.json": ("following", "follows"),
        "followers_1.json": ("followers", "followed by"),
        "close_friends.json": ("close_friends", "close friend"),
        "recently_unfollowed_profiles.json": ("unfollowed", "unfollowed"),
        "recent_follow_requests.json": ("follow_requests", "requested to follow"),
        "blocked_profiles.json": ("blocked", "blocked"),
        "following_hashtags.json": ("hashtags_followed", "follows hashtag"),
    }
    for name in arc.names("followers_and_following/"):
        base = name.rsplit("/", 1)[-1]
        if base not in wanted:
            continue
        kind, verb = wanted[base]
        for entry in _iter_entries(arc.load(name)):
            flat, _ = parse_labels(entry)
            # Two shapes: `title` + string_list_data (followers, following) and
            # label_values with Name/Username (close friends, churn, blocked).
            label, ts = "", None
            for item in entry.get("string_list_data") or []:
                ts = item.get("timestamp") or ts
                label = label or item.get("value") or ""
            label = clean(entry.get("title") or flat.get("Username") or flat.get("Name")
                          or label, 80)
            ts = ts or entry.get("timestamp")
            if not label:
                continue
            out.append({
                "kind": kind,
                "ts": ts or 0,
                "actor": clean(label, 80),
                "text": f"{verb} {clean(label, 80)}",
                "tags": [],
                "meta": {},
            })
    return out


def _threads_signal(arc: Archive) -> list[dict]:
    out = []
    for n in arc.names("threads/threads_viewed"):
        for entry in _iter_entries(arc.load(n)):
            flat, _ = parse_labels(entry)
            ts = entry_ts(entry, flat)
            author = flat.get("Author", "")
            if ts is None or not author:
                continue
            out.append({"kind": "threads", "ts": ts, "actor": clean(author, 80),
                        "text": f"saw a Threads post by {clean(author, 80)}",
                        "tags": [], "meta": {"url": flat.get("URL", "")}})
    for n in arc.names("threads/following"):
        for entry in _iter_entries(arc.load(n)):
            label = entry.get("title")
            ts = 0
            for item in entry.get("string_list_data") or []:
                ts = item.get("timestamp") or ts
            if label:
                out.append({"kind": "threads_following", "ts": ts,
                            "actor": clean(label, 80), "text": f"follows {label} on Threads",
                            "tags": [], "meta": {}})
    return out


def _insights_signal(arc: Archive) -> list[dict]:
    """Own posts / reels / stories: cadence and reach, one record per item."""
    out = []
    for needle, kind in (("insights/posts", "own_posts"),
                         ("insights/reels", "own_reels"),
                         ("insights/stories", "own_stories"),
                         ("insights/videos", "own_videos")):
        for n in arc.names(needle):
            data = arc.load(n)
            for entry in _iter_entries(data):
                flat, _ = parse_labels(entry)
                media = entry.get("media_map_data") or {}
                thumb = media.get("Media thumbnail") or {}
                ts = entry_ts(entry, flat) or thumb.get("creation_timestamp")
                if ts is None:
                    continue
                metrics = {k: v for k, v in flat.items()
                           if k not in ("Creation timestamp",) and str(v).strip().isdigit()}
                out.append({
                    "kind": kind,
                    "ts": ts,
                    "actor": "",
                    "text": clean(thumb.get("title") or flat.get("Caption", ""), 200),
                    "tags": hashtags_of(thumb.get("title", "")),
                    "meta": metrics,
                })
    return out


def _profile_changes_signal(arc: Archive) -> list[dict]:
    out = []
    for n in arc.names("profile_changes"):
        for entry in _iter_entries(arc.load(n)):
            flat, _ = parse_labels(entry)
            ts = entry_ts(entry, flat)
            if ts is None:
                continue
            what = flat.get("Changed", "")
            old = flat.get("Previous value", "")
            new = flat.get("New value", "")
            out.append({
                "kind": "profile_changes",
                "ts": ts,
                "actor": "",
                "text": clean(f"{what}: {old or '(empty)'} → {new or '(empty)'}", 200),
                "tags": [],
                "meta": {},
            })
    return out


# ── messages ─────────────────────────────────────────────────────────────────

_ATTACHMENT_RE = re.compile(r"\bsent an attachment\.?$")
_NOISE = {"ok", "hmm", "hm", "haan", "ha", "hmm.", "ok.", "yes", "no", "nahi", "ya"}


def _messages_signal(arc: Archive, me: str) -> dict[str, list[dict]]:
    """One record per thread, each carrying its sampled lines and stats.

    Threads arrive split across message_1.json, message_2.json … so they are
    grouped by folder. A thread whose text is long enough to span years is
    chunked by calendar year — a 23,000-message thread summarised once would
    describe nothing.
    """
    threads: dict[str, dict] = {}
    for n in arc.names("messages/", endswith=".json"):
        if "/inbox/" not in n and "/message_requests/" not in n:
            continue
        if not n.rsplit("/", 1)[-1].startswith("message_"):
            continue
        folder = n.rsplit("/", 2)[-2]
        try:
            data = arc.load(n)
        except (json.JSONDecodeError, UnicodeDecodeError, KeyError, OSError) as exc:
            log.warning("messages: skipping unreadable %s (%s)", folder, exc)
            continue
        thread = threads.setdefault(folder, {
            "folder": folder, "title": "", "participants": set(), "texts": [],
        })
        thread["title"] = thread["title"] or clean(data.get("title", ""), 80)
        for p in data.get("participants") or []:
            if p.get("name"):
                thread["participants"].add(fix_text(p["name"]))
        for m in data.get("messages") or []:
            ts = m.get("timestamp_ms")
            body = fix_text(m.get("content") or "")
            if not ts or not body:
                continue
            if _ATTACHMENT_RE.search(body):
                continue
            thread["texts"].append({
                "ts": int(ts) // 1000,
                "who": "me" if fix_text(m.get("sender_name")) == me else "them",
                "sender": clean(m.get("sender_name", ""), 60),
                "text": clean(body, 200),
            })

    out: dict[str, list[dict]] = {"dm_threads": [], "dm_rollup": []}
    rollup = []
    for folder, thread in threads.items():
        texts = thread["texts"]
        if not texts:
            continue
        texts.sort(key=lambda t: t["ts"])
        span_start, span_end = texts[0]["ts"], texts[-1]["ts"]
        rollup.append({
            "folder": folder,
            "title": thread["title"] or " · ".join(sorted(thread["participants"])) or folder,
            "count": len(texts),
            "start": span_start,
            "end": span_end,
            "others": sorted(p for p in thread["participants"] if p != me),
        })
        if len(texts) < 50:
            continue                      # tiny threads live only in the rollup
        by_year: dict[int, list[dict]] = collections.defaultdict(list)
        for t in texts:
            by_year[datetime.fromtimestamp(t["ts"], tz=timezone.utc).year].append(t)
        chunks = [by_year[y] for y in sorted(by_year)]
        if len(texts) < 1200:
            chunks = [texts]
        elif len(texts) < 3000 and len(chunks) > 2:
            chunks = [texts]              # 2 years of a mid-size thread read fine together
        for chunk in chunks:
            if len(chunk) < 40:
                continue
            out["dm_threads"].append({
                "folder": folder,
                "title": thread["title"] or folder,
                "others": sorted(p for p in thread["participants"] if p != me),
                "start": chunk[0]["ts"],
                "end": chunk[-1]["ts"],
                "count": len(chunk),
                "sent_by_me": sum(1 for t in chunk if t["who"] == "me"),
                "sample": _sample_lines(chunk, 24),
            })
    out["dm_rollup"] = rollup
    return out


def _sample_lines(chunk: list[dict], n: int) -> list[dict]:
    """Evenly spaced, noise-filtered lines — representative, not cherry-picked."""
    useful = [t for t in chunk
              if len(t["text"]) >= 4 and t["text"].lower().strip(" .!") not in _NOISE]
    pool = useful or chunk
    if len(pool) <= n:
        return pool
    step = len(pool) / n
    return [pool[int(i * step)] for i in range(n)]


# ── signal store ─────────────────────────────────────────────────────────────

def collect_signals(activity: Path, messages: Path | None, me: str) -> dict[str, Any]:
    arc = Archive(activity)
    signals: dict[str, Any] = {}
    signals["likes"] = _posts_signal(arc, ["likes/liked_posts"], "likes")
    signals["saved"] = _posts_signal(arc, ["saved/saved_posts"], "saved")
    signals["story_likes"] = _posts_signal(arc, ["story_interactions/story_likes"], "story_likes")
    signals["stories_viewed"] = _posts_signal(arc, ["story_interactions/stories_viewed"], "stories_viewed")
    signals["comments"] = _comments_signal(arc)
    signals["story_interactions"] = _story_interactions_signal(arc)
    signals["searches"] = _searches_signal(arc)
    signals["connections"] = _connections_signal(arc)
    signals["threads"] = _threads_signal(arc)
    signals["insights"] = _insights_signal(arc)
    signals["profile_changes"] = _profile_changes_signal(arc)
    # Instagram splits exports: sometimes the message JSON rides with the
    # activity archive and the second zip holds only attachments. Parse
    # whichever archive actually carries message_*.json.
    dms: dict[str, list[dict]] = {"dm_threads": [], "dm_rollup": []}
    candidates = [Archive(p) for p in (messages, activity) if p is not None]
    for candidate in candidates:
        if not candidate.names("messages/", endswith=".json"):
            continue
        dms = _messages_signal(candidate, me)
        if dms["dm_threads"] or dms["dm_rollup"]:
            log.info("signals: messages read from %s", candidate.path.name)
            break
    signals["dm_threads"] = dms["dm_threads"]
    signals["dm_rollup"] = dms["dm_rollup"]
    for key, value in signals.items():
        log.info("signals: %-20s %d records", key, len(value))
    return signals


# ── digests ──────────────────────────────────────────────────────────────────

def period_of(ts: int) -> tuple[date, date, str]:
    """Yearly buckets through 2025, monthly for 2026.

    The recent period is the one that gets re-derived, so it keeps its
    resolution; the older years would produce 130 near-identical monthly
    digests for no extra signal.
    """
    d = date_of(ts) or date(1970, 1, 1)
    if d.year >= 2026:
        first = date(d.year, d.month, 1)
        nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        return first, nxt - timedelta(days=1), f"{first.year}-{first.month:02d}"
    return date(d.year, 1, 1), date(d.year, 12, 31), str(d.year)


def _top(counter: collections.Counter, n: int = 10, minimum: int = 1) -> str:
    items = [(k, v) for k, v in counter.most_common() if v >= minimum and k][:n]
    return ", ".join(f"{k} ({v})" for k, v in items)


def _digest(kind: str, start: date, end: date, label: str, body: str) -> dict:
    text = f"Instagram {kind} — {label} ({start.isoformat()} → {end.isoformat()})\n{body.strip()}"
    if len(text) > MAX_DIGEST_CHARS:
        text = text[: MAX_DIGEST_CHARS - 1].rstrip() + "…"
    return {
        "digest_key": f"{kind}:{start.isoformat()}:{end.isoformat()}",
        "kind": kind,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "source_ref": f"instagram:{kind}:{label}:{EXPORT_TAG}",
        "text": text,
    }


def _periodic_digests(kind: str, records: list[dict], min_period: int = 10,
                      sample_label: str = "Sample") -> list[dict]:
    buckets: dict[str, list[dict]] = collections.defaultdict(list)
    for r in records:
        buckets[period_of(r["ts"])[2]].append(r)
    out = []
    for label in sorted(buckets):
        rows = buckets[label]
        if len(rows) < min_period:
            continue
        start, end, _ = period_of(rows[0]["ts"])
        actors = collections.Counter(r["actor"] for r in rows if r["actor"])
        tags = collections.Counter(t for r in rows for t in r["tags"])
        lines = [f"{len(rows)} items."]
        if actors:
            lines.append(f"Top accounts: {_top(actors, 8, 2) or _top(actors, 5)}.")
        if tags:
            lines.append(f"Top hashtags: {_top(tags, 10, 2)}.")
        samples = [r["text"] for r in _spread(rows, SAMPLE_LINES) if r["text"]]
        if samples:
            lines.append(f"{sample_label}:")
            lines.extend(f'- "{s}"' for s in samples)
        out.append(_digest(kind, start, end, label, "\n".join(lines)))
    return out


def _spread(rows: list[dict], n: int) -> list[dict]:
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    return [rows[int(i * step)] for i in range(n)]


def build_digests(signals: dict[str, Any]) -> list[dict]:
    digests: list[dict] = []

    # Interests: what he chose to amplify, keep, and write to people.
    # 50, not 10: the first year of an account is 13 stray likes — enough to
    # create an episode whose only content is a celebrity name.
    digests += _periodic_digests("likes", signals["likes"], min_period=50)
    digests += _periodic_digests("saved", signals["saved"])
    digests += _periodic_digests("comments_written", signals["comments"], min_period=5,
                                 sample_label="Comments he wrote")
    digests += _periodic_digests("story_likes", signals["story_likes"], min_period=15)

    # Attention: what was passively consumed (one month of story views).
    viewed = signals["stories_viewed"]
    if viewed:
        by_account = collections.Counter(r["actor"] for r in viewed if r["actor"])
        by_tag = collections.Counter(t for r in viewed for t in r["tags"])
        start, end = period_of(viewed[0]["ts"])[:2]
        digests.append(_digest(
            "stories_viewed", start, end, "window",
            f"{len(viewed)} stories and reels viewed.\n"
            f"Top accounts: {_top(by_account, 12, 3)}.\n"
            f"Top hashtags: {_top(by_tag, 12, 3)}.\n"
            f"Window: {iso(min(r['ts'] for r in viewed))} → {iso(max(r['ts'] for r in viewed))}.",
        ))

    # Intent: searches are the most direct "what is he looking for" signal.
    searches = signals["searches"]
    if searches:
        searches.sort(key=lambda r: r["ts"], reverse=True)
        lines = [f"{len(searches)} searches on record:"]
        lines += [f'- "{r["text"]}" ({r["meta"].get("file", "")})' for r in searches[:40]]
        start, end = period_of(searches[-1]["ts"])[:2]
        digests.append(_digest("searches", start, end, "window", "\n".join(lines)))

    # Authored story interactions: what he asked his audience.
    inter = signals["story_interactions"]
    for kind, label in (("polls", "polls_published"), ("questions", "questions_answered"),
                        ("quizzes", "quizzes_answered"), ("emoji_sliders", "emoji_sliders")):
        rows = [r for r in inter if r["kind"] == kind]
        if not rows:
            continue
        lines = [f"{len(rows)} interactions."]
        lines += [f'- "{r["text"]}"' for r in _spread(rows, 20)]
        start, end = period_of(rows[0]["ts"])[:2]
        digests.append(_digest(label, start, end, "all", "\n".join(lines)))

    # Own content: cadence and reach.
    insights = signals["insights"]
    for kind, label in (("own_posts", "own_posts"), ("own_reels", "own_reels")):
        rows = [r for r in insights if r["kind"] == kind]
        if len(rows) < 3:
            continue
        reach = collections.Counter()
        for r in rows:
            for k, v in r["meta"].items():
                if k in ("Reach", "Impressions", "Profile visits", "Accounts reached", "Follows"):
                    reach[k] += int(v)
        labels = [r["text"] for r in rows if r["text"]]
        tags = collections.Counter(t for r in rows for t in r["tags"])
        lines = [f"{len(rows)} published, {iso(min(r['ts'] for r in rows))} → {iso(max(r['ts'] for r in rows))}."]
        if reach:
            lines.append("Totals: " + ", ".join(f"{k.lower()} {v}" for k, v in reach.items()) + ".")
        if tags:
            lines.append(f"Top hashtags: {_top(tags, 10, 2)}.")
        if labels:
            lines.append("Captions he published:")
            lines += [f'- "{s}"' for s in _spread(labels, 12)]
        start, end = period_of(rows[0]["ts"])[:2]
        digests.append(_digest(label, start, end, "all", "\n".join(lines)))

    stories = [r for r in insights if r["kind"] == "own_stories"]
    if stories:
        reach = sum(int(r["meta"].get("Accounts reached", 0) or 0) for r in stories)
        digests.append(_digest(
            "own_stories", period_of(stories[0]["ts"])[0], period_of(stories[-1]["ts"])[1], "all",
            f"{len(stories)} stories published, {iso(min(r['ts'] for r in stories))} → "
            f"{iso(max(r['ts'] for r in stories))}; combined accounts reached {reach}.",
        ))

    # Relationships: counted, not roster-dumped.
    conns = signals["connections"]
    by_kind = collections.defaultdict(list)
    for r in conns:
        by_kind[r["kind"]].append(r)
    if by_kind:
        start = min((date_of(r["ts"]) for rows in by_kind.values() for r in rows if r["ts"]),
                    default=date(2013, 12, 28))
        lines = [f"follows {len(by_kind.get('following', []))} accounts",
                 f"followed by {len(by_kind.get('followers', []))}",
                 f"close friends {len(by_kind.get('close_friends', []))}",
                 f"follow hashtags {len(by_kind.get('hashtags_followed', []))}",
                 f"pending requests {len(by_kind.get('follow_requests', []))}"]
        digests.append(_digest("social_snapshot", start, date.today(), "20261111",
                               "Snapshot of the Instagram graph: " + "; ".join(lines) + "."))
        unfollowed = by_kind.get("unfollowed", [])
        if unfollowed:
            dumped = collections.Counter(r["actor"] for r in unfollowed)
            lines = [f"{len(unfollowed)} accounts unfollowed between "
                     f"{iso(min(r['ts'] for r in unfollowed if r['ts']))} and "
                     f"{iso(max(r['ts'] for r in unfollowed if r['ts']))} — a churn burst, "
                     f"not steady trimming."]
            lines.append(f"Most repeated: {_top(dumped, 8, 2) or '(all once)'}.")
            digests.append(_digest("social_churn", date(2026, 9, 1), date(2026, 11, 10),
                                   "window", "\n".join(lines)))

    # Threads (the app) — consumption and follows.
    threads = [r for r in signals["threads"] if r["kind"] == "threads"]
    if threads:
        authors = collections.Counter(r["actor"] for r in threads)
        digests.append(_digest(
            "threads_viewed", period_of(min(r["ts"] for r in threads))[0],
            period_of(max(r["ts"] for r in threads))[1], "window",
            f"{len(threads)} Threads posts viewed, {iso(min(r['ts'] for r in threads))} → "
            f"{iso(max(r['ts'] for r in threads))}.\nTop authors: {_top(authors, 12, 3)}.",
        ))

    changes = signals["profile_changes"]
    if changes:
        changes.sort(key=lambda r: r["ts"])
        lines = [f"{len(changes)} profile changes, {iso(changes[0]['ts'])} → {iso(changes[-1]['ts'])}."]
        lines += [f'- {iso(r["ts"])}: "{r["text"]}"' for r in _spread(changes, 16)]
        digests.append(_digest("profile_changes", date_of(changes[0]["ts"]),
                               date_of(changes[-1]["ts"]), "all", "\n".join(lines)))

    # Messages: one digest per substantial thread chunk, plus a rollup.
    for thread in signals["dm_threads"]:
        start, end = date_of(thread["start"]), date_of(thread["end"])
        share = thread["sent_by_me"] / max(1, thread["count"])
        lines = [f"Thread with {', '.join(thread['others']) or thread['title']}: "
                 f"{thread['count']} text messages, {iso(thread['start'])} → {iso(thread['end'])}; "
                 f"{share:.0%} sent by Hiten."]
        lines.append("Sampled messages in order:")
        for s in thread["sample"]:
            who = "Hiten" if s["who"] == "me" else s["sender"]
            lines.append(f'- {iso(s["ts"])} {who}: "{s["text"]}"')
        digests.append(_digest("dm_thread", start, end,
                               f"{thread['folder']}-{start.isoformat()}", "\n".join(lines)))

    rollup = sorted(signals["dm_rollup"], key=lambda r: r["count"], reverse=True)
    if rollup:
        total = sum(r["count"] for r in rollup)
        lines = [f"{len(rollup)} direct-message threads with text, {total} messages total.",
                 "Most active (messages, span):"]
        for r in rollup[:25]:
            lines.append(f"- {r['title']}: {r['count']} ({iso(r['start'])} → {iso(r['end'])})")
        quiet = sum(1 for r in rollup if r["count"] < 50)
        lines.append(f"{quiet} threads have under 50 messages — acquaintances, not correspondents.")
        digests.append(_digest("dm_rollup", date_of(min(r["start"] for r in rollup)),
                               date_of(max(r["end"] for r in rollup)), "all", "\n".join(lines)))
    return digests


# ── writing ──────────────────────────────────────────────────────────────────

def write_digests(digests: list[dict], *, api: str, scope: str, dsn: str,
                  pace: float, limit: int, rewrite: bool) -> dict[str, int]:
    import httpx
    import psycopg

    stats = collections.Counter()
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT digest_key, status FROM instagram_digests")
            ledger = dict(cur.fetchall())

        chosen = [d for d in digests if rewrite or ledger.get(d["digest_key"]) != "written"]
        if limit:
            chosen = chosen[:limit]
        log.info("write: %d of %d digests to send", len(chosen), len(digests))

        with httpx.Client(timeout=120.0) as client:
            for i, d in enumerate(chosen, 1):
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO instagram_digests "
                        "(digest_key, kind, period_start, period_end, source_ref, chars, status) "
                        "VALUES (%s,%s,%s,%s,%s,%s,'pending') "
                        "ON CONFLICT (digest_key) DO UPDATE SET status='pending', error=NULL",
                        (d["digest_key"], d["kind"], d["period_start"], d["period_end"],
                         d["source_ref"], len(d["text"])),
                    )
                try:
                    resp = client.post(f"{api}/api/ingest", json={
                        "text": d["text"], "source_ref": d["source_ref"], "scope": scope,
                        "add_memory": True, "extract_knowledge": True, "kind": "fact",
                        "epistemic_status": "canonical",
                    })
                    resp.raise_for_status()
                    body = resp.json()
                    epi = body.get("epi_id")
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE instagram_digests SET status='written', episode_id=%s, "
                            "written_at=now() WHERE digest_key=%s",
                            (epi, d["digest_key"]),
                        )
                    stats["written"] += 1
                    log.info("write: [%d/%d] %s → %s", i, len(chosen), d["source_ref"], epi)
                except Exception as exc:  # noqa: BLE001 — one bad digest must not stop the run
                    stats["failed"] += 1
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE instagram_digests SET status='failed', error=%s "
                            "WHERE digest_key=%s", (str(exc)[:500], d["digest_key"]),
                        )
                    log.error("write: %s failed: %s", d["source_ref"], exc)
                if pace and i < len(chosen):
                    time.sleep(pace)
    finally:
        conn.close()
    return dict(stats)


# ── stage running ────────────────────────────────────────────────────────────

def cache_path(scratch: Path) -> Path:
    return scratch / f"signals-v{CACHE_VERSION}.json"


def load_or_build_signals(args) -> dict[str, Any]:
    scratch = Path(args.scratch).expanduser()
    scratch.mkdir(parents=True, exist_ok=True)
    path = cache_path(scratch)
    if path.exists() and not args.refresh:
        log.info("signals: using cache %s", path)
        with path.open() as f:
            return json.load(f)
    started = time.time()
    signals = collect_signals(Path(args.activity).expanduser(),
                              Path(args.messages).expanduser() if args.messages else None,
                              args.me)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(signals, f)
    tmp.replace(path)
    log.info("signals: cached to %s in %.0fs", path, time.time() - started)
    return signals


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--activity", default=os.path.expanduser(
        "~/Downloads/instagram-crazymage21-2026-11-11-UT3slwyH.zip"))
    ap.add_argument("--messages", default=os.path.expanduser(
        "~/Downloads/instagram-crazymage21-2026-11-11-cC6PB892.zip"),
        help="attachment-only archive; parsed only if it carries message JSON")
    ap.add_argument("--stage", choices=["signals", "digests", "write"], default="digests")
    ap.add_argument("--scratch", default="~/.cache/curlyos/instagram-20261111")
    ap.add_argument("--scope", default=os.environ.get("CURLYOS_SCOPE", SCOPE_DEFAULT))
    ap.add_argument("--api", default=os.environ.get("CURLYOS_API_URL", "http://127.0.0.1:8643"))
    ap.add_argument("--me", default="Hiten Saxena", help="sender name that counts as 'me'")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--pace", type=float, default=10.0, help="seconds between POSTs")
    ap.add_argument("--out", default="", help="write digest JSON here (digests stage)")
    ap.add_argument("--refresh", action="store_true", help="ignore the signals cache")
    ap.add_argument("--rewrite", action="store_true", help="re-send digests already written")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    signals = load_or_build_signals(args)
    if args.stage == "signals":
        return
    digests = build_digests(signals)
    log.info("digests: %d (%d chars total)", len(digests),
             sum(len(d["text"]) for d in digests))
    if args.out:
        Path(args.out).expanduser().write_text(json.dumps(digests, indent=1))
        log.info("digests: wrote %s", args.out)
    if args.stage == "digests":
        if args.dry_run:
            for d in digests[: args.limit or len(digests)]:
                print(f"\n─── {d['source_ref']} ({len(d['text'])} chars)")
                print(d["text"])
        return

    if args.dry_run:
        for d in digests:
            print(f"{d['source_ref']:60} {len(d['text']):5d} chars")
        return
    dsn = os.environ.get("CURLYOS_DATABASE_URL")
    if not dsn:
        sys.exit("CURLYOS_DATABASE_URL is not set — source .env first")
    stats = write_digests(digests, api=args.api, scope=args.scope, dsn=dsn,
                          pace=args.pace, limit=args.limit, rewrite=args.rewrite)
    log.info("write: %s", stats)


if __name__ == "__main__":
    main()
