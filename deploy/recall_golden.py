#!/usr/bin/env python3
"""Golden recall evaluation against the live API.

Each query names one or more statement fragments (prefix or contains). The
fragments are resolved to active memory IDs at run time, so the dataset does
not rot when a record is superseded into a new version with a new id.

Usage:
    .venv/bin/python -I deploy/eval/recall_golden.py
    .venv/bin/python -I deploy/eval/recall_golden.py --bust-cache --json /tmp/eval.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402

from memory.retrieval.eval import aggregate, score_query  # noqa: E402

DEFAULT_DATASET = Path(__file__).with_name("recall_golden.json")
SCOPE = os.environ.get("CURLYOS_SCOPE", "user:usr_hiten")


def dsn() -> str:
    value = os.environ.get("CURLYOS_DATABASE_URL")
    if value:
        return value
    env_file = ROOT / ".env"
    if env_file.exists():
        from dotenv import dotenv_values

        value = dotenv_values(env_file).get("CURLYOS_DATABASE_URL")
    if not value:
        raise SystemExit("CURLYOS_DATABASE_URL is not set")
    return value


def _pattern(fragment: str, match: str) -> str:
    escaped = (
        fragment.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"{escaped}%" if match == "prefix" else f"%{escaped}%"


def resolve_expected(conn, fragments: list[str], match: str) -> set[str]:
    ids: set[str] = set()
    with conn.cursor() as cur:
        for fragment in fragments:
            cur.execute(
                "SELECT id FROM memories WHERE scope = %s AND valid_to IS NULL "
                "AND statement LIKE %s ESCAPE '\\'",
                (SCOPE, _pattern(fragment, match)),
            )
            ids.update(row[0] for row in cur.fetchall())
    return ids


def call(query: str, api: str, k: int, mode: str) -> tuple[list[str], bool]:
    body = json.dumps({"query": query, "scope": SCOPE, "mode": mode, "k": k}).encode()
    request = urllib.request.Request(
        api.rstrip("/") + "/api/recall", data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = json.load(response)
    if payload.get("error"):
        raise RuntimeError(payload["error"])
    return [item["id"] for item in payload.get("results", [])], bool(payload.get("cached"))


def redis_url() -> str:
    value = os.environ.get("CURLYOS_REDIS_URL")
    if value:
        return value
    env_file = ROOT / ".env"
    if env_file.exists():
        from dotenv import dotenv_values

        value = dotenv_values(env_file).get("CURLYOS_REDIS_URL")
    return value or ""


def bust_cache(scope: str) -> None:
    url = redis_url()
    if not url:
        return
    try:
        import redis

        client = redis.Redis.from_url(url)
        client.incr(f"recall:gen:{scope}")
        client.close()
    except Exception as exc:
        print(f"cache bust skipped: {exc}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:8643")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--k", type=int, default=8)
    parser.add_argument("--mode", default="deep", choices=("fast", "deep", "divergent"))
    parser.add_argument("--json", type=Path)
    parser.add_argument("--bust-cache", action="store_true")
    args = parser.parse_args()

    if args.bust_cache:
        bust_cache(SCOPE)

    dataset = json.loads(args.dataset.read_text())
    rows = []
    with psycopg.connect(dsn()) as conn:
        for entry in dataset:
            expected = resolve_expected(conn, entry["expect"], entry.get("match", "contains"))
            if not expected:
                print(f"UNRESOLVED {entry['query']!r} — no active memory matched")
                rows.append({"query": entry["query"], "unresolved": True})
                continue
            returned, cached = call(entry["query"], args.api, args.k, args.mode)
            score = score_query(expected, returned)
            score.update({
                "query": entry["query"], "expected": len(expected),
                "cached": cached, "note": entry.get("note", ""),
                "returned_ids": returned,
            })
            rows.append(score)

    print(f"{'query':<46}{'exp':>4}{'hit':>5}{'rr':>7}{'prec':>7}{'cache':>7}")
    for row in rows:
        if row.get("unresolved"):
            print(f"{row['query'][:44]:<46}{'!':>4}")
            continue
        print(
            f"{row['query'][:44]:<46}{row['expected']:>4}"
            f"{int(row['hit']):>5}{row['reciprocal_rank']:>7.2f}"
            f"{row['precision']:>7.2f}{str(row['cached']):>7}"
        )
    summary = aggregate(rows)
    print("-" * 76)
    print(
        f"queries={summary['queries']} hit_rate={summary['hit_rate']:.2f} "
        f"mrr={summary['mrr']:.3f} precision={summary['precision']:.2f}"
    )
    if args.json:
        args.json.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2))
        print(f"saved → {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
