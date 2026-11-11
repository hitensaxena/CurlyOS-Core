#!/usr/bin/env python3
"""Selective retirement of the 2026-06 ChatGPT chunk memories.

Context: that import wrote each conversation's text as ~2 KB memories of raw
excerpt (`# archives/chatgpt/…`), one per chunk. Phase 4 replaced them with one
distilled summary memory per conversation, but recall reads `memories` only, so
until the chunks are retired they still crowd every recall response.

The chunks are not all noise: some hold facts the summary omits (code, exact
numbers, step-by-step instructions). So retirement is by rule, calibrated
against an LLM judgement sample:

    --calibrate N   sample N chunks, ask the model whether the chunk's durable
                    content is already in its conversation summary, and report
                    how each candidate rule scores against those labels.
    --dry-run       print what the chosen rule would retire, write nothing.
    --apply         set valid_to on the selected chunks.

Nothing is deleted: the text stays in `episodes` (re-pointed in phase 4) and in
the markdown mirror at ~/mind/archives/chatgpt/. Retirement only removes it
from recall.

Usage (from the repo root):
    set -a; . ./.env; set +a
    .venv/bin/python -I deploy/retire_chatgpt_chunks.py --calibrate 150
    .venv/bin/python -I deploy/retire_chatgpt_chunks.py --dry-run
    .venv/bin/python -I deploy/retire_chatgpt_chunks.py --apply
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import import_chatgpt_export as base  # noqa: E402

log = logging.getLogger("retire-chunks")

# Assistant-side scaffolding: web-browsing transcripts, tool calls, CoT traces.
_NOISE_RE = re.compile(
    r"(_\[(?:thinking|browsing|tool)\]_|viewing lines \[|### tool|"
    r"file:///home/oai|\"cursor\"\s*:|Search results for query)"
)
_SUMMARY_RX = re.compile(r"^ChatGPT conversation ")


def noise_ratio(text: str) -> float:
    hits = _NOISE_RE.findall(text)
    if not hits:
        return 0.0
    # Markers cluster: each hit implies roughly a tool block to follow. Cap at 1.
    return min(1.0, len(hits) * 0.25)


def load_pairs(conn, limit: int | None = None) -> list[dict]:
    """Raw chunk + its conversation's summary, with similarity."""
    sql = """
      SELECT m.id, m.statement, s.statement AS summary,
             1 - (m.embedding <=> s.embedding) AS sim,
             m.source_episode_id
      FROM memories m
      JOIN memories s
        ON s.source_episode_id = m.source_episode_id
       AND s.statement LIKE 'ChatGPT conversation %'
       AND s.valid_to IS NULL
      WHERE m.statement LIKE '# archives/chatgpt/%'
        AND m.valid_to IS NULL
        AND m.embedding IS NOT NULL
    """
    rows = conn.execute(sql).fetchall()
    out = []
    for mem_id, text, summary, sim, epi in rows:
        out.append({
            "mem_id": mem_id, "text": text, "summary": summary,
            "sim": float(sim), "epi": epi, "len": len(text),
            "noise": noise_ratio(text),
        })
    if limit:
        random.seed(7)
        out = random.sample(out, min(limit, len(out)))
    return out


def add_within_conversation_ranks(rows: list[dict]) -> None:
    """Percentile of `sim` inside its own conversation — a global cut is
    useless because every chunk/summary pair scores 0.55-0.75."""
    by_epi: dict[str, list[dict]] = {}
    for r in rows:
        by_epi.setdefault(r["epi"], []).append(r)
    for group in by_epi.values():
        group.sort(key=lambda r: r["sim"])
        n = len(group)
        for i, r in enumerate(group):
            r["sim_pct"] = (i + 0.5) / n


JUDGE_PROMPT = """You are auditing a memory system. A long conversation was condensed
into the summary below, and one chunk of the raw transcript is shown.

Question: does the chunk contain durable facts that the summary does NOT already
state? Durable = decisions, preferences, named products/people/places, numbers,
dates, concrete instructions someone would want to recall later.

Answer with exactly one word on the first line:
COVERED   — everything durable in the chunk is already in the summary
UNIQUE    — the chunk holds durable facts the summary omits

Chunk:
{chunk}

Summary:
{summary}
"""


async def judge(llm, model: str, chunk: str, summary: str) -> str:
    resp = await llm.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": JUDGE_PROMPT.replace("{chunk}", chunk[:6000])
                   .replace("{summary}", summary[:3000])}],
        temperature=0.0,
        max_tokens=1200,
    )
    out = (resp.choices[0].message.content or "").strip().upper()
    return "COVERED" if out.startswith("COVERED") else ("UNIQUE" if "UNIQUE" in out else "?")


RULES = {
    "noise": lambda r: r["noise"] >= 0.25,
    "top-half-sim": lambda r: r["sim_pct"] >= 0.5,
    "top-quartile-sim": lambda r: r["sim_pct"] >= 0.75,
    "top-third-sim": lambda r: r["sim_pct"] >= 0.667,
    "sim>=0.70": lambda r: r["sim"] >= 0.70,
    "noise OR top-half-sim": lambda r: r["noise"] >= 0.25 or r["sim_pct"] >= 0.5,
    "noise OR top-third-sim": lambda r: r["noise"] >= 0.25 or r["sim_pct"] >= 0.667,
}


def score_rules(sample: list[dict]) -> list[tuple[str, int, int, float]]:
    """(rule, retired, wrong_retired, precision_of_keep) over the judged sample."""
    out = []
    for name, fn in RULES.items():
        retired = [r for r in sample if fn(r)]
        wrong = [r for r in retired if r.get("label") == "UNIQUE"]
        out.append((name, len(retired), len(wrong),
                    (len(retired) - len(wrong)) / len(retired) if retired else 0.0))
    return out


async def calibrate(args: argparse.Namespace) -> None:
    import psycopg

    base.load_env()
    conn = psycopg.connect(base.dsn())
    rows = load_pairs(conn)
    add_within_conversation_ranks(rows)
    sample = random.Random(11).sample(rows, min(args.calibrate, len(rows)))
    llm = base.make_llm()
    model = os.environ.get("CURLYOS_LLM_MODEL", "claude-haiku-5-5")
    sem = asyncio.Semaphore(4)
    done = 0

    async def one(r: dict) -> None:
        nonlocal done
        async with sem:
            try:
                r["label"] = await judge(llm, model, r["text"], r["summary"])
            except Exception as exc:  # noqa: BLE001
                log.warning("judge failed %s: %s", r["mem_id"], exc)
                r["label"] = "?"
            done += 1
            if done % 25 == 0:
                log.info("judged %d/%d", done, len(sample))

    await asyncio.gather(*(one(r) for r in sample))
    labelled = [r for r in sample if r.get("label") in ("COVERED", "UNIQUE")]
    covered = sum(1 for r in labelled if r["label"] == "COVERED")
    print()
    print(f"judged          : {len(labelled)} (of {len(sample)} sampled)")
    print(f"  COVERED       : {covered} ({covered/max(1,len(labelled)):.0%})")
    print(f"  UNIQUE        : {len(labelled)-covered}")
    print()
    print(f"{'rule':<26} {'retired':>8} {'wrong':>6} {'precision':>10}")
    for name, retired, wrong, prec in sorted(score_rules(labelled), key=lambda x: -x[3]):
        print(f"{name:<26} {retired:>8} {wrong:>6} {prec:>9.0%}")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            [{k: r[k] for k in ("mem_id", "sim", "sim_pct", "noise", "len", "label")}
             for r in sample], indent=1))
        print(f"\nlabels written to {args.json_out}")


def apply(args: argparse.Namespace) -> None:
    import psycopg

    base.load_env()
    conn = psycopg.connect(base.dsn())
    rows = load_pairs(conn)
    add_within_conversation_ranks(rows)
    rule = RULES[args.rule]
    retire = [r["mem_id"] for r in rows if rule(r)]
    print(f"\nrule: {args.rule}")
    print(f"raw chunks in scope : {len(rows)}")
    print(f"would retire        : {len(retire)}")
    print(f"would keep          : {len(rows) - len(retire)}")
    for r in sorted(rows, key=lambda r: -r["sim"])[:0]:
        pass
    if args.dry_run or not args.approve:
        print("\n[dry-run] nothing written — pass --approve with --apply to write")
        for r in [x for x in rows if rule(x)][:3]:
            print(f"  retire  sim={r['sim']:.3f} pct={r['sim_pct']:.2f} "
                  f"noise={r['noise']:.2f} | {r['text'][:90]!r}")
        for r in [x for x in rows if not rule(x)][:3]:
            print(f"  keep    sim={r['sim']:.3f} pct={r['sim_pct']:.2f} "
                  f"noise={r['noise']:.2f} | {r['text'][:90]!r}")
        return
    before = conn.execute("SELECT count(*) FROM memories WHERE valid_to IS NULL").fetchone()[0]
    with conn:
        conn.execute(
            "UPDATE memories SET valid_to = now() WHERE id = ANY(%s) AND valid_to IS NULL",
            (retire,),
        )
    after = conn.execute("SELECT count(*) FROM memories WHERE valid_to IS NULL").fetchone()[0]
    print(f"\nretired {len(retire)} chunk memories; valid memories {before} → {after}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Selectively retire ChatGPT chunk memories")
    ap.add_argument("--calibrate", type=int, default=0, metavar="N")
    ap.add_argument("--rule", default="noise OR top-third-sim", choices=sorted(RULES))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--approve", action="store_true", help="required for --apply to write")
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.calibrate:
        asyncio.run(calibrate(args))
    elif args.apply:
        apply(args)
    else:
        ap.error("pass --calibrate N or --apply")


if __name__ == "__main__":
    main()
