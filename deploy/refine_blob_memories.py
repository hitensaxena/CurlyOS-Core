"""Refine document-blob memories into atomic facts via hermes-bridge (Claude Max).

For each valid memory whose statement is a document dump (>= MIN_LEN chars — the
brain.db docs), Claude extracts atomic facts from the FULL source episode; each
becomes a proper atomic memory (embedding backfilled by a later pass / restart
sweep), and the blob is soft-invalidated. The full doc remains in its episode.

    set -a; . ./.env; set +a
    .venv/bin/python deploy/refine_blob_memories.py
"""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from psycopg_pool import AsyncConnectionPool
from shared.events.implementations import PgOnlyPublisher
from shared.llm import first_json
from memory.governance import add

logging.basicConfig(level=logging.WARNING)
DSN = os.environ["CURLYOS_DATABASE_URL"]
SCOPE = os.environ.get("CURLYOS_SCOPE", "user:usr_hiten")
BRIDGE_URL = os.environ.get("HERMES_BRIDGE_URL", "http://127.0.0.1:8787/v1")
MODEL = os.environ.get("REFINE_MODEL", "claude-sonnet-4-6")
MIN_LEN = 2000        # document-blob threshold
MAX_FACTS = 15
CONCURRENCY = 2       # hermes-bridge spawns a Claude subprocess per call
CONTENT_CAP = 9000

PROMPT = (
    "Extract atomic facts about Hiten from this personal-knowledge document.\n"
    'Return ONLY JSON: {"facts": ["<short standalone fact>", ...]}.\n'
    "Rules: each fact is one self-contained statement (use \"Hiten\", no pronouns); "
    "preserve names/dates/numbers; 5-15 meaningful, durable facts; "
    "skip filler, meta-commentary, and transcript noise.\n\nDocument:\n"
)


def log(m: str) -> None:
    print(m, flush=True)


def _bridge_key() -> str:
    if os.environ.get("BRIDGE_API_KEY"):
        return os.environ["BRIDGE_API_KEY"]
    p = os.path.expanduser("~/hermes-bridge/.env")
    with open(p) as f:
        for line in f:
            if line.startswith("BRIDGE_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("no BRIDGE_API_KEY")


def parse_facts(content: str | None) -> list[str]:
    p = first_json(content, default=None)
    seq = None
    if isinstance(p, dict):
        for v in p.values():
            if isinstance(v, list):
                seq = v
                break
    elif isinstance(p, list):
        seq = p
    out = []
    for x in seq or []:
        s = (x if isinstance(x, str) else str(x)).strip()
        if 10 <= len(s) <= 300:
            out.append(s)
    return out[:MAX_FACTS]


async def main() -> None:
    from openai import AsyncOpenAI
    llm = AsyncOpenAI(base_url=BRIDGE_URL, api_key=_bridge_key(), timeout=180.0, max_retries=1)
    pool = AsyncConnectionPool(DSN, min_size=2, max_size=4, open=False)
    await pool.open()
    pub = PgOnlyPublisher()

    async with pool.connection() as c:
        cur = await c.execute(
            "SELECT m.id, m.source_episode_id, e.content "
            "FROM memories m JOIN episodes e ON e.id = m.source_episode_id "
            "WHERE m.valid_to IS NULL AND char_length(m.statement) >= %s "
            "ORDER BY m.created_at",
            [MIN_LEN],
        )
        blobs = await cur.fetchall()
    log(f"blob memories to refine: {len(blobs)}")

    sem = asyncio.Semaphore(CONCURRENCY)
    lock = asyncio.Lock()
    counters = {"done": 0, "facts": 0, "refined": 0, "skipped": 0}
    total = len(blobs)

    async def tick():
        async with lock:
            counters["done"] += 1
            if counters["done"] % 10 == 0:
                log(f"  {counters['done']}/{total}; refined={counters['refined']} "
                    f"facts={counters['facts']} skipped={counters['skipped']}")

    async def process(mem_id, src_epi, content):
        facts = []
        async with sem:
            try:
                r = await llm.chat.completions.create(
                    model=MODEL,
                    messages=[{"role": "user", "content": PROMPT + (content or "")[:CONTENT_CAP]}],
                    max_tokens=1500, temperature=0,
                )
                facts = parse_facts(r.choices[0].message.content)
            except Exception as e:  # noqa: BLE001
                log(f"  extract failed mem={mem_id}: {e}")
        if not facts:
            async with lock:
                counters["skipped"] += 1  # leave the blob intact — never lose data
            await tick()
            return
        for f in facts:
            await add(pool, pub, SCOPE, statement=f, source_episode_id=src_epi,
                      kind="fact", epistemic_status="canonical")
        async with pool.connection() as c:
            await c.execute("UPDATE memories SET valid_to = now() WHERE id = %s", [mem_id])
        async with lock:
            counters["refined"] += 1
            counters["facts"] += len(facts)
        await tick()

    await asyncio.gather(*[process(m, s, c) for (m, s, c) in blobs])
    log(f"DONE: refined={counters['refined']}/{total} blobs into {counters['facts']} atomic facts; "
        f"skipped={counters['skipped']}")
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
