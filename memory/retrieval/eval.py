"""Scoring helpers for the golden recall evaluation.

Kept separate from the harness so the metrics are unit-testable and the harness
stays a thin CLI. A query's expected set is resolved to active memory IDs at
run time; these functions compare that set with the returned list.
"""

from __future__ import annotations


def score_query(expected_ids: set[str], returned_ids: list[str]) -> dict:
    """Hit@k, reciprocal rank of the first hit, and precision over returned."""
    hits = [
        rank for rank, memory_id in enumerate(returned_ids, 1)
        if memory_id in expected_ids
    ]
    hit = bool(hits)
    reciprocal_rank = 1.0 / hits[0] if hits else 0.0
    precision = (
        len(hits) / len(returned_ids) if returned_ids else 0.0
    )
    return {
        "hit": hit,
        "reciprocal_rank": round(reciprocal_rank, 4),
        "precision": round(precision, 4),
        "hits": len(hits),
        "returned": len(returned_ids),
    }


def aggregate(rows: list[dict]) -> dict:
    """Macro averages over queries that resolved an expected set."""
    scored = [row for row in rows if not row.get("unresolved")]
    if not scored:
        return {"queries": 0, "hit_rate": 0.0, "mrr": 0.0, "precision": 0.0}
    return {
        "queries": len(scored),
        "hit_rate": round(sum(1 for r in scored if r["hit"]) / len(scored), 4),
        "mrr": round(sum(r["reciprocal_rank"] for r in scored) / len(scored), 4),
        "precision": round(sum(r["precision"] for r in scored) / len(scored), 4),
    }
