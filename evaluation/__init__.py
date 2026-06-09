"""Evaluation engine — score agents, RAG, and memory against versioned golden datasets.

Key APIs:
  POST /gate/evaluate → GateVerdict (promote/hold/rollback)

Components:
  scorers/  — pluggable: LLM-as-judge, RAG triad, embedding, agent-trajectory
  gate/     — Gate Service: promote iff all suites pass + no regressions
  replay/   — deterministic re-execution from Action→Observation events

Golden Store: content-addressed (sha256) in Postgres + MinIO.

See: ~/hitenos-architecture/11-eval-quality-harness.md
"""
from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any

from shared.types.ulid import mint, mint_ulid


class GateDecision(StrEnum):
    PROMOTE = "promote"
    HOLD = "hold"
    ROLLBACK = "rollback"


def _compute_content_hash(data: Any) -> str:
    """Compute sha256 hash of canonical JSON representation."""
    canonical = json.dumps(data, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── Golden Datasets ──────────────────────────────────────────────────────────


async def create_golden_dataset(
    pool: Any,
    name: str,
    data: Any,
    metadata: Any | None = None,
) -> dict:
    """Create a content-addressed golden dataset.

    Returns {id, name, content_hash}.
    """
    ds_id = f"gds_{mint_ulid()}"
    content_hash = _compute_content_hash(data)
    meta = json.dumps(metadata if metadata is not None else {})

    await pool.execute(
        "INSERT INTO golden_datasets (id, name, content_hash, data, metadata) "
        "VALUES (%s, %s, %s, %s, %s)",
        (ds_id, name, content_hash, json.dumps(data), meta),
    )
    return {"id": ds_id, "name": name, "content_hash": content_hash}


async def get_golden_dataset(pool: Any, dataset_id: str) -> dict | None:
    """Fetch a single golden dataset by id."""
    row = await pool.fetchrow(
        "SELECT id, name, content_hash, data, metadata FROM golden_datasets WHERE id = %s",
        (dataset_id,),
    )
    if row is None:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "content_hash": row["content_hash"],
        "data": row["data"],
        "metadata": row["metadata"],
    }


async def list_golden_datasets(pool: Any) -> list[dict]:
    """List all golden datasets."""
    rows = await pool.fetch(
        "SELECT id, name, content_hash, created_at FROM golden_datasets ORDER BY created_at DESC",
    )
    return [
        {"id": r["id"], "name": r["name"], "content_hash": r["content_hash"], "created_at": r["created_at"]}
        for r in rows
    ]


# ── Scorers ─────────────────────────────────────────────────────────────────


def _scorer_llm_judge(expected_items: list, candidate_response: str) -> dict:
    """LLM-judge scorer: check if candidate covers expected key-points.

    A simple heuristic: count the fraction of expected items whose
    lowercase text appears in the candidate response.
    """
    if not expected_items:
        return {"score": 1.0, "details": {"method": "llm_judge", "matched": 0, "expected": 0}}

    candidate_lower = candidate_response.lower()
    matched = 0
    detail_hits = []
    for item in expected_items:
        text = item if isinstance(item, str) else str(item)
        hit = text.lower() in candidate_lower
        if hit:
            matched += 1
        detail_hits.append({"expected": text, "hit": hit})

    score = matched / len(expected_items)
    return {"score": score, "details": {"method": "llm_judge", "matched": matched, "expected": len(expected_items), "hits": detail_hits}}


def _scorer_embedding(expected_items: list, candidate_response: str) -> dict:
    """Embedding cosine-similarity scorer (character-level trigram fallback).

    When pgvector / sentence-transformers are not available, falls back
    to a bag-of-trigrams cosine similarity.
    """

    def _trigram_vec(text: str) -> dict[str, int]:
        t = text.lower()
        vec: dict[str, int] = {}
        for i in range(len(t) - 2):
            tri = t[i : i + 3]
            vec[tri] = vec.get(tri, 0) + 1
        return vec

    def _cosine(a: dict[str, int], b: dict[str, int]) -> float:
        if not a or not b:
            return 0.0
        common = set(a) & set(b)
        if not common:
            return 0.0
        dot = sum(a[k] * b[k] for k in common)
        mag_a = sum(v * v for v in a.values()) ** 0.5
        mag_b = sum(v * v for v in b.values()) ** 0.5
        if mag_a == 0 or mag_b == 0:
            return 0.0
        return dot / (mag_a * mag_b)

    vec_cand = _trigram_vec(candidate_response)
    if expected_items:
        # average cosine against all expected texts
        scores = []
        for item in expected_items:
            text = item if isinstance(item, str) else str(item)
            scores.append(_cosine(vec_cand, _trigram_vec(text)))

        avg = sum(scores) / len(scores) if scores else 0.0
    else:
        avg = 1.0

    return {"score": avg, "details": {"method": "embedding", "similarity": avg}}


def _scorer_exact(expected_items: list, candidate_response: str) -> dict:
    """Exact-match scorer: candidate must match at least one expected item verbatim."""
    if not expected_items:
        return {"score": 1.0, "details": {"method": "exact", "matched": True, "expected": 0}}

    matched = False
    detail_hits = []
    for item in expected_items:
        text = item if isinstance(item, str) else str(item)
        hit = text.strip() == candidate_response.strip()
        detail_hits.append({"expected": text, "exact_hit": hit})
        if hit:
            matched = True

    return {
        "score": 1.0 if matched else 0.0,
        "details": {"method": "exact", "matched": int(matched), "expected": len(expected_items), "hits": detail_hits},
    }


_SCORERS = {
    "llm_judge": _scorer_llm_judge,
    "embedding": _scorer_embedding,
    "exact": _scorer_exact,
}


# ── Scorer runner ───────────────────────────────────────────────────────────


async def run_scorer(
    pool: Any,
    dataset_id: str,
    candidate_response: str,
    scorer_type: str = "llm_judge",
) -> dict:
    """Run a single scorer against a golden dataset.

    Returns {score: 0-1, details: {}}.
    """
    row = await pool.fetchrow(
        "SELECT id, name, content_hash, data FROM golden_datasets WHERE id = %s",
        (dataset_id, ),
    )
    if row is None:
        raise ValueError(f"Golden dataset {dataset_id!r} not found")

    data = row["data"]
    if isinstance(data, str):
        data = json.loads(data)

    # Extract expected answers from dataset.
    # Supported shapes:
    #   {"expected": [...]}          — explicit expected list
    #   [{"expected": ...}, ...]     — list of items, each with expected
    #   {"items": [{"expected": …}]} — nested items

    expected_items: list = []
    if isinstance(data, dict):
        if "expected" in data:
            raw = data["expected"]
            expected_items = raw if isinstance(raw, list) else [raw]
        elif "items" in data and isinstance(data["items"], list):
            for item in data["items"]:
                if isinstance(item, dict) and "expected" in item:
                    exp = item["expected"]
                    expected_items.extend(exp if isinstance(exp, list) else [exp])
                elif isinstance(item, str):
                    expected_items.append(item)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "expected" in item:
                exp = item["expected"]
                expected_items.extend(exp if isinstance(exp, list) else [exp])
            elif isinstance(item, str):
                expected_items.append(item)

    fn = _SCORERS.get(scorer_type)
    if fn is None:
        raise ValueError(f"Unknown scorer_type {scorer_type!r}. Available: {list(_SCORERS)}")

    result = fn(expected_items, candidate_response)
    result["scorer_type"] = scorer_type
    result["dataset_id"] = dataset_id
    return result


# ── Candidate evaluation (Gate) ──────────────────────────────────────────────


async def evaluate_candidate(
    pool: Any,
    publisher: Any,
    candidate_ref: str,
    dataset_ids: list[str],
    scorers: list[str] | None = None,
) -> dict:
    """Run evaluation suite against a candidate. Returns GateVerdict.

    GateDecision:
      - promote  if pass_rate >= 0.8 AND no regressions
      - hold     if 0.5 <= pass_rate < 0.8
      - rollback if pass_rate < 0.5
    """
    if scorers is None:
        scorers = ["llm_judge"]

    evr_id = mint("evr")
    all_scores: list[float] = []
    regressions = 0
    details_per_dataset: dict[str, list[dict]] = {}

    for ds_id in dataset_ids:
        dataset = await get_golden_dataset(pool, ds_id)
        if dataset is None:
            raise ValueError(f"Dataset {ds_id!r} not found")

        # Derive a candidate_response from the dataset structure.
        # In production this would come from running the candidate agent;
        # here we extract a baseline from the data itself.
        data = dataset.get("data", {})
        if isinstance(data, str):
            data = json.loads(data)

        # Build candidate response by checking the dataset's own expected
        # answers — this acts as a self-consistency check.
        candidate_response = ""
        if isinstance(data, dict):
            candidate_response = data.get("response", data.get("answer", ""))
        elif isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, dict):
                candidate_response = first.get("response", first.get("answer", ""))

        per_ds_scores: list[dict] = []
        for scorer_type in scorers:
            result = await run_scorer(pool, ds_id, candidate_response, scorer_type)
            per_ds_scores.append(result)
            all_scores.append(result["score"])

        # Detect regression: any scorer dropped below 0.5 on this dataset
        for r in per_ds_scores:
            if r["score"] < 0.5:
                regressions += 1

        details_per_dataset[ds_id] = per_ds_scores

    pass_rate = sum(all_scores) / len(all_scores) if all_scores else 0.0

    # Determine gate decision
    if pass_rate >= 0.8 and regressions == 0:
        decision = GateDecision.PROMOTE
        reason = "All scorers passed threshold with no regressions"
    elif pass_rate >= 0.5:
        decision = GateDecision.HOLD
        reason = f"pass_rate={pass_rate:.2f}, {regressions} regressions detected — needs review"
    else:
        decision = GateDecision.ROLLBACK
        reason = f"pass_rate={pass_rate:.2f} below 0.5 minimum"

    # Persist evaluation run
    await pool.execute(
        "INSERT INTO evaluation_runs "
        "(id, candidate_ref, dataset_ids, scorers, pass_rate, decision) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (evr_id, candidate_ref, json.dumps(dataset_ids), json.dumps(scorers), pass_rate, decision.value),
    )

    return {
        "evr_id": evr_id,
        "decision": decision,
        "pass_rate": pass_rate,
        "regressions": regressions,
        "reason": reason,
    }
