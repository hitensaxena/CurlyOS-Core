#!/usr/bin/env python3
"""One-off cleanup: re-classify + prune the Concept/Other entity buckets.

The entity extractor dumps anything it can't confidently type into `Concept`
(catch-all idea) or `Other` (unclear), and never sets `epistemic_status` — so
the graph ended up with ~2k weakly-typed nodes, all `canonical`, mixing three
very different things:

  1. real entities mis-bucketed   -> RETYPE into the proper label
  2. operational noise / fragments -> PRUNE (soft-delete via valid_to)
  3. claim-like assertions          -> KEEP but mark belief/hypothesis

An LLM makes the call per entity (batched). Defaults to a DRY RUN that writes a
full audit JSON and changes nothing; pass --apply to mutate. Every change is
bi-temporal (valid_to / epistemic_status updates), and the audit file is the
reversal record.

Usage:
  python deploy/reclassify_entities.py                 # dry run, all Concept/Other
  python deploy/reclassify_entities.py --limit 100     # dry run on a sample
  python deploy/reclassify_entities.py --apply         # mutate the DB
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import psycopg
from psycopg.rows import dict_row

from knowledge.extraction import ENTITY_LABELS, normalize_label
from shared.epistemic import normalize_status, MEMORY_STATUSES
from shared.llm import json_records


def load_env() -> None:
    """Populate os.environ from core/.env for keys not already set."""
    env_path = os.path.join(ROOT, ".env")
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                if k and k not in os.environ:
                    os.environ[k] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass


SCOPE = os.environ.get("CURLYOS_SCOPE", "user:usr_hiten")
BATCH = 25

CLASSIFY_PROMPT = (
    "You are cleaning a personal knowledge graph belonging to Hiten. Each input is a node that "
    "was weakly auto-typed as 'Concept' or 'Other'. Decide what each node really is.\n\n"
    "For EACH node return an action:\n"
    "- \"prune\": NOT a real knowledge node. Operational/runtime noise (ports, PIDs, file paths, "
    "version strings, counts like '84 tagged conversations'), single filler words ('things', 'now', "
    "'manually', 'none'), or meaningless fragments. These get soft-deleted.\n"
    "- \"retype\": a genuine entity (a person, org, project, tool, skill, place, event, health item, "
    "media work, activity, or a real abstract concept). Give the correct `label` from the taxonomy.\n"
    "- \"claim\": NOT an entity but an assertion/idea/observation Hiten holds or is exploring "
    "(e.g. 'the wrong kind of question', 'two expressions of the same thing'). Keep it but mark it "
    "`belief` (a held worldview/value/opinion) or `hypothesis` (a tentative/speculative pattern).\n\n"
    f"Taxonomy for retype `label` (use EXACTLY one): {', '.join(ENTITY_LABELS)}.\n"
    "  Person, Organization, Project, Tool (software/app/library/device), Skill, Concept (genuine "
    "abstract idea/field/philosophy), Place, Event, Health (medical metric/condition/drug), "
    "Media (book/song/film/artist), Activity (hobby/practice/routine), Other.\n\n"
    "Input: JSON array of {\"id\",\"name\",\"label\"}. Classify EVERY id.\n"
    "Return JSON ONLY: {\"results\":[{\"id\":\"...\",\"action\":\"prune|retype|claim\","
    "\"label\":\"<taxonomy, retype only>\",\"epistemic\":\"belief|hypothesis (claim only)\"}]}\n\n"
    "Nodes:\n"
)


def make_llm():
    from openai import AsyncOpenAI
    from shared.models import FallbackClient, general_chain, primary_model

    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY not set (checked env + .env)")
    raw = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=key, timeout=90.0, max_retries=0)
    client = FallbackClient(raw, general_chain())
    model = os.environ.get("CURLYOS_LLM_MODEL") or primary_model()
    return client, model


async def classify_batch(llm, model, items: list[dict]) -> dict[str, dict]:
    """items: [{id,name,label}] -> {id: {action,label,epistemic}}."""
    resp = await llm.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You curate a knowledge graph. Return JSON only."},
            {"role": "user", "content": CLASSIFY_PROMPT + json.dumps(items, ensure_ascii=False)},
        ],
        temperature=0.0,
        max_tokens=3072,
    )
    out: dict[str, dict] = {}
    # A failover model can return a choice with null content — guard so one bad
    # response degrades to an empty batch instead of crashing the whole run.
    content = resp.choices[0].message.content if resp and resp.choices else None
    for r in json_records(content):
        rid = r.get("id")
        if not rid:
            continue
        out[str(rid)] = {
            "action": (r.get("action") or "").strip().lower(),
            "label": normalize_label(r.get("label")),
            "epistemic": normalize_status(r.get("epistemic"), allowed=("belief", "hypothesis"), default="hypothesis"),
        }
    return out


def fetch_targets(conn, limit: int | None, ids: list[str] | None = None) -> list[dict]:
    sql = (
        "SELECT id, name, label FROM knowledge_entities "
        "WHERE scope = %s AND valid_to IS NULL AND label IN ('Concept','Other') "
    )
    params: list = [SCOPE]
    if ids:
        sql += "AND id = ANY(%s) "
        params.append(ids)
    sql += "ORDER BY label, name"
    if limit:
        sql += " LIMIT %s"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def missing_ids_from_audit(path: str) -> list[str]:
    """The ids the LLM never returned a verdict for in a prior run."""
    with open(path) as f:
        data = json.load(f)
    return [r["id"] for r in data.get("plan", {}).get("missing", [])]


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="mutate the DB (default: dry run)")
    ap.add_argument("--limit", type=int, default=None, help="only process the first N targets")
    ap.add_argument("--from-audit", default=None, help="only process the 'missing' ids from a prior audit json")
    args = ap.parse_args()

    load_env()
    dsn = os.environ.get("CURLYOS_DATABASE_URL")
    if not dsn:
        raise SystemExit("CURLYOS_DATABASE_URL not set")

    ids = missing_ids_from_audit(args.from_audit) if args.from_audit else None
    if args.from_audit:
        print(f"Re-processing {len(ids)} 'missing' ids from {os.path.basename(args.from_audit)}", flush=True)

    conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
    targets = fetch_targets(conn, args.limit, ids)
    print(f"Targets (Concept/Other, active): {len(targets)}", flush=True)
    if not targets:
        return

    llm, model = make_llm()
    print(f"Model: {model}  |  mode: {'APPLY' if args.apply else 'DRY RUN'}", flush=True)

    decisions: dict[str, dict] = {}
    batches = list(chunked(targets, BATCH))
    for i, batch in enumerate(batches, 1):
        items = [{"id": t["id"], "name": t["name"], "label": t["label"]} for t in batch]
        try:
            res = await classify_batch(llm, model, items)
        except Exception as e:
            print(f"  batch {i}/{len(batches)} FAILED: {e}", flush=True)
            res = {}
        decisions.update(res)
        print(f"  batch {i}/{len(batches)}  classified {len(res)}/{len(batch)}", flush=True)

    # Build the change plan; anything the LLM skipped is left untouched.
    plan = {"prune": [], "retype": [], "claim": [], "noop": [], "missing": []}
    for t in targets:
        d = decisions.get(t["id"])
        if not d:
            plan["missing"].append({"id": t["id"], "name": t["name"], "label": t["label"]})
            continue
        action = d["action"]
        rec = {"id": t["id"], "name": t["name"], "from_label": t["label"]}
        if action == "prune":
            plan["prune"].append(rec)
        elif action == "retype":
            new_label = d["label"]
            if new_label != t["label"] and new_label != "Other":
                plan["retype"].append({**rec, "to_label": new_label})
            else:
                plan["noop"].append({**rec, "reason": "retype->same/Other"})
        elif action == "claim":
            plan["claim"].append({**rec, "epistemic": d["epistemic"]})
        else:
            plan["noop"].append({**rec, "reason": f"unknown action '{action}'"})

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit_path = os.path.join(HERE, f"reclassify_audit_{ts}.json")
    with open(audit_path, "w") as f:
        json.dump({"scope": SCOPE, "model": model, "applied": args.apply, "plan": plan}, f, indent=2, ensure_ascii=False)

    print("\n=== PLAN ===", flush=True)
    for k in ("prune", "retype", "claim", "noop", "missing"):
        print(f"  {k:8s}: {len(plan[k])}", flush=True)
    # label retype breakdown
    rt: dict[str, int] = {}
    for r in plan["retype"]:
        rt[r["to_label"]] = rt.get(r["to_label"], 0) + 1
    if rt:
        print("  retype ->", ", ".join(f"{k}:{v}" for k, v in sorted(rt.items(), key=lambda x: -x[1])), flush=True)
    print(f"  audit: {audit_path}", flush=True)

    if not args.apply:
        print("\nDRY RUN — no changes written. Re-run with --apply to mutate.", flush=True)
        return

    # ── Apply ────────────────────────────────────────────────────────────────
    now = datetime.now(timezone.utc)
    pruned = retyped = claimed = 0
    with conn.cursor() as cur:
        for r in plan["prune"]:
            cur.execute(
                "UPDATE knowledge_entities SET valid_to = %s WHERE id = %s AND valid_to IS NULL",
                (now, r["id"]),
            )
            # soft-invalidate edges touching a pruned node so the graph has no dangles
            cur.execute(
                "UPDATE knowledge_edges SET valid_to = %s "
                "WHERE (src_entity_id = %s OR dst_entity_id = %s) AND valid_to IS NULL",
                (now, r["id"], r["id"]),
            )
            pruned += 1
        for r in plan["retype"]:
            cur.execute(
                "UPDATE knowledge_entities SET label = %s WHERE id = %s AND valid_to IS NULL",
                (r["to_label"], r["id"]),
            )
            retyped += 1
        for r in plan["claim"]:
            cur.execute(
                "UPDATE knowledge_entities SET epistemic_status = %s WHERE id = %s AND valid_to IS NULL",
                (r["epistemic"], r["id"]),
            )
            claimed += 1
    print(f"\nAPPLIED — pruned:{pruned} retyped:{retyped} claim-marked:{claimed}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
