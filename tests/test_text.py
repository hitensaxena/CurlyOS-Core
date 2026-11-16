"""Distinctive-term extraction."""

from __future__ import annotations

from shared.text import terms


def test_terms_filters_stopwords_and_short_words():
    assert terms(["the ocean and the sea"]) == {"ocean"}


def test_terms_deduplicates_and_limits():
    result = terms(["alpha beta gamma", "alpha delta"], limit=3)
    assert len(result) == 3
    assert "alpha" in result


def test_overlap_score():
    from shared.text import overlap_score

    assert overlap_score({"design", "tool"}, "Hiten design tool Figma") == 1.0
    assert overlap_score({"design", "tool"}, "unrelated text") == 0.0
    assert overlap_score(set(), "anything") == 0.0
