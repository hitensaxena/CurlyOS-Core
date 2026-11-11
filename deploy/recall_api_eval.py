#!/usr/bin/env python3
"""User-facing recall benchmark: hits the real /api/recall and scores what comes back.

recall_eval.py measures the pipeline in-process, which is the right tool for
tuning _graph_expand but not for judging output: /api/recall post-processes the
list (per-episode cap, archive-summary preference, chunk penalty) and that is
what the Hermes plugin and the terminal actually see.

Endpoint responses carry no episode id, so the metrics that need one are joined
back against the database afterwards.

    .venv/bin/python -I deploy/recall_api_eval.py --save before.json
    .venv/bin/python -I deploy/recall_api_eval.py --compare before.json
"""
from __future__ import annotations

import argparse
import collections
import json
import statistics
import time
import urllib.request

import psycopg

API = "http://127.0.0.1:8643/api/recall"
SCOPE = "user:usr_hiten"
K = 8

QUERIES = [
    "what am I building with curlyos",
    "which tracks have I released as crazymage",
    "ableton live setup and remote scripts",
    "express browser product design work",
    "my liver and health markers",
    "dehradun live set venues",
    "claude code delegation workflow",
    "3d audio visual project christmas",
    "ballam ai automation business",
    "figma design system work",
    "what models does hermes route to",
    "neti neti meditation",
]


def dsn() -> str:
    import os
    return os.environ["CURLYOS_DATABASE_URL"]


def call(query: str) -> tuple[list[dict], float]:
    body = json.dumps({"query": query, "scope": SCOPE, "mode": "deep", "k": K}).encode()
    req = urllib.request.Request(API, data=body, headers={"content-type": "application/json"})
    t0 = time.monotonic()
    payload = json.load(urllib.request.urlopen(req, timeout=120))
    return payload.get("results", []), (time.monotonic() - t0) * 1000


def enrich(rows: list[dict]) -> None:
    ids = [i["id"] for r in rows for i in r["items"]]
    if not ids:
        return
    with psycopg.connect(dsn()) as conn:
        meta = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT id, source_episode_id, is_chatter FROM memories WHERE id = ANY(%s)", (ids,)
        ).fetchall()}
    for r in rows:
        eps = collections.Counter()
        chat = 0
        for it in r["items"]:
            ep, is_chat = meta.get(it["id"], (None, False))
            eps[ep or "?"] += 1
            chat += 1 if is_chat else 0
        r["episodes"] = len(eps)
        r["max_ep"] = max(eps.values()) if eps else 0
        r["chatter"] = chat


def report(rows: list[dict], label: str) -> None:
    hdr = f"{'query':<34}{'n':>4}{'eps':>5}{'maxep':>6}{'chat':>5}{'ms':>6}"
    print(f"\n{label}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r['query'][:33]:<34}{len(r['items']):>4}{r['episodes']:>5}"
              f"{r['max_ep']:>6}{r['chatter']:>5}{r['ms']:>6.0f}")
    print("-" * len(hdr))
    print(f"{'mean':<34}{statistics.mean(len(r['items']) for r in rows):>4.1f}"
          f"{statistics.mean(r['episodes'] for r in rows):>5.1f}"
          f"{statistics.mean(r['max_ep'] for r in rows):>6.1f}"
          f"{statistics.mean(r['chatter'] for r in rows):>5.1f}"
          f"{statistics.mean(r['ms'] for r in rows):>6.0f}")


def compare(before: list[dict], after: list[dict]) -> None:
    print("\nper-query change (overlap = ids shared by both top-k)")
    print(f"{'query':<34}{'before':>8}{'after':>8}{'shared':>8}")
    print("-" * 58)
    for b, a in zip(before, after):
        bid = [i["id"] for i in b["items"]]
        aid = [i["id"] for i in a["items"]]
        shared = len(set(bid) & set(aid))
        print(f"{b['query'][:33]:<34}{len(bid):>8}{len(aid):>8}{shared:>8}")
    tot_b = sum(len(b["items"]) for b in before)
    tot_s = sum(len(set(i["id"] for i in b["items"]) & set(i["id"] for i in a["items"]))
                for b, a in zip(before, after))
    print(f"\ntop-{K} items retained: {tot_s}/{tot_b} ({tot_s/max(1,tot_b):.0%})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", default=None)
    ap.add_argument("--compare", default=None)
    args = ap.parse_args()

    rows = []
    for q in QUERIES:
        items, ms = call(q)
        rows.append({"query": q, "items": items, "ms": round(ms)})
    enrich(rows)
    report(rows, "live /api/recall")

    if args.save:
        with open(args.save, "w") as fh:
            json.dump(rows, fh, indent=1)
        print(f"saved → {args.save}")
    if args.compare:
        with open(args.compare) as fh:
            before = json.load(fh)
        compare(before, rows)


if __name__ == "__main__":
    main()
