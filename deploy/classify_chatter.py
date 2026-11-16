#!/usr/bin/env python3
"""Ask a model which short memories are scaffolding rather than facts.

Regexes are exhausted: they catch probes and stubs, but "The repo is live and
pushed." is shaped exactly like a fact. This classifies a sample and reports;
it writes nothing to the database. Read the output before anything is flagged.

Candidates are short, unflagged memories outside the ChatGPT archive. The
archive is excluded because those rows are conversation summaries — long,
attributed, and mostly real content.

Usage (from the repo root):
    .venv/bin/python deploy/classify_chatter.py                 # 150-row sample
    .venv/bin/python deploy/classify_chatter.py --limit 400
    .venv/bin/python deploy/classify_chatter.py --all --out /tmp/full.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

MAX_LEN = 200
VERDICTS = ("fact", "chatter", "unsure")

PROMPT = """You are auditing one person's memory system. Each item is a stored \
statement that a retrieval engine may surface when the person asks a question.

Decide for each item:
  "fact"    — it says something true and useful about the person, their work,
              projects, tools, preferences, health, relationships or history.
              A short statement is still a fact if it is informative.
  "chatter" — it is scaffolding: a probe/test row, an assistant acknowledgement
              ("The repo is live and pushed."), a fragment of a reply, a status
              report about the system itself, or a lead-in with no content.
  "unsure"  — genuinely ambiguous, or you would need the surrounding text.

Judge meaning, not length or punctuation. "Hiten has ADHD." is a fact.
"The repo is live and pushed." is chatter.

Return ONLY a JSON array, one object per item, in the same order:
[{"id": "<id>", "verdict": "fact" | "chatter" | "unsure", "reason": "<8 words max>"}]

Items:
"""


def _make_client():
    """Fast-tier client, resolved the same way the API resolves it."""
    from openai import AsyncOpenAI
    from shared.models import FallbackClient, general_chain

    key = os.environ.get("CURLYOS_LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    base_url = os.environ.get("CURLYOS_LLM_BASE_URL") or "https://openrouter.ai/api/v1"
    chain = general_chain()
    if not key or not chain:
        return None, ""
    raw = AsyncOpenAI(base_url=base_url, api_key=key, timeout=120.0, max_retries=0)
    return FallbackClient(raw, chain, tier="fast"), os.environ.get("CURLYOS_LLM_MODEL") or chain[0]


async def _classify_batch(client, model: str, rows: list[tuple]) -> dict[str, dict]:
    from shared.llm import first_json

    items = [{"id": mid, "statement": statement} for mid, statement in rows]
    prompt = PROMPT + json.dumps(items, ensure_ascii=False)
    response = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.0,
    )
    data = first_json(response.choices[0].message.content, default=[])
    if isinstance(data, dict):  # some models wrap the array in an object
        for value in data.values():
            if isinstance(value, list):
                data = value
                break
    out: dict[str, dict] = {}
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id", ""))
        verdict = str(item.get("verdict", "")).lower().strip()
        if mid and verdict in VERDICTS:
            out[mid] = {"verdict": verdict, "reason": str(item.get("reason", ""))[:80]}
    return out


async def main() -> int:
    import psycopg.rows
    from psycopg_pool import AsyncConnectionPool

    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=150)
    parser.add_argument("--all", action="store_true", help="classify every candidate")
    parser.add_argument("--batch", type=int, default=25)
    parser.add_argument("--seed", default="2026-11-16")
    parser.add_argument("--scope", default=None)
    parser.add_argument("--out", default="/tmp/chatter_report.json")
    parser.add_argument(
        "--flagged", action="store_true",
        help="classify rows the regex already flagged. Use it to check agreement: "
             "a classifier that calls most of them facts is either wrong or the "
             "flag set is.")
    args = parser.parse_args()

    scope = args.scope or os.environ.get("CURLYOS_SCOPE") or "user:usr_hiten"
    dsn = os.environ.get("CURLYOS_DATABASE_URL")
    if not dsn:
        raise SystemExit("CURLYOS_DATABASE_URL is not set")

    client, model = _make_client()
    if client is None:
        raise SystemExit("no LLM key/model configured (CURLYOS_LLM_API_KEY + model chain)")

    pool = AsyncConnectionPool(dsn, min_size=1, max_size=2, open=False,
                               kwargs={"row_factory": psycopg.rows.tuple_row})
    await pool.open()
    try:
        limit_sql = "" if args.all else "LIMIT %s"
        params: list = [scope]
        if not args.all:
            params.append(args.limit)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                flag_clause = "is_chatter" if args.flagged else "NOT is_chatter"
                await cur.execute(
                    "SELECT id, statement FROM memories "
                    "WHERE scope = %s AND valid_to IS NULL AND " + flag_clause + " "
                    "  AND length(statement) <= %s "
                    "  AND statement NOT LIKE '%%archives/chatgpt/%%' "
                    "ORDER BY md5(id || %s) " + limit_sql,
                    [scope, MAX_LEN, args.seed] + params[1:],
                )
                rows = await cur.fetchall()
    finally:
        await pool.close()

    print(f"scope={scope} model={model} candidates={len(rows)} batch={args.batch}")
    verdicts: dict[str, dict] = {}
    for start in range(0, len(rows), args.batch):
        chunk = rows[start:start + args.batch]
        try:
            verdicts.update(await _classify_batch(client, model, chunk))
        except Exception as exc:
            print(f"  batch {start // args.batch}: {type(exc).__name__}: {exc}"[:200])
        done = min(start + args.batch, len(rows))
        print(f"  {done}/{len(rows)} classified", end="\r", flush=True)
    print()

    buckets: dict[str, list[dict]] = {v: [] for v in VERDICTS}
    missing = []
    for mid, statement in rows:
        verdict = verdicts.get(mid)
        if verdict is None:
            missing.append({"id": mid, "statement": statement[:160]})
            continue
        buckets[verdict["verdict"]].append(
            {"id": mid, "statement": statement[:160], "reason": verdict["reason"]})

    print("\nverdicts")
    for verdict in VERDICTS:
        print(f"  {verdict:<8} {len(buckets[verdict]):>5}")
    print(f"  {'unparsed':<8} {len(missing):>5}")

    for verdict in ("chatter", "unsure"):
        if not buckets[verdict]:
            continue
        print(f"\n{verdict} examples:")
        for row in buckets[verdict][:12]:
            print(f"  - {row['statement']!r}  ({row['reason']})")

    report = {
        "scope": scope, "model": model, "sample": len(rows),
        "counts": {v: len(buckets[v]) for v in VERDICTS} | {"unparsed": len(missing)},
        "buckets": buckets, "unparsed": missing,
    }
    Path(args.out).write_text(json.dumps(report, indent=1))
    print(f"\nreport written to {args.out} (nothing was written to the database)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
