"""Re-classify existing memories into the epistemic spectrum (canonical/belief/
hypothesis) via the configured model chain. The bulk store was flat 'canonical';
this assigns belief to worldview/values/opinions and hypothesis to inferred
patterns. Idempotent-ish (re-runs re-classify).

    set -a; . ./.env; set +a
    .venv/bin/python deploy/reclassify_epistemic.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg
from shared.models import FallbackClient, general_chain, primary_model
from shared.epistemic import classify_statements

DSN = os.environ["CURLYOS_DATABASE_URL"]
SCOPE = os.environ.get("CURLYOS_SCOPE", "user:usr_hiten")
BATCH = 40
CONCURRENCY = 4


def log(m): print(m, flush=True)


async def main():
    from openai import AsyncOpenAI
    key = os.environ["OPENROUTER_API_KEY"]
    raw = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=key, timeout=120.0, max_retries=0)
    llm = FallbackClient(raw, general_chain())
    model = primary_model()

    conn = psycopg.connect(DSN, autocommit=True)
    rows = conn.execute(
        "SELECT id, statement FROM memories WHERE valid_to IS NULL AND scope = %s ORDER BY created_at",
        [SCOPE],
    ).fetchall()
    log(f"memories to classify: {len(rows)} (model={model})")

    sem = asyncio.Semaphore(CONCURRENCY)
    lock = asyncio.Lock()
    counters = {"done": 0, "changed": 0}
    dist: dict[str, int] = {}
    total = len(rows)

    async def process(idx, batch):
        items = [{"id": r[0], "statement": (r[1] or "")[:300]} for r in batch]
        labels = {}
        async with sem:
            for attempt in range(2):
                try:
                    labels = await classify_statements(llm, model, items)
                    break
                except Exception as e:  # noqa: BLE001
                    log(f"  batch {idx} attempt {attempt+1} failed: {e}")
        async with lock:
            for rid, _ in batch:
                status = labels.get(rid, "canonical")
                conn.execute(
                    "UPDATE memories SET epistemic_status = %s WHERE id = %s AND valid_to IS NULL",
                    [status, rid],
                )
                dist[status] = dist.get(status, 0) + 1
                if status != "canonical":
                    counters["changed"] += 1
            counters["done"] += len(batch)
            if counters["done"] % 200 == 0 or counters["done"] == total:
                log(f"  {counters['done']}/{total}")

    batches = [(i // BATCH, rows[i:i + BATCH]) for i in range(0, total, BATCH)]
    await asyncio.gather(*[process(idx, b) for idx, b in batches])
    conn.close()
    log(f"DONE: {total} classified, {counters['changed']} non-canonical")
    log("distribution: " + ", ".join(f"{k}={v}" for k, v in sorted(dist.items(), key=lambda x: -x[1])))


if __name__ == "__main__":
    asyncio.run(main())
