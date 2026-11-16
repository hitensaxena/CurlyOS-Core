"""Process-local retrieval caches."""

from __future__ import annotations

from memory import retrieval


def test_invalidate_caches_clears_both():
    retrieval._ALIAS_CACHE = (10**18, (["raw"], ["canon"]))
    retrieval._GRAPH_CACHE[("scope", ("seed",), 1, None, ("canonical",))] = (10**18, [{"id": "x"}])

    retrieval.invalidate_caches()

    assert retrieval._ALIAS_CACHE is None
    assert retrieval._GRAPH_CACHE == {}
