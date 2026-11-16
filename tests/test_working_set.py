"""Salience scoring for working memory."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cognition.working_set import compute_salience

NOW = datetime(2026, 11, 16, tzinfo=timezone.utc)


def test_pinned_is_max():
    assert compute_salience("anything", NOW - timedelta(days=365), set(), set(), pinned=True, now=NOW) == 1.0


def test_recent_beats_old():
    recent = compute_salience("a note", NOW - timedelta(hours=1), set(), set(), now=NOW)
    old = compute_salience("a note", NOW - timedelta(days=90), set(), set(), now=NOW)
    assert recent > old


def test_goal_overlap_raises_salience():
    plain = compute_salience("a note about music", NOW, set(), set(), now=NOW)
    goal = compute_salience("ship the curlyos memory engine", NOW, {"curlyos", "memory", "engine"}, set(), now=NOW)
    assert goal > plain


def test_identity_overlap_raises_salience():
    base = compute_salience("a note", NOW, set(), set(), now=NOW)
    identity = compute_salience("hiten works as an artist", NOW, set(), {"hiten", "artist"}, now=NOW)
    assert identity > base


def test_attention_terms_raise_salience():
    plain = compute_salience("walk daily", NOW, set(), set(), now=NOW)
    attentive = compute_salience(
        "walk daily", NOW, set(), set(), attention_terms={"walk"}, now=NOW,
    )
    assert attentive > plain


def test_output_is_bounded():
    score = compute_salience(
        "one two three four five six", NOW, {"one", "two", "three"}, {"four", "five", "six"}, now=NOW,
    )
    assert 0.0 <= score <= 1.0
