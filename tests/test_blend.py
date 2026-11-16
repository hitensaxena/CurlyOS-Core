"""Tests for memory.retrieval.blend_rank_scores."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from memory.retrieval import blend_rank_scores

NOW = datetime(2026, 11, 16, tzinfo=timezone.utc)


def test_pure_cosine_ranks_by_cosine():
    scores = blend_rank_scores(
        {"a": 0.9, "b": 0.2},
        {"a": 0.1, "b": 0.1},
        {"a": NOW, "b": NOW},
        w_cosine=1.0,
        w_fused=0.0,
        w_recency=0.0,
        now=NOW,
    )
    assert scores["a"] > scores["b"]


def test_pure_recency_ranks_newer_higher():
    scores = blend_rank_scores(
        {"old": 0.5, "new": 0.5},
        {"old": 0.0, "new": 0.0},
        {"old": NOW - timedelta(days=180), "new": NOW},
        w_cosine=0.0,
        w_fused=0.0,
        w_recency=1.0,
        half_life_days=30.0,
        now=NOW,
    )
    assert scores["new"] > scores["old"]
    assert scores["new"] == 1.0


def test_fused_breaks_a_cosine_tie():
    scores = blend_rank_scores(
        {"a": 0.5, "b": 0.5},
        {"a": 0.9, "b": 0.1},
        {"a": NOW, "b": NOW},
        w_cosine=0.5,
        w_fused=0.5,
        w_recency=0.0,
        now=NOW,
    )
    assert scores["a"] > scores["b"]


def test_identical_signals_are_neutral():
    scores = blend_rank_scores(
        {"a": 0.4, "b": 0.4},
        {"a": 0.2, "b": 0.2},
        {"a": NOW, "b": NOW},
        now=NOW,
    )
    assert scores["a"] == scores["b"]


def test_weights_are_renormalised():
    # Weights sum to 2.0; the output must still be in [0, 1].
    scores = blend_rank_scores(
        {"a": 1.0, "b": 0.0},
        {"a": 0.0, "b": 1.0},
        {"a": NOW, "b": NOW},
        w_cosine=1.0,
        w_fused=1.0,
        w_recency=0.0,
        now=NOW,
    )
    assert all(0.0 <= v <= 1.0 for v in scores.values())


def test_missing_cosine_does_not_crash():
    scores = blend_rank_scores(
        {},
        {"a": 0.5},
        {"a": NOW},
        now=NOW,
    )
    assert "a" in scores


def test_empty_candidates():
    assert blend_rank_scores({}, {}, {}, now=NOW) == {}


def test_chatter_is_demoted_not_dropped():
    """A flagged row must lose to an equal unflagged one, and still be scored.

    "It's live." ranked first for "where do I live" on the word 'live' alone.
    Dropping flagged rows entirely would be wrong the other way: when chatter is
    the only match, returning it beats returning nothing.
    """
    scores = blend_rank_scores(
        {"clean": 0.9, "noise": 0.9},
        {"clean": 0.1, "noise": 0.1},
        {"clean": NOW, "noise": NOW},
        w_cosine=1.0, w_fused=0.0, w_recency=0.0,
        chatter={"noise"}, chatter_factor=0.5, now=NOW,
    )
    assert scores["clean"] > scores["noise"]
    assert scores["noise"] > 0


def test_chatter_factor_of_one_disables_the_penalty():
    scores = blend_rank_scores(
        {"clean": 0.9, "noise": 0.9},
        {"clean": 0.1, "noise": 0.1},
        {"clean": NOW, "noise": NOW},
        w_cosine=1.0, w_fused=0.0, w_recency=0.0,
        chatter={"noise"}, chatter_factor=1.0, now=NOW,
    )
    assert scores["clean"] == scores["noise"]
