"""Golden recall scoring."""

from __future__ import annotations

from memory.retrieval.eval import aggregate, score_query


def test_hit_and_reciprocal_rank():
    result = score_query({"a", "b"}, ["x", "a", "b"])
    assert result["hit"] is True
    assert result["reciprocal_rank"] == 0.5
    assert result["hits"] == 2
    assert result["precision"] == round(2 / 3, 4)


def test_miss():
    result = score_query({"a"}, ["x", "y"])
    assert result["hit"] is False
    assert result["reciprocal_rank"] == 0.0
    assert result["precision"] == 0.0


def test_aggregate_skips_unresolved():
    rows = [
        {"hit": True, "reciprocal_rank": 1.0, "precision": 0.5},
        {"hit": False, "reciprocal_rank": 0.0, "precision": 0.0},
        {"unresolved": True},
    ]
    summary = aggregate(rows)
    assert summary["queries"] == 2
    assert summary["hit_rate"] == 0.5
    assert summary["mrr"] == 0.5
    assert summary["precision"] == 0.25
