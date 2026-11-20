#!/usr/bin/env python3
"""Import a Google Takeout (YouTube + YouTube Music) export into CurlyOS.

The export is one substantive archive holding two HTML activity logs
(`watch-history.html`, `search-history.html`) and ~58 CSVs: playlists,
subscriptions, comments, own uploads and the YouTube Music library. A second,
tiny archive in the same delivery only carries `archive_browser.html` — Google's
own index page, with no data — so it is accepted and ignored.

Three stages, each independently re-runnable:

    signals   parse the archives into a normalised record store (cached)
    digests   bucket records into dated, deterministic digest texts
    write     POST each digest to /api/ingest (episode + memory + KG extraction)

Why digests and not raw rows: 117,685 watched entries and 21,837 YouTube Music
plays are not episodes. A digest is one paragraph per slice ("2024: 35,123
entries, top channels …, music share 31%, …"), which the extraction model can
read and the recall layer can retrieve. Raw events stay in the archive and in
the music corpus at ~/learning-music/corpus/youtube/.

Idempotency lives in `youtube_digests` (migration 0035): a digest that already
reached the API is skipped, a failed one is retried, and nothing is rewritten
unless --rewrite is passed.

Usage (from the repo root, env sourced):

    set -a; . ./.env; set +a
    .venv/bin/python -I deploy/import_youtube_takeout.py --stage signals
    .venv/bin/python -I deploy/import_youtube_takeout.py --stage digests --dry-run
    .venv/bin/python -I deploy/import_youtube_takeout.py --stage write --pace 8

Safety: the archives are untrusted data. They are opened read-only through
zipfile, their members are parsed as text/CSV only (never executed), and every
path that reaches SQL or HTTP comes from this file, not from the archive.
"""
from __future__ import annotations

import argparse
import collections
import csv
import html
import io
import json
import logging
import os
import re
import sys
import time
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("youtube-import")

SCOPE_DEFAULT = "user:usr_hiten"
EXPORT_TAG = "20261119"          # stamped into every source_ref
MAX_DIGEST_CHARS = 3200          # extraction reads this; recall stores it too
SAMPLE_LINES = 8                 # quoted examples per period digest
CACHE_VERSION = 1

RAW = "Takeout/YouTube and YouTube Music"

# ── text ─────────────────────────────────────────────────────────────────────

_WS_RE = re.compile(r"\s+")


def clean(value: Any, limit: int = 240) -> str:
    """One line: entities resolved, whitespace collapsed, truncated."""
    text = _WS_RE.sub(" ", html.unescape(str(value or ""))).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def parse_ts(value: str | None) -> datetime | None:
    """'Nov 19, 2026, 9:53:56 PM IST' → aware-local datetime (naive, IST clock)."""
    if not value:
        return None
    text = clean(value, 120)
    for fmt in ("%b %d, %Y, %I:%M:%S %p %Z", "%b %d, %Y, %I:%M:%S %p"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def iso_date(value: str | None) -> str | None:
    """First 10 chars of an ISO timestamp, validated; None when unusable."""
    if not value or len(str(value)) < 10:
        return None
    head = str(value)[:10]
    try:
        date.fromisoformat(head)
    except ValueError:
        return None
    return head


def short(titles: Iterable[str], n: int = 3, limit: int = 60) -> str:
    out = []
    for t in titles:
        t = clean(t, limit)
        if t and t not in out:
            out.append(t)
        if len(out) >= n:
            break
    return "; ".join(f'"{t}"' for t in out)


# ── archive access ───────────────────────────────────────────────────────────

class Archive:
    """Read-only view over one export: a zip, or an already-extracted folder."""

    def __init__(self, path: Path):
        self.path = path
        self._zip: zipfile.ZipFile | None = None
        if path.is_dir():
            self.root = path
        else:
            self._zip = zipfile.ZipFile(path)
            self.root = None

    def _names(self) -> list[str]:
        if self._zip is not None:
            return self._zip.namelist()
        return [str(p.relative_to(self.root)).replace(os.sep, "/")
                for p in self.root.rglob("*") if p.is_file()]

    def find(self, needle: str, endswith: str = "") -> str | None:
        """First member whose path contains `needle` (case-insensitive)."""
        needle = needle.lower()
        for name in self._names():
            low = name.lower()
            if needle in low and low.endswith(endswith) and "/media/" not in low:
                return name
        return None

    def find_all(self, needle: str, endswith: str = "") -> list[str]:
        needle = needle.lower()
        return sorted(n for n in self._names()
                      if needle in n.lower() and n.lower().endswith(endswith))

    def text(self, name: str) -> str:
        if self._zip is not None:
            with self._zip.open(name) as f:
                return f.read().decode("utf-8", errors="replace")
        return (self.root / name).read_text(encoding="utf-8", errors="replace")

    def csv_rows(self, name: str) -> list[dict[str, str]]:
        return list(csv.DictReader(io.StringIO(self.text(name))))


# ── HTML activity logs ───────────────────────────────────────────────────────

_ENTRY_RE = re.compile(
    r'<div class="outer-cell mdl-cell mdl-cell--12-col mdl-shadow--2dp">(.*?)</div></div></div>',
    re.S)
_PRODUCT_RE = re.compile(r'<p class="mdl-typography--title">(.*?)<br>', re.S)
_BODY_RE = re.compile(r'mdl-typography--body-1">(.*?)<br>', re.S)
_LINK_RE = re.compile(r'<a href="(.*?)">(.*?)</a>', re.S)
_TIME_RE = re.compile(r'<br>([A-Z][a-z]{2} \d{1,2}, \d{4}, [^<]+)<br>')
_VID_RE = re.compile(r'[?&]v=([\w-]{11})')


def _entries(text: str) -> list[str]:
    return [m.group(1) for m in _ENTRY_RE.finditer(text)]


def parse_watch_history(text: str) -> list[dict]:
    """One record per activity row: what was watched, where, and when."""
    out: list[dict] = []
    for block in _entries(text):
        product = _PRODUCT_RE.search(block)
        body = _BODY_RE.search(block)
        body_html = body.group(1) if body else ""
        # The action verb is the plain text before the first link; the rest of
        # the cell is the linked title, channel and timestamp.
        verb = clean(re.split(r"<a\b", body_html, maxsplit=1)[0], 40)
        action = verb.split()[0] if verb else ""

        video_id = title = channel = None
        for href, label in _LINK_RE.findall(block):
            if video_id is None and (m := _VID_RE.search(href)):
                video_id, title = m.group(1), clean(label, 160)
            elif channel is None and ("/channel/" in href or href.startswith("https://www.youtube.com/@")):
                channel = clean(label, 80)

        stamp = _TIME_RE.search(block)
        ts = parse_ts(clean(stamp.group(1), 60)) if stamp else None
        out.append({
            "ts": ts.isoformat() if ts else None,
            "product": clean(product.group(1), 40) if product else "",
            "action": action,
            "video_id": video_id,
            "title": title,
            "channel": channel,
        })
    return out


def parse_search_history(text: str) -> list[dict]:
    out: list[dict] = []
    for block in _entries(text):
        query = None
        for href, label in _LINK_RE.findall(block):
            if "youtube.com/results" in href or "google.com/search" in href:
                query = clean(label, 160)
                break
        if not query:
            continue
        stamp = _TIME_RE.search(block)
        ts = parse_ts(clean(stamp.group(1), 60)) if stamp else None
        out.append({"ts": ts.isoformat() if ts else None, "query": query})
    return out


# ── CSV members ──────────────────────────────────────────────────────────────

def parse_playlists(arc: Archive) -> tuple[list[dict], list[dict]]:
    """Playlist headers (from playlists.csv) and per-item additions."""
    headers: list[dict] = []
    name = arc.find("playlists/playlists.csv")
    if name:
        for row in arc.csv_rows(name):
            title = clean(row.get("Playlist Title (Original)"), 120)
            if not title:
                continue
            headers.append({
                "title": title,
                "created": iso_date(row.get("Playlist Create Timestamp")),
                "updated": iso_date(row.get("Playlist Update Timestamp")),
                "visibility": clean(row.get("Playlist Visibility"), 20),
                "description": clean(row.get("Playlist Description (Original)"), 280),
            })

    items: list[dict] = []
    for member in arc.find_all("/playlists/", ".csv"):
        base = member.rsplit("/", 1)[-1]
        if base in {"playlists.csv"} or not base.endswith("-videos.csv"):
            continue
        title = clean(base[: -len("-videos.csv")], 120)
        for row in arc.csv_rows(member):
            vid = (row.get("Video ID") or "").strip()
            if not vid:
                continue
            items.append({
                "playlist": title,
                "video_id": vid,
                "added": iso_date(row.get("Playlist Video Creation Timestamp")),
            })
    return headers, items


def parse_subscriptions(arc: Archive) -> list[dict]:
    name = arc.find("subscriptions/subscriptions.csv")
    if not name:
        return []
    return [{"title": clean(r.get("Channel Title"), 90),
             "channel_id": clean(r.get("Channel Id"), 40)}
            for r in arc.csv_rows(name) if clean(r.get("Channel Title"), 90)]


def parse_comments(arc: Archive) -> list[dict]:
    name = arc.find("comments/comments.csv")
    if not name:
        return []
    out = []
    for row in arc.csv_rows(name):
        text = clean(row.get("Comment Text"), 280)
        # Comment Text is JSON: {"text": "…"}
        if text.startswith("{"):
            try:
                text = clean(json.loads(text).get("text"), 280)
            except (ValueError, AttributeError):
                pass
        out.append({"ts": iso_date(row.get("Comment Create Timestamp")),
                    "video_id": clean(row.get("Video ID"), 20),
                    "text": text})
    return out


def parse_own_videos(arc: Archive) -> list[dict]:
    name = arc.find("video metadata/videos.csv")
    if not name:
        return []
    out = []
    for row in arc.csv_rows(name):
        title = clean(row.get("Video Title (Original)"), 140)
        if not title:
            continue
        out.append({
            "title": title,
            "published": iso_date(row.get("Video Publish Timestamp")),
            "category": clean(row.get("Video Category"), 40),
            "privacy": clean(row.get("Privacy"), 20),
            "tags": [clean(row.get(f"Tag {i}"), 30) for i in range(1, 13)
                     if clean(row.get(f"Tag {i}"), 30)],
        })
    return out


def parse_music_library(arc: Archive) -> list[dict]:
    name = arc.find("music library songs.csv")
    if not name:
        return []
    out = []
    for row in arc.csv_rows(name):
        artists = [clean(row.get(f"Artist Name {i}"), 60) for i in range(1, 18)]
        out.append({
            "video_id": clean(row.get("Video ID"), 20),
            "song": clean(row.get("Song Title"), 120),
            "album": clean(row.get("Album Title"), 120),
            "artists": [a for a in artists if a],
        })
    return out


# ── channel classification ───────────────────────────────────────────────────

# Ordered: the first cluster whose keyword appears in the channel or title wins.
# Music clusters come first because a techno label page is music even when the
# string "design" or "art" appears in it.
CLUSTERS: list[tuple[str, tuple[str, ...]]] = [
    ("psychedelic", ("psytrance", "psy-trance", "hi-tech", "hitech", "hi tech", "shpongle",
                     "kindzadza", "necropsycho", "psyntific", "mandragora", "raju baba",
                     "damru", "trippy", "dark psy", "goa", "techno trance", "trance")),
    ("electronic", ("techno", "acid", "303", "boiler room", "hör", "hor berlin", "awakenings",
                    "dj set", "dj-set", "house music", "progressive house", "melodic house",
                    "charlotte de witte", "boris brejcha", "carl cox", "artbat", "monolink",
                    "jan blomqvist", "pezsi", "artists unlimited", "cercle", "edm", "bpm",
                    "electronic", "dixon", "anetha", "miss monique", "crazymage", "virus",
                    "underdog electronic", "hate", "resident advisor", "mixmag")),
    ("hiphop-desi", ("talha anjum", "talhah yunus", "young stunners", "seedhe maut", "jokhay",
                     "dakait", "divine", "krsna", "emiway", "raftaar", "badshah", "bohemia",
                     "coke studio", "jatt prabhjot", "dino james", "king - topic", "aleemrk",
                     "inhit", "indian hip hop", "hindi hip hop", "punjabi", "gully gang",
                     "kalam ink", "anuv jain", "guru randhawa", "sez on the beat",
                     "inkalaap", "t-series", "saregama", "zee music", "sony music india")),
    ("hiphop-western", ("mgk", "machine gun kelly", "21 savage", "drake", "post malone",
                        "juice wrld", "travis scott", "metro boomin", "central cee", "jack harlow",
                        "g-eazy", "lil ", "kodak", "king von", "wiz khalifa", "ynw melly",
                        "dababy", "offset", "saint jhn", "tory lanez", "drill", "rap", "trap",
                        "hip hop", "hip-hop", "kanye", "kendrick", "future", "asap",
                        "santan dave", "headie one", "digga", "skepta", "eminem")),
    ("rock-legacy", ("pink floyd", "david gilmour", "porcupine tree", "coldplay", "linkin park",
                     "goo goo dolls", "red hot chili", "guns n", "led zeppelin", "nirvana",
                     "radiohead", "queen", "ac/dc", "metallica", "rock", "david bowie",
                     "steven tyler", "slash", "psychedelic rock", "tool", "opeth")),
    ("music-craft", ("ableton", "fl studio", "music production", "production tips", "sound design",
                     "mixing", "mastering", "music theory", "melody", "synthesis", "synth",
                     "eurorack", "serum", "vital", "bitwig", "logic pro", "musicologist",
                     "underdog", "pick yourself", "learn to dj", "dj tutorial", "jands",
                     "red means recording", "beatmaking", "type beat", "audio engineering",
                     "lacey", "seed to stage", "mr bill")),
    ("art-visual", ("touchdesigner", "blender", "cinema 4d", "c4d", "houdini", "unreal engine",
                    "notch", "resolume", "vj", "visuals", "visualizer", "motion design",
                    "motion graphics", "3d", "shader", "projection mapping", "generative art",
                    "generative", "concept art", "digital art", "art direction", "figma",
                    "graphic design", "design", "illustration", "okamirufu", "xtal")),
    ("science-tech", ("veritasium", "kurzgesagt", "3blue1brown", "fireship", "mrwhosetheboss",
                      "linus tech", "apple", "google", "anthropic", "claude", "openai", "ai ",
                      "programmer", "programming", "developer", "code", "getsetfly", "science",
                      "physics", "engineering", "electronics", "robotics", "veritasium",
                      "cleo abram", "everyday astronaut", "space", "computer", "linux",
                      "kali", "android", "tech", "lakshay chaudhary", "infographics show",
                      "dhruv rathee", "explains", "documentary", "coldfusion")),
    ("philosophy-spirituality", ("alan watts", "eckhart tolle", "krishnamurti", "vedanta",
                                 "non-duality", "nonduality", "adhyasa", "kundalini", "spiritual",
                                 "mantra", "hanuman", "shiva", "gita", "upanishad", "vedas",
                                 "dharma", "meditation", "after skool", "embodied philosophy",
                                 "asangoham", "sadhguru", "occult", "tantra", "consciousness",
                                 "psychedelic research", "hamilton morris", "dmt", "mdma",
                                 "ayahuasca", "shaman", "big think", "brainpickings")),
    ("comedy-entertainment", ("tanmay bhat", "carryminati", "stand up", "standup", "comedy",
                              "funny", "india's got latent", "samay raina", "beerbiceps",
                              "harsh beniwal", "bb ki vines", "snl", "saturday night live",
                              "adult swim", "dropout", "jcs", "criminal", "podcast", "interview",
                              "bakchod", "vines", "vine", "reaction", "roast", "gutsy ladka", "big laksh")),
    ("health-fitness", ("workout", "fitness", "gym", "chris heria", "diet", "nutrition",
                        "health", "sleep", "doctor", "medicine", "bodybuilding", "yoga")),
    ("moto-adventure", ("hunter 350", "royal enfield", "riding", "rider", "bike", "moto",
                        "adv", "offroad", "off-road", "enduro", "adventure", "himalayan",
                        "bmw motorrad", "ktm", "yamaha", "road trip", "travel")),
    ("business-finance", ("startup", "crypto", "bitcoin", "stock", "investing", "money",
                          "business", "marketing", "sales", "entrepreneur", "career", "jobs",
                          "product design", "product management", "ux", "figma config")),
    ("gaming", ("gaming", "gameplay", "cod", "call of duty", "gta", "playstation", "xbox",
                "valorant", "minecraft", "stream", "walkthrough")),
]

MUSIC_CLUSTERS = ("psychedelic", "electronic", "hiphop-desi", "hiphop-western",
                  "rock-legacy", "music-craft")
ART_CLUSTERS = ("art-visual",)
MUSIC_OTHER = "music-other"     # a "- Topic"/VEVO page that matched no keyword


def _kw(word: str) -> list[re.Pattern]:
    """Short words match whole words only — otherwise 'art' hits every 'part'.

    Multiword names also match their run-together form, because label channels
    write 'PostMaloneVEVO' where the artist is 'Post Malone'.
    """
    escaped = re.escape(word)
    if len(word) <= 5 and " " not in word:
        patterns = [re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])")]
    else:
        patterns = [re.compile(escaped)]
    squashed = re.sub(r"[\s-]", "", word)
    if squashed != word and len(squashed) >= 5:
        patterns.append(re.compile(re.escape(squashed)))
    return patterns


_COMPILED: list[tuple[str, list[re.Pattern]]] = [
    (cluster, [p for w in words for p in _kw(w)]) for cluster, words in CLUSTERS]

_TOPIC_RE = re.compile(r"-\s*topic\s*$|vevo\s*$")
_VEVO_TAIL_RE = re.compile(r"\s*(?:-\s*topic|vevo)\s*$", re.I)


def classify(channel: str | None, title: str | None = None) -> str | None:
    """Bucket a watch by channel name; fall back to the title only without one.

    Matching on the title as well produced noise: 'Part 2' contains 'art' and a
    comedy clip mentioning techno is not a techno watch. The channel is the
    stable signal; the title is only used when the export has no channel.
    """
    raw = (channel or "").strip()
    text = _VEVO_TAIL_RE.sub("", raw or (title or "")).strip().lower()
    if not text:
        return None
    for cluster, patterns in _COMPILED:
        if any(p.search(text) for p in patterns):
            return cluster
    # Every YouTube Music artist page is "<Artist> - Topic"; labels use VEVO.
    if _TOPIC_RE.search(raw.lower()):
        return MUSIC_OTHER
    return None


def artist_of(channel: str) -> str:
    """'Talha Anjum - Topic' → 'Talha Anjum'; 'Pink Floyd' stays as is."""
    name = re.sub(r"\s*-\s*Topic$", "", channel or "", flags=re.I)
    name = re.sub(r"VEVO$", "", name, flags=re.I)
    return clean(name, 80)


# ── signals ──────────────────────────────────────────────────────────────────

def collect_signals(paths: list[Path]) -> dict[str, Any]:
    archives = [Archive(p) for p in paths]
    watch: list[dict] = []
    searches: list[dict] = []
    playlists: list[dict] = []
    items: list[dict] = []
    subs: list[dict] = []
    comments: list[dict] = []
    own: list[dict] = []
    library: list[dict] = []
    sources: list[str] = []

    for arc in archives:
        sources.append(str(arc.path))
        name = arc.find("/watch-history.html", ".html")
        if name and not watch:
            watch += parse_watch_history(arc.text(name))
        name = arc.find("/search-history.html", ".html")
        if name and not searches:
            searches += parse_search_history(arc.text(name))
        if not playlists:
            playlists, items = parse_playlists(arc)
        if not subs:
            subs = parse_subscriptions(arc)
        if not comments:
            comments = parse_comments(arc)
        if not own:
            own = parse_own_videos(arc)
        if not library:
            library = parse_music_library(arc)

    # Enrich: classify each record once, here, so every digest agrees.
    for r in watch:
        r["cluster"] = classify(r.get("channel"), r.get("title"))
        r["artist"] = artist_of(r["channel"]) if r.get("channel") else None
        dt = None
        if r.get("ts"):
            try:
                dt = datetime.fromisoformat(r["ts"])
            except ValueError:
                dt = None
        r["date"] = dt.date().isoformat() if dt else None
        r["hour"] = dt.hour if dt else None
        r["year"] = dt.year if dt else None

    return {
        "sources": sources,
        "watch": watch,
        "searches": searches,
        "playlists": playlists,
        "playlist_items": items,
        "subscriptions": subs,
        "comments": comments,
        "own_videos": own,
        "music_library": library,
    }


# ── digests ──────────────────────────────────────────────────────────────────

def _top(counter: collections.Counter, n: int = 10, minimum: int = 1) -> str:
    items = [(k, v) for k, v in counter.most_common() if v >= minimum and k][:n]
    return ", ".join(f"{k} ({v})" for k, v in items)


def _digest(kind: str, start: str | None, end: str | None, label: str, body: str) -> dict:
    head = f"YouTube {kind} — {label} ({start} → {end})" if start else f"YouTube {kind} — {label}"
    text = f"{head}\n{body.strip()}"
    if len(text) > MAX_DIGEST_CHARS:
        text = text[: MAX_DIGEST_CHARS - 1].rstrip() + "…"
    return {
        "digest_key": f"{kind}:{start or '-'}:{end or '-'}",
        "kind": kind,
        "period_start": start,
        "period_end": end,
        "source_ref": f"youtube:{kind}:{label}:{EXPORT_TAG}",
        "text": text,
    }


def _spread(rows: list[dict], n: int) -> list[dict]:
    if len(rows) <= n:
        return rows
    step = len(rows) / n
    return [rows[int(i * step)] for i in range(n)]


def _watched(signals: dict) -> list[dict]:
    """Real watches only, oldest first. Ads, surveys and visits are dropped."""
    rows = [r for r in signals["watch"]
            if r.get("action") == "Watched" and r.get("date")]
    return sorted(rows, key=lambda r: (r["date"], r["hour"] or 0))


def build_digests(signals: dict[str, Any]) -> list[dict]:
    watch = _watched(signals)
    music = [r for r in watch if r["product"] == "YouTube Music"]
    digests: list[dict] = []
    if not watch:
        return digests

    first, last = watch[0], watch[-1]
    span = (first["date"], last["date"])

    # 1. Overview — the whole export in one paragraph.
    by_product = collections.Counter(r["product"] or "YouTube" for r in watch)
    by_cluster = collections.Counter(r["cluster"] or "unclassified" for r in watch)
    body = [
        f"{len(watch):,} watched entries, {span[0]} → {span[1]}.",
        f"Split: {_top(by_product, 4)}.",
        f"Interest clusters across all watching: {_top(by_cluster, 12)}.",
        f"Distinct channels: {len({r['channel'] for r in watch if r['channel']}):,}.",
        "Largest channels the classifier could not place: "
        + _top(collections.Counter(r["channel"] for r in watch
                                  if not r["cluster"] and r["channel"]), 10, 5) + ".",
        f"Playlists: {len(signals['playlists'])}, "
        f"subscriptions: {len(signals['subscriptions']):,}, "
        f"searches: {len(signals['searches']):,}, "
        f"own uploads: {len(signals['own_videos'])}.",
        "Counts are raw activity volume, not preference strength: a long techno set "
        "counts once, and YouTube Music only starts in 2024.",
    ]
    digests.append(_digest("watch-overview", span[0], span[1], "all", "\n".join(body)))

    # 2. Rhythm — when he watches.
    hours = collections.Counter(r["hour"] for r in watch if r["hour"] is not None)
    late = sum(v for h, v in hours.items() if h < 5)
    evening = sum(v for h, v in hours.items() if 19 <= h <= 23)
    weekday = collections.Counter(
        datetime.fromisoformat(r["date"]).strftime("%A") for r in watch)
    body = [
        f"HOURS (IST): {', '.join(f'{h:02d}h {hours[h]}' for h in range(24) if hours[h])}.",
        f"Peak window 11:00–22:00; late-night 00:00–04:59 is {late:,} entries "
        f"({late / len(watch):.1%}). Evening 19:00–23:59 is {evening / len(watch):.1%}.",
        f"WEEKDAY: {_top(weekday, 7)}.",
        "Reading: evenings dominate; weekends are not structurally different from weekdays.",
    ]
    digests.append(_digest("watch-rhythm", span[0], span[1], "all", "\n".join(body)))

    # 3. Per-year volume and channel mix.
    buckets: dict[int, list[dict]] = collections.defaultdict(list)
    for r in watch:
        buckets[r["year"]].append(r)
    for year in sorted(buckets):
        rows = buckets[year]
        channels = collections.Counter(r["channel"] for r in rows if r["channel"])
        clusters = collections.Counter(r["cluster"] or "unclassified" for r in rows)
        m = sum(1 for r in rows if r["product"] == "YouTube Music")
        body = [
            f"{len(rows):,} watched entries ({m:,} YouTube Music, {m / len(rows):.1%}).",
            f"Top channels: {_top(channels, 12, 2)}.",
            f"Clusters: {_top(clusters, 10, 2)}.",
            f"Sample titles: {short([r['title'] for r in _spread(rows, SAMPLE_LINES) if r['title']])}.",
        ]
        digests.append(_digest("watch-year", f"{year}-01-01", f"{year}-12-31", str(year),
                               "\n".join(body)))

    # 4. Music identity — all-time artist plays on YouTube Music.
    if music:
        artists = collections.Counter(r["artist"] for r in music if r["artist"])
        body = [
            f"{len(music):,} YouTube Music plays, {music[0]['date']} → {music[-1]['date']}.",
            f"Top artists: {_top(artists, 30)}.",
            f"Distinct artists: {len(artists):,}.",
            f"Clusters: {_top(collections.Counter(r['cluster'] or 'unclassified' for r in music), 8)}.",
            f"Sample tracks: {short([r['title'] for r in _spread(music, SAMPLE_LINES) if r['title']])}.",
            "A play is evidence of listening, not of taste; YouTube Music starts 2024-03.",
        ]
        digests.append(_digest("music-identity", music[0]["date"], music[-1]["date"], "all",
                               "\n".join(body)))

        mbuckets: dict[str, list[dict]] = collections.defaultdict(list)
        for r in music:
            mbuckets[f"{r['year']}-{r['date'][5:7]}"].append(r)
        years: dict[int, list[dict]] = collections.defaultdict(list)
        for key, rows in mbuckets.items():
            years[int(key[:4])] += rows
        for year in sorted(years):
            rows = sorted(years[year], key=lambda r: r["date"])
            artists = collections.Counter(r["artist"] for r in rows if r["artist"])
            body = [
                f"{len(rows):,} plays across {len({r['date'][:7] for r in rows})} months.",
                f"Top artists: {_top(artists, 15, 2)}.",
                f"Sample tracks: {short([r['title'] for r in _spread(rows, 6) if r['title']])}.",
            ]
            digests.append(_digest("music-year", f"{year}-01-01", f"{year}-12-31", str(year),
                                   "\n".join(body)))

    # 5. Music and art clusters — what each taste world actually contains.
    for cluster in MUSIC_CLUSTERS + ART_CLUSTERS + (MUSIC_OTHER,):
        rows = [r for r in watch if r["cluster"] == cluster]
        if len(rows) < 10:
            continue
        channels = collections.Counter(r["channel"] for r in rows if r["channel"])
        artists = collections.Counter(artist_of(r["channel"]) for r in rows if r["channel"])
        sample = [r["title"] for r in _spread(rows, 10) if r["title"]]
        body = [
            f"{len(rows):,} entries, {rows[0]['date']} → {rows[-1]['date']}.",
            f"Top channels: {_top(channels, 12, 2)}.",
            f"Top artists/pages: {_top(artists, 12, 2)}.",
            f"Peak year: {collections.Counter(r['year'] for r in rows).most_common(1)[0][0]}.",
            f"Sample: {short(sample)}.",
        ]
        digests.append(_digest(f"interest-{cluster}", rows[0]["date"], rows[-1]["date"],
                               cluster, "\n".join(body)))

    # 6. Playlists — the taxonomy, then the largest ones in detail.
    if signals["playlists"]:
        items_by_pl: dict[str, list[dict]] = collections.defaultdict(list)
        for it in signals["playlist_items"]:
            items_by_pl[it["playlist"]].append(it)
        titles_by_vid = {r["video_id"]: r for r in watch if r.get("video_id")}
        ordered = sorted(items_by_pl.items(), key=lambda kv: -len(kv[1]))
        lines = [f"{len(signals['playlists'])} playlists, "
                 f"{len(signals['playlist_items']):,} items total."]
        for title, rows in ordered[:30]:
            stamp = min((it["added"] for it in rows if it["added"]), default=None)
            lines.append(f"- {title} ({len(rows)} items, from {stamp or 'undated'})")
        digests.append(_digest("playlists-taxonomy",
                               min((p["created"] for p in signals["playlists"] if p["created"]),
                                   default=None),
                               max((p["updated"] for p in signals["playlists"] if p["updated"]),
                                   default=None),
                               "all", "\n".join(lines)))

        for title, rows in ordered[:15]:
            if len(rows) < 20:
                continue
            stamps = sorted(it["added"] for it in rows if it["added"])
            known = [titles_by_vid[f"{it['video_id']}"] for it in rows
                     if f"{it['video_id']}" in titles_by_vid]
            chans = collections.Counter(r["channel"] for r in known if r["channel"])
            body = [
                f"{len(rows)} items added {stamps[0] if stamps else '?'} → "
                f"{stamps[-1] if stamps else '?'}.",
                f"Channels among the {len(known)} items he also watched: "
                f"{_top(chans, 10, 2) or 'none recorded'}.",
                f"Sample titles: {short([r['title'] for r in _spread(known, 8)])}.",
            ]
            key = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]
            digests.append(_digest("playlist", stamps[0] if stamps else None,
                                   stamps[-1] if stamps else None, key, "\n".join(body)))

    # 7. Searches — what he went looking for, by year.
    if signals["searches"]:
        rows = []
        for s in signals["searches"]:
            if not s.get("ts"):
                continue
            try:
                rows.append({**s, "year": int(s["ts"][:4])})
            except ValueError:
                continue
        if rows:
            per_year: dict[int, list[dict]] = collections.defaultdict(list)
            for r in rows:
                per_year[r["year"]].append(r)
            for year in sorted(per_year):
                year_rows = per_year[year]
                queries = collections.Counter(r["query"] for r in year_rows)
                body = [
                    f"{len(year_rows):,} searches.",
                    f"Most repeated: {_top(queries, 20, 2)}.",
                ]
                digests.append(_digest("searches", f"{year}-01-01", f"{year}-12-31", str(year),
                                       "\n".join(body)))

    # 8. Subscriptions — the standing intent, not the momentary watch.
    if signals["subscriptions"]:
        titles = [s["title"] for s in signals["subscriptions"]]
        clusters = collections.Counter(classify(t) or "other" for t in titles)
        by_cluster: dict[str, list[str]] = collections.defaultdict(list)
        for t in titles:
            by_cluster[classify(t) or "other"].append(t)
        lines = [f"{len(titles):,} subscriptions.", f"Clusters: {_top(clusters, 14)}."]
        for cluster, _ in clusters.most_common(8):
            lines.append(f"- {cluster}: {', '.join(sorted(by_cluster[cluster])[:18])}")
        digests.append(_digest("subscriptions", None, None, "all", "\n".join(lines)))

    # 9. Own channel — what he has published.
    if signals["own_videos"]:
        rows = sorted((v for v in signals["own_videos"] if v["published"]),
                      key=lambda r: r["published"])
        by_cat = collections.Counter(r["category"] for r in rows)
        music_videos = [r for r in rows if "crazymage" in r["title"].lower()
                        or "techno" in r["title"].lower()]
        body = [
            f"Channel 'Hiten Saxena': {len(rows)} uploaded videos, "
            f"{rows[0]['published']} → {rows[-1]['published']}.",
            f"Categories: {_top(by_cat, 8)}.",
            f"Titles: {', '.join(r['title'] for r in rows[-14:])}.",
            f"CRAZYMAGE-music uploads ({len(music_videos)}): ",
            ", ".join("{0} ({1})".format(v["title"], v["published"]) for v in music_videos),
        ]
        digests.append(_digest("own-channel",
                               rows[0]["published"], rows[-1]["published"], "all",
                               "\n".join(body)))

    # 10. Comments — the few things he wrote in public.
    if signals["comments"]:
        rows = sorted(signals["comments"], key=lambda r: r["ts"] or "")
        body = [f"{len(rows)} comments, {rows[0]['ts']} → {rows[-1]['ts']}."]
        body += [f'- {r["ts"]} on {r["video_id"]}: "{r["text"]}"' for r in rows[:15]]
        digests.append(_digest("comments", rows[0]["ts"], rows[-1]["ts"], "all",
                               "\n".join(body)))

    # 11. Timeline shifts — when each interest world appears and peaks.
    lines = []
    for cluster in MUSIC_CLUSTERS + ART_CLUSTERS + (MUSIC_OTHER, "science-tech",
                                                    "philosophy-spirituality",
                                                    "comedy-entertainment", "moto-adventure",
                                                    "business-finance"):
        rows = [r for r in watch if r["cluster"] == cluster]
        if not rows:
            continue
        years = collections.Counter(r["year"] for r in rows)
        peak = years.most_common(1)[0]
        lines.append(f"- {cluster}: first {rows[0]['date']}, last {rows[-1]['date']}, "
                     f"peak {peak[0]} ({peak[1]:,}/{len(rows):,}).")
    if lines:
        digests.append(_digest("timeline-shifts", span[0], span[1], "all", "\n".join(lines)))

    return digests


# ── write stage ──────────────────────────────────────────────────────────────

def write_digests(digests: list[dict], *, api: str, scope: str, dsn: str,
                  pace: float, limit: int, rewrite: bool) -> dict[str, int]:
    import httpx
    import psycopg

    stats = collections.Counter()
    conn = psycopg.connect(dsn, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT digest_key, status FROM youtube_digests")
            ledger = dict(cur.fetchall())

        chosen = [d for d in digests if rewrite or ledger.get(d["digest_key"]) != "written"]
        if limit:
            chosen = chosen[:limit]
        log.info("write: %d of %d digests to send", len(chosen), len(digests))

        with httpx.Client(timeout=180.0) as client:
            for i, d in enumerate(chosen, 1):
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO youtube_digests "
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
                    epi = resp.json().get("epi_id")
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE youtube_digests SET status='written', episode_id=%s, "
                            "written_at=now() WHERE digest_key=%s",
                            (epi, d["digest_key"]),
                        )
                    stats["written"] += 1
                    log.info("write: [%d/%d] %s → %s", i, len(chosen), d["source_ref"], epi)
                except Exception as exc:  # noqa: BLE001 — one bad digest must not stop the run
                    stats["failed"] += 1
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE youtube_digests SET status='failed', error=%s "
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
    signals = collect_signals([Path(p).expanduser() for p in args.archive])
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(signals, f)
    tmp.replace(path)
    log.info("signals: cached to %s in %.0fs", path, time.time() - started)
    return signals


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", action="append", default=None,
                    help="takeout zip or extracted folder; repeatable")
    ap.add_argument("--stage", choices=["signals", "digests", "write"], default="digests")
    ap.add_argument("--scratch", default="~/.cache/curlyos/youtube-20261119")
    ap.add_argument("--scope", default=os.environ.get("CURLYOS_SCOPE", SCOPE_DEFAULT))
    ap.add_argument("--api", default=os.environ.get("CURLYOS_API_URL", "http://127.0.0.1:8643"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--pace", type=float, default=8.0, help="seconds between POSTs")
    ap.add_argument("--out", default="", help="write digest JSON here (digests stage)")
    ap.add_argument("--refresh", action="store_true", help="ignore the signals cache")
    ap.add_argument("--rewrite", action="store_true", help="re-send digests already written")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.archive:
        args.archive = [os.path.expanduser(
            "~/Downloads/takeout-20261119T164423Z-1-001.zip")]

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    signals = load_or_build_signals(args)
    for key, value in signals.items():
        if isinstance(value, list):
            log.info("signals: %-18s %d records", key, len(value))
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
