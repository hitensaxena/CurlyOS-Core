"""Shared distinctive-term extraction for cognitive engines."""

from __future__ import annotations

import re
from typing import Iterable

STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "about",
    "your", "their", "have", "has", "are", "was", "were", "will", "would",
    "should", "could", "been", "being", "they", "them", "there", "here",
    "what", "when", "where", "which", "while", "also", "more", "most",
    "some", "such", "only", "over", "under", "than", "then", "very",
}


def terms(values: Iterable[str], limit: int = 30) -> set[str]:
    """Distinctive words (4+ chars, no stopwords) from a set of strings."""
    found: set[str] = set()
    for value in values:
        for word in re.findall(r"[a-z0-9][a-z0-9_-]{3,}", (value or "").lower()):
            if word not in STOPWORDS:
                found.add(word)
                if len(found) >= limit:
                    return found
    return found


def overlap_score(query_terms: set[str] | Iterable[str], text: str, cap: int = 5) -> float:
    """0..1 fraction of query terms present in `text` (capped denominator).

    Used as a small lexical correction: terse stored facts ("design tool
    Figma") embed below longer passages that merely discuss the same topic,
    even when they contain the query words exactly.
    """
    query = set(query_terms)
    if not query:
        return 0.0
    text_terms = terms([text])
    if not text_terms:
        return 0.0
    return min(1.0, len(query & text_terms) / max(1, min(len(query), cap)))
