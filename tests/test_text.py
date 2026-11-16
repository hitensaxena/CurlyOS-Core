"""Distinctive-term extraction."""

from __future__ import annotations

from cognition.text import terms


def test_terms_filters_stopwords_and_short_words():
    assert terms(["the ocean and the sea"]) == {"ocean"}


def test_terms_deduplicates_and_limits():
    result = terms(["alpha beta gamma", "alpha delta"], limit=3)
    assert len(result) == 3
    assert "alpha" in result
