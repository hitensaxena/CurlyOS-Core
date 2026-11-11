"""Instagram import — export decoding, bucketing, and what never gets written.

The importer's job is to turn an untrusted archive into a small number of
readable digests. These cover the parts that silently produced wrong output
when the parsers were first pointed at the real export: mojibake that survived
into every caption, a nested Owner group that emptied "Top accounts", and the
month-end arithmetic for recent months.
"""
from __future__ import annotations

import importlib.util
import json
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load():
    spec = importlib.util.spec_from_file_location(
        "import_instagram_export", ROOT / "deploy" / "import_instagram_export.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


IG = _load()


# ── text decoding ────────────────────────────────────────────────────────────
def test_mojibake_is_repaired():
    # Instagram writes UTF-8 bytes through a latin-1 encoder.
    mangled = "😊 already here".encode("utf-8").decode("latin-1")
    assert IG.fix_text(mangled) == "😊 already here"
    assert IG.clean("line one\n\nline two") == "line one line two"


def test_already_valid_text_is_left_alone():
    assert IG.fix_text("₹8 Crore — 10% step-up") == "₹8 Crore — 10% step-up"
    assert IG.fix_text(None) == ""


def test_clean_truncates_on_a_word_boundary():
    out = IG.clean("x" * 300, 40)
    assert len(out) == 40 and out.endswith("…")


# ── label_values decoding ────────────────────────────────────────────────────
def test_two_level_owner_group_still_yields_the_username():
    # The real shape: Owner → {"title": ""} → [URL, Name, Username].
    entry = {"label_values": [
        {"label": "Caption", "value": "hello #techno"},
        {"title": "Owner", "dict": [
            {"title": "", "dict": [
                {"label": "Name", "value": "Hiten"},
                {"label": "Username", "value": "crazymage21"}]}]},
        {"title": "Hashtags", "dict": [{"label": "Name", "value": "techno"}]},
    ]}
    flat, groups = IG.parse_labels(entry)
    assert flat["Caption"] == "hello #techno"
    assert IG.owner_of(groups) == "crazymage21"
    assert IG.tags_of(groups) == ["techno"]


def test_string_map_data_is_flattened_with_timestamps():
    flat, _ = IG.parse_labels({"string_map_data": {
        "Comment": {"value": "Ignite"},
        "Media Owner": {"value": "ignitemymusic"},
        "Time": {"timestamp": 1794349573},
    }})
    assert flat["Comment"] == "Ignite"
    assert flat["Time (ts)"] == "1794349573"


# ── periods ──────────────────────────────────────────────────────────────────
def test_old_years_bucket_yearly_and_2026_buckets_monthly():
    start, end, label = IG.period_of(1704067200)      # 2024-01-01
    assert (start, end, label) == (date(2024, 1, 1), date(2024, 12, 31), "2024")
    start, end, label = IG.period_of(1794000000)      # 2026-11-06
    assert (start.year, label) == (2026, "2026-11")
    assert end.day == 30                              # November has a real end


# ── digests ──────────────────────────────────────────────────────────────────
def _records(n: int, ts: int, actor: str = "someone") -> list[dict]:
    return [{"kind": "likes", "ts": ts + i, "actor": actor,
             "text": f"caption {i}", "tags": ["techno"], "meta": {}} for i in range(n)]


def test_a_thin_period_does_not_become_a_digest():
    out = IG._periodic_digests("likes", _records(3, 1704067200), min_period=10)
    assert out == []


def test_digest_carries_counts_tags_and_samples():
    out = IG._periodic_digests("likes", _records(20, 1704067200), min_period=10)
    assert len(out) == 1
    text = out[0]["text"]
    assert "20 items." in text and "Top hashtags: techno (20)" in text
    assert out[0]["source_ref"] == "instagram:likes:2024:20261111"
    assert len(text) <= IG.MAX_DIGEST_CHARS


def test_samples_are_evenly_spread_not_just_the_newest():
    rows = _records(100, 1704067200)
    picked = IG._spread(rows, 8)
    assert len(picked) == 8
    assert picked[0] is rows[0] and picked[-1] is not rows[-1]


# ── messages ─────────────────────────────────────────────────────────────────
def _dm_thread(count: int, start_ts: int, spread_days: int = 30) -> dict:
    step = spread_days * 86400 // max(1, count)
    texts = [{"ts": start_ts + i * step, "who": "me" if i % 2 else "them",
              "sender": "Friend", "text": f"message number {i}"} for i in range(count)]
    return {"folder": "friend_1", "title": "Friend",
            "participants": {"Hiten Saxena", "Friend"}, "texts": texts}


class _FakeArchive:
    def __init__(self, threads):
        self.threads = threads

    def names(self, needle, endswith=".json"):
        return [f"messages/inbox/{t['folder']}/message_1.json" for t in self.threads]

    def load(self, name):
        folder = name.split("/")[2]
        thread = next(t for t in self.threads if t["folder"] == folder)
        return {
            "title": thread["title"],
            "participants": [{"name": p} for p in thread["participants"]],
            "messages": [{"sender_name": "Hiten Saxena" if m["who"] == "me" else m["sender"],
                          "timestamp_ms": m["ts"] * 1000, "content": m["text"]}
                         for m in thread["texts"]],
        }


def test_small_threads_stay_in_the_rollup_and_are_not_episodes():
    out = IG._messages_signal(_FakeArchive([_dm_thread(20, 1704067200)]), "Hiten Saxena")
    assert out["dm_threads"] == []
    assert len(out["dm_rollup"]) == 1


def test_attachment_placeholders_are_not_quoted_as_messages():
    thread = _dm_thread(60, 1704067200)
    thread["texts"][5]["text"] = "Hiten Saxena sent an attachment."
    thread["texts"][6]["text"] = "Instagram user sent an attachment."
    out = IG._messages_signal(_FakeArchive([thread]), "Hiten Saxena")
    quoted = json.dumps(out["dm_threads"])
    assert "sent an attachment" not in quoted


def test_a_very_long_thread_is_chunked_by_year_so_it_is_not_one_blob():
    out = IG._messages_signal(_FakeArchive([_dm_thread(4000, 1685577600, spread_days=800)]),
                              "Hiten Saxena")
    chunks = out["dm_threads"]
    assert len(chunks) >= 2
    assert len({c["start"] // 86400 for c in chunks}) == len(chunks)
    assert sum(c["count"] for c in chunks) <= 4000


def test_a_mid_size_thread_stays_one_episode_even_across_years():
    # One 2,000-message thread is still one relationship; only threads long
    # enough to change subject per year are split.
    out = IG._messages_signal(_FakeArchive([_dm_thread(2000, 1685577600, spread_days=600)]),
                              "Hiten Saxena")
    assert len(out["dm_threads"]) == 1
