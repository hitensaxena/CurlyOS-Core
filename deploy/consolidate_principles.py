"""Consolidate redundant principles into a canonical set via hermes-bridge (Claude).

The distill job produced ~46 valid principles that are mostly rephrasings of
~10-15 ideas. One Claude call merges near-duplicates into crisp, well-domained
statements; the old ones are superseded (valid_to) and the canonical set inserted.

    set -a; . ./.env; set +a
    .venv/bin/python deploy/consolidate_principles.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg
from shared.llm import first_json
from shared.types.ulid import mint

DSN = os.environ["CURLYOS_DATABASE_URL"]
SCOPE = os.environ.get("CURLYOS_SCOPE", "user:usr_hiten")
BRIDGE_URL = os.environ.get("HERMES_BRIDGE_URL", "http://127.0.0.1:8787/v1")
MODEL = os.environ.get("REFINE_MODEL", "claude-sonnet-4-6")
DOMAINS = ["work", "tooling", "general", "health", "creative", "personal"]

PROMPT = (
    "Below are personal principles distilled for Hiten; many are rephrasings of the "
    "same underlying idea. Consolidate them into a DEDUPLICATED canonical set: one "
    "crisp, well-phrased principle per distinct idea, merging near-duplicates. Keep "
    "only genuinely distinct, meaningful principles (aim for ~10-15). Assign each the "
    f"most fitting domain from {DOMAINS}. Preserve specificity; drop vague filler.\n"
    'Return ONLY JSON: {"principles":[{"statement":"...","domain":"..."}]}.\n\nPrinciples:\n'
)


def log(m): print(m, flush=True)


def _bridge_key():
    if os.environ.get("BRIDGE_API_KEY"):
        return os.environ["BRIDGE_API_KEY"]
    with open(os.path.expanduser("~/hermes-bridge/.env")) as f:
        for line in f:
            if line.startswith("BRIDGE_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("no BRIDGE_API_KEY")


async def main():
    from openai import AsyncOpenAI
    llm = AsyncOpenAI(base_url=BRIDGE_URL, api_key=_bridge_key(), timeout=180.0, max_retries=1)
    conn = psycopg.connect(DSN, autocommit=False)
    rows = conn.execute(
        "SELECT statement, domain FROM principles WHERE valid_to IS NULL AND scope = %s "
        "ORDER BY domain, statement", [SCOPE]
    ).fetchall()
    log(f"current valid principles: {len(rows)}")
    items = [{"statement": r[0], "domain": r[1]} for r in rows]

    resp = await llm.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": PROMPT + json.dumps(items, ensure_ascii=False)}],
        max_tokens=2500, temperature=0,
    )
    data = first_json(resp.choices[0].message.content, default={})
    canon = data.get("principles") if isinstance(data, dict) else (data if isinstance(data, list) else None)
    canon = [p for p in (canon or []) if isinstance(p, dict) and (p.get("statement") or "").strip()]
    if not canon or len(canon) > len(rows):
        log(f"ABORT: bad consolidation result ({len(canon) if canon else 0} principles); leaving table unchanged")
        conn.close()
        return
    log(f"consolidated -> {len(canon)} canonical principles")

    # Replace: supersede current, insert canonical (one tx).
    conn.execute("UPDATE principles SET valid_to = now() WHERE valid_to IS NULL AND scope = %s", [SCOPE])
    for p in canon:
        dom = p.get("domain") if p.get("domain") in DOMAINS else "general"
        conn.execute(
            "INSERT INTO principles (id, scope, statement, domain, epistemic_status) "
            "VALUES (%s, %s, %s, %s, 'canonical')",
            [mint("prn"), SCOPE, p["statement"].strip()[:500], dom],
        )
    conn.commit()
    conn.close()
    log(f"DONE: {len(rows)} -> {len(canon)} principles")
    for p in canon:
        log(f"  [{p.get('domain')}] {p['statement'][:90]}")


if __name__ == "__main__":
    asyncio.run(main())
