"""Narrative engine — segmentation, honest fallbacks, and failure behaviour.

These cover the two defects that made the derived layer read as noise: the
chapter query only ever saw the oldest 200 episodes, and every LLM failure
silently degraded to a keyword list (or, for themes, wiped the table first).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import cognition.narrative as N

T0 = datetime(2026, 6, 7, 9, 0)


def _rows(*days_and_counts):
    """(day offset, episode count) pairs → synthetic (id, content, ts) rows."""
    rows = []
    i = 0
    for day, count in days_and_counts:
        for _ in range(count):
            rows.append((f"epi_{i}", f"note {i} about curlyos and memory", T0 + timedelta(days=day)))
            i += 1
    return rows


# ── timeline segmentation ─────────────────────────────────────────────────────
def test_windows_span_the_whole_timeline_not_just_the_beginning():
    rows = _rows((0, 2), (30, 2), (150, 2))
    windows = N._time_windows(rows)
    assert len(windows) == 3
    assert windows[0]["start"] == T0
    assert windows[-1]["end"] == T0 + timedelta(days=150)


def test_windows_are_contiguous_and_carry_their_own_end():
    win = N.CHAPTER_WINDOW_DAYS
    rows = _rows((0, 3), (2, 3), (win + 2, 3))
    windows = N._time_windows(rows)
    assert [len(seg["ids"]) for seg in windows] == [6, 3]
    assert all(w["end"] >= w["start"] for w in windows)
    for a, b in zip(windows, windows[1:]):
        assert b["start"] > a["end"]


def test_window_edge_slivers_join_the_chapter_before_them():
    big = N._time_windows(_rows((0, 100)))[0]
    sliver = N._time_windows(_rows((N.CHAPTER_WINDOW_DAYS + 1, 5)))[0]
    merged = N._merge_small([big, sliver])
    assert len(merged) == 1
    assert len(merged[0]["ids"]) == 105
    assert merged[0]["end"] == sliver["end"]


def test_oversized_window_is_split_with_real_sub_ranges():
    n = N.CHAPTER_SPLIT_OVER + 50
    rows = _rows((0, n))
    windows = N._time_windows(rows)
    parts = N._split_topic(windows[0])
    assert 1 < len(parts) <= N.CHAPTER_MAX_PARTS
    assert sum(len(p["ids"]) for p in parts) == n
    assert parts[0]["start"] <= parts[-1]["end"]


def test_small_window_is_left_alone():
    rows = _rows((0, 10))
    seg = N._time_windows(rows)[0]
    assert N._split_topic(seg) == [seg]


# ── titles ───────────────────────────────────────────────────────────────────
def test_keyword_triplets_are_rejected_as_titles():
    assert N._looks_like_keyword_list("Tasks, Task, Flow")
    assert not N._looks_like_keyword_list("Building CurlyOS")


def test_date_range_reads_like_a_date():
    assert N._date_range(T0, T0) == "07 Jun 2026"
    assert N._date_range(T0, T0 + timedelta(days=5)) == "07 Jun – 12 Jun 2026"


@pytest.mark.asyncio
async def test_chapter_without_llm_is_dated_not_keyword_mashed():
    seg = N._time_windows(_rows((0, 12)))[0]
    title, summary, synthesised = await N._chapter_text(seg, "", None, "")
    assert title == "07 Jun 2026" and synthesised is False
    assert "12 entries" in summary


class _BrokenLLM:
    class chat:
        class completions:
            @staticmethod
            async def create(**kw):
                raise RuntimeError("chain exhausted")


@pytest.mark.asyncio
async def test_chapter_llm_failure_is_announced_and_dated():
    seg = N._time_windows(_rows((0, 12)))[0]
    title, _, synthesised = await N._chapter_text(seg, "", _BrokenLLM(), "m")
    assert synthesised is False and title == "07 Jun 2026"


# ── themes: failure keeps the old set ────────────────────────────────────────
class _WriteCountingPool:
    def __init__(self, entities=(), notes=()):
        self.entities = list(entities)
        self.notes = list(notes)
        self.writes = []

    def connection(self):
        pool = self

        class _Cur:
            def __init__(self):
                self._rows = []

            async def execute(self, sql, params=None):
                s = " ".join(sql.split())
                if s.startswith("INSERT") or s.startswith("UPDATE"):
                    pool.writes.append((s, params))
                self._rows = pool.entities if "knowledge_entities" in s else [
                    (n,) for n in pool.notes
                ]

            async def fetchall(self):
                return self._rows

            async def fetchone(self):
                return ("thm_test",)

            rowcount = 0

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class _Conn:
            def cursor(self):
                return _Cur()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        return _Conn()


@pytest.mark.asyncio
async def test_themes_without_llm_write_nothing():
    pool = _WriteCountingPool(entities=[("CurlyOS", "Project", 90)])
    out = await N.surface_themes(pool, None, "user:usr_test", llm_client=None)
    assert out == [] and pool.writes == []


@pytest.mark.asyncio
async def test_themes_llm_failure_does_not_supersede_the_existing_set():
    pool = _WriteCountingPool(entities=[("CurlyOS", "Project", 90)])
    out = await N.surface_themes(pool, None, "user:usr_test", llm_client=_BrokenLLM())
    assert out == [] and pool.writes == []


class _GoodLLM:
    class chat:
        class completions:
            @staticmethod
            async def create(**kw):
                class M:
                    content = ('{"themes": [{"name": "Memory architecture", '
                               '"description": "Building the recall layer.", '
                               '"entities": ["CurlyOS", "Postgres"]}]}')

                class Choice:
                    message = M

                class R:
                    choices = [Choice]

                return R()


@pytest.mark.asyncio
async def test_themes_llm_success_supersedes_then_inserts():
    pool = _WriteCountingPool(entities=[("CurlyOS", "Project", 90)])
    out = await N.surface_themes(pool, None, "user:usr_test", llm_client=_GoodLLM())
    assert [t["name"] for t in out] == ["Memory architecture"]
    assert out[0]["frequency"] == 2
    kinds = [s.split()[0] for s, _ in pool.writes]
    assert kinds == ["UPDATE", "INSERT"]  # supersede before insert, never the reverse
