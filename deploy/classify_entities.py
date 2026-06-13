"""One-off backfill: classify existing knowledge_entities into the type taxonomy
(Person/Project/Tool/Skill/Concept/...) via LLM, using each entity's relationship
types as context hints. Sets knowledge_entities.label. Idempotent-ish: re-running
re-classifies; pass ONLY_UNTYPED=1 to touch only label='Entity'/'Other'.

    set -a; . ./.env; set +a
    .venv/bin/python deploy/classify_entities.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg
from knowledge.extraction import ENTITY_LABELS, normalize_label
from shared.llm import json_records

DSN = os.environ["CURLYOS_DATABASE_URL"]
SCOPE = os.environ.get("CURLYOS_SCOPE", "user:usr_hiten")
# CLASSIFY_MODEL overrides the configured model (e.g. a reliable paid model for a
# bulk one-off, while the live pipeline keeps CURLYOS_LLM_MODEL).
MODEL = os.environ.get("CLASSIFY_MODEL") or os.environ.get("CURLYOS_LLM_MODEL", "openai/gpt-4o-mini")
ONLY_UNTYPED = os.environ.get("ONLY_UNTYPED") == "1"
# Small batches by default: a :free model truncates large JSON (json_records
# salvages, but small asks rarely truncate). Bump CLASSIFY_BATCH/CONCURRENCY for
# a reliable model.
BATCH = int(os.environ.get("CLASSIFY_BATCH", "12"))
CONCURRENCY = int(os.environ.get("CLASSIFY_CONCURRENCY", "4"))

PROMPT = """Classify each entity into exactly ONE type from this set:
Person, Organization, Project, Tool, Skill, Concept, Place, Event, Health, Media, Activity, Other

  Person: a named individual         Organization: company / school / team
  Project: a named project/product   Tool: software / app / library / device / tech
  Skill: an ability or competency    Concept: abstract idea / field / emotion / philosophy
  Place: a location                  Event: a dated happening
  Health: a medical metric/condition Media: a book / song / film / album / artist
  Activity: a hobby / practice       Other: none of the above / unclear

Each input item is {"id","name","rels"} where rels are relationship types touching
the entity (strong hints, e.g. "hemoglobin"→Health, "design_tool"→Tool, "occupation"→Concept).
Classify EVERY id. Return JSON: {"results":[{"id":"...","type":"..."}]}.

Entities:
"""


def log(m): print(m, flush=True)


async def make_llm():
    # Default OpenRouter; override LLM_BASE_URL + LLM_API_KEY to use the Claude Max
    # hermes-bridge (base http://localhost:8787/v1, key BRIDGE_API_KEY).
    from openai import AsyncOpenAI
    base = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
    key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY", "")
    return AsyncOpenAI(base_url=base, api_key=key, timeout=90.0, max_retries=2)


async def classify(llm, batch):
    items = [{"id": r[0], "name": r[1], "rels": (r[2] or [])[:8]} for r in batch]
    resp = await llm.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": "You classify entities. Return JSON only."},
            {"role": "user", "content": PROMPT + json.dumps(items, ensure_ascii=False)},
        ],
        response_format={"type": "json_object"},
        temperature=0.0, max_tokens=2048,
    )
    out = {}
    for r in json_records(resp.choices[0].message.content):
        if r.get("id"):
            out[r["id"]] = normalize_label(r.get("type"))
    return out


async def main():
    conn = psycopg.connect(DSN, autocommit=True)
    where = "e.scope = %s AND e.valid_to IS NULL"
    if ONLY_UNTYPED:
        where += " AND e.label IN ('Entity', 'Other')"
    rows = conn.execute(
        f"SELECT e.id, e.name, array_agg(DISTINCT k.rel_type) "
        f"FROM knowledge_entities e "
        f"LEFT JOIN knowledge_edges k ON (k.src_entity_id = e.id OR k.dst_entity_id = e.id) "
        f"  AND k.valid_to IS NULL "
        f"WHERE {where} GROUP BY e.id, e.name ORDER BY e.created_at",
        [SCOPE],
    ).fetchall()
    log(f"entities to classify: {len(rows)}")

    llm = await make_llm()
    dist: dict[str, int] = {}
    counters = {"updated": 0, "missing": 0, "done": 0}
    total = len(rows)
    sem = asyncio.Semaphore(CONCURRENCY)
    lock = asyncio.Lock()

    async def process(idx, batch):
        async with sem:
            labels = {}
            for attempt in range(3):
                try:
                    labels = await classify(llm, batch)
                    break
                except Exception as e:  # noqa: BLE001
                    log(f"  batch {idx} attempt {attempt+1} failed: {e}")
                    await asyncio.sleep(2 * (attempt + 1))
        async with lock:
            for eid, name, _ in batch:
                label = labels.get(eid)
                if label is None:
                    counters["missing"] += 1
                    label = "Other"
                conn.execute("UPDATE knowledge_entities SET label = %s WHERE id = %s", [label, eid])
                dist[label] = dist.get(label, 0) + 1
                counters["updated"] += 1
            counters["done"] += len(batch)
            log(f"  {counters['done']}/{total} classified")

    batches = [(idx, rows[i:i + BATCH]) for idx, i in enumerate(range(0, total, BATCH))]
    await asyncio.gather(*[process(idx, b) for idx, b in batches])
    log(f"unresolved (defaulted to Other): {counters['missing']}")
    updated = counters["updated"]
    conn.close()
    log(f"DONE: {updated} entities labelled")
    log("distribution: " + ", ".join(f"{k}={v}" for k, v in sorted(dist.items(), key=lambda x: -x[1])))


if __name__ == "__main__":
    asyncio.run(main())
