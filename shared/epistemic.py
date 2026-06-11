"""Epistemic-status taxonomy + an LLM classifier for memory statements.

The full spectrum (assigned by source): seed (studio drafts) -> conjecture
-> hypothesis (reflection/tentative) -> canonical (established fact); plus
belief (subjective worldview/values) and possible_world (simulation).

For free-text memories we classify into the three that content alone can
distinguish: canonical / belief / hypothesis. The others come from their
specific producers (studio/simulation/reflection) and are not inferred here.
"""
from __future__ import annotations

from typing import Any

from shared.llm import json_records

ALL_STATUSES = ("seed", "conjecture", "hypothesis", "canonical", "belief", "possible_world")
MEMORY_STATUSES = ("canonical", "belief", "hypothesis")

CLASSIFY_PROMPT = (
    "Classify each statement about Hiten into exactly ONE epistemic type:\n"
    "- canonical: an established, objective fact — biographical detail, health metric, "
    "dated event, concrete preference, a tool/app he uses, a relationship, a place.\n"
    "- belief: a subjective worldview, value, philosophy, opinion, spiritual/metaphysical "
    "position, or self-perception Hiten HOLDS (e.g. 'Hiten holds/believes/argues that ...', "
    "views on consciousness/identity/reality, personal values).\n"
    "- hypothesis: a tentative, inferred, or speculative pattern/interpretation not yet "
    "established as fact.\n"
    'Input is a JSON array of {"id","statement"}. Classify EVERY id. '
    'Return JSON: {"results":[{"id":"...","type":"..."}]}.\n\nStatements:\n'
)


def normalize_status(raw: str | None, allowed=MEMORY_STATUSES, default: str = "canonical") -> str:
    s = (raw or "").strip().lower()
    return s if s in allowed else default


async def classify_statements(llm: Any, model: str, items: list[dict]) -> dict[str, str]:
    """items: [{"id","statement"}] -> {id: status}. Best-effort; robust JSON parse."""
    import json
    resp = await llm.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You classify statements by epistemic type. Return JSON only."},
            {"role": "user", "content": CLASSIFY_PROMPT + json.dumps(items, ensure_ascii=False)},
        ],
        temperature=0.0, max_tokens=2048,
    )
    out: dict[str, str] = {}
    for r in json_records(resp.choices[0].message.content):
        if r.get("id"):
            out[str(r["id"])] = normalize_status(r.get("type"))
    return out
