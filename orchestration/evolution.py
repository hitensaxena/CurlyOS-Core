"""Self-evolution v1 — the Executive's planner prompt becomes evolvable
(curlyos-final Phase E).

The loop:  propose → eval gate → approval gate → activate.

  * propose_prompt        — insert a candidate version + event.
  * evaluate_prompt       — run the candidate against the golden planner
                            dataset with DETERMINISTIC constraint scoring
                            (valid JSON plan, known tools, step cap, per-task
                            expectations). Records an evaluation_runs row.
                            pass_rate < threshold OR worse than the active
                            version → status 'held' (the gate refusing a
                            regression — the spec's exit criterion).
  * activate_prompt       — BOTH gates checked through the real PDP
                            (action_class=self_modify, eval_verdict +
                            approval_state → must yield ALLOW). Retires the
                            previous active version. Emits the event.
  * get_active_prompt     — runtime lookup with hardcoded-default fallback;
                            the graph's plan node calls this.

v1 evolves ONE prompt (executive.plan) end-to-end; the machinery is generic
(name-keyed), so adding prompts later is registration, not design.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Awaitable, Callable

log = logging.getLogger("curlyos-core.orchestration.evolution")

LLMFn = Callable[[str, str], Awaitable[str]]

PASS_THRESHOLD = 0.8
GOLDEN_DATASET_NAME = "executive-planner-v1"

# Golden tasks with deterministic expectations. `no_writes` = read tools only;
# `includes_tool` = the plan must contain it; `first_tool` = step 0 check.
GOLDEN_PLANNER_TASKS: list[dict] = [
    {"task": "What do I know about Mintrix?",
     "expect": {"first_tool": "recall", "no_writes": True}},
    {"task": "List my goals and how they're going.",
     "expect": {"includes_tool": "list_goals", "no_writes": True}},
    {"task": "Remember that I prefer working in the early morning.",
     "expect": {"includes_tool": "remember"}},
    {"task": "Create a goal to learn the piano this year.",
     "expect": {"includes_tool": "create_goal"}},
    {"task": "Record my decision to keep Postgres as the only database; it's costly to reverse; review in a month.",
     "expect": {"includes_tool": "record_decision"}},
    {"task": "Send me a short summary of what I worked on this week.",
     "expect": {"includes_tool": "notify"}},
]


def _extract_plan(text: str) -> list | None:
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return None
    try:
        out = json.loads(m.group(0))
        return out if isinstance(out, list) else None
    except json.JSONDecodeError:
        return None


def score_plan(plan_text: str, expect: dict) -> tuple[bool, str]:
    """Deterministic constraint scoring of one planner output."""
    from orchestration.graph import MAX_STEPS
    from orchestration.tools import REGISTRY

    plan = _extract_plan(plan_text)
    if plan is None:
        return False, "no parseable JSON array"
    if not plan or len(plan) > MAX_STEPS:
        return False, f"bad step count {len(plan)}"
    tools = []
    for step in plan:
        if not isinstance(step, dict) or str(step.get("tool", "")) not in REGISTRY:
            return False, f"unknown/malformed step: {step!r:.80}"
        tools.append(str(step["tool"]))
    write_classes = {"memory_write", "external_post"}
    if expect.get("no_writes") and any(REGISTRY[t].action_class in write_classes for t in tools):
        return False, f"writes in a read-only task: {tools}"
    if expect.get("first_tool") and tools[0] != expect["first_tool"]:
        return False, f"first tool {tools[0]!r} != {expect['first_tool']!r}"
    if expect.get("includes_tool") and expect["includes_tool"] not in tools:
        return False, f"missing required tool {expect['includes_tool']!r}: {tools}"
    return True, "ok"


# ── golden dataset bootstrap ──────────────────────────────────────────────────

async def ensure_golden_dataset(pool: Any) -> str:
    """Create the planner golden dataset if absent; returns its id."""
    from evaluation import create_golden_dataset, list_golden_datasets

    for ds in await list_golden_datasets(pool):
        if ds["name"] == GOLDEN_DATASET_NAME:
            return ds["id"]
    ds = await create_golden_dataset(
        pool, name=GOLDEN_DATASET_NAME, data=GOLDEN_PLANNER_TASKS,
        metadata={"scorer": "planner-constraints", "phase": "E"},
    )
    return ds["id"] if isinstance(ds, dict) else ds


# ── the loop ──────────────────────────────────────────────────────────────────

async def propose_prompt(pool: Any, publisher: Any, scope: str, *,
                         name: str, content: str, notes: str = "",
                         proposed_by: str = "manual") -> dict:
    from agent.pdp_gate import scope_parts
    from shared.events import build_event
    from shared.types.ulid import mint

    pmt_id = mint("pmt")
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT COALESCE(max(version), 0) + 1 FROM prompt_versions WHERE name = %s",
                (name,),
            )
            version = (await cur.fetchone())[0]
            await cur.execute(
                "INSERT INTO prompt_versions (id, scope, name, version, content, "
                "proposed_by, notes) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (pmt_id, scope, name, version, content, proposed_by, notes[:1000]),
            )
        ev = build_event(
            short_type="evolution.candidate.proposed", subject=pmt_id,
            scope=scope_parts(scope),
            data={"pmt_id": pmt_id, "name": name, "version": version,
                  "proposed_by": proposed_by, "notes": notes[:300]},
            actor="system", source="curlyos-core/evolution",
        )
        await publisher.stage(ev, conn)
    return {"id": pmt_id, "name": name, "version": version, "status": "candidate"}


async def _active_pass_rate(pool: Any, name: str) -> float | None:
    """The current active version's recorded pass_rate (regression baseline)."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT e.pass_rate FROM prompt_versions p "
                "JOIN evaluation_runs e ON e.id = p.eval_run_id "
                "WHERE p.name = %s AND p.status = 'active' LIMIT 1",
                (name,),
            )
            row = await cur.fetchone()
    return float(row[0]) if row else None


async def evaluate_prompt(pool: Any, publisher: Any, scope: str, *,
                          pmt_id: str, llm: LLMFn | None) -> dict:
    """Gate 1. Runs the candidate against the golden tasks; held on failure."""
    if llm is None:
        return {"error": "evaluation requires an LLM"}

    from agent.pdp_gate import scope_parts
    from psycopg.types.json import Jsonb
    from shared.events import build_event
    from shared.types.ulid import mint

    from orchestration.tools import planner_tool_block
    from orchestration.graph import MAX_STEPS, _PLAN_USER

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT name, content, status FROM prompt_versions WHERE id = %s AND scope = %s",
                (pmt_id, scope),
            )
            row = await cur.fetchone()
    if row is None:
        return {"error": f"prompt version {pmt_id!r} not found"}
    name, content, status = row
    if status not in ("candidate", "held"):
        return {"error": f"prompt {pmt_id!r} is {status}, not evaluable"}

    ds_id = await ensure_golden_dataset(pool)
    system = content.format(tools=planner_tool_block(), max_steps=MAX_STEPS)

    details, passed = [], 0
    for case in GOLDEN_PLANNER_TASKS:
        try:
            out = await llm(system, _PLAN_USER.format(task=case["task"], context="(eval)"))
        except Exception as exc:  # noqa: BLE001
            out = f"LLM error: {exc}"
        ok, why = score_plan(out, case["expect"])
        passed += ok
        details.append({"task": case["task"][:80], "pass": ok, "why": why})
    pass_rate = passed / len(GOLDEN_PLANNER_TASKS)

    baseline = await _active_pass_rate(pool, name)
    regression = baseline is not None and pass_rate < baseline
    verdict = "pass" if (pass_rate >= PASS_THRESHOLD and not regression) else "held"

    evr_id = mint("evr")
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO evaluation_runs (id, candidate_ref, dataset_ids, scorers, "
                "pass_rate, decision) VALUES (%s, %s, %s, %s, %s, %s)",
                (evr_id, pmt_id, json.dumps([ds_id]),
                 json.dumps(["planner-constraints"]), pass_rate,
                 "promote" if verdict == "pass" else "hold"),
            )
            await cur.execute(
                "UPDATE prompt_versions SET eval_run_id = %s, "
                "status = CASE WHEN %s = 'held' THEN 'held' ELSE status END "
                "WHERE id = %s",
                (evr_id, verdict, pmt_id),
            )
        ev = build_event(
            short_type="evolution.eval.completed", subject=pmt_id,
            scope=scope_parts(scope),
            data={"pmt_id": pmt_id, "evr_id": evr_id, "pass_rate": pass_rate,
                  "baseline": baseline, "verdict": verdict},
            actor="system", source="curlyos-core/evolution",
        )
        await publisher.stage(ev, conn)
        if verdict == "held":
            held = build_event(
                short_type="evolution.candidate.held", subject=pmt_id,
                scope=scope_parts(scope),
                data={"pmt_id": pmt_id, "pass_rate": pass_rate, "baseline": baseline,
                      "reasons": [d["why"] for d in details if not d["pass"]][:5]},
                actor="system", source="curlyos-core/evolution",
            )
            await publisher.stage(held, conn)
    return {"pmt_id": pmt_id, "evr_id": evr_id, "pass_rate": pass_rate,
            "baseline": baseline, "verdict": verdict, "details": details}


async def activate_prompt(pool: Any, publisher: Any, redis: Any, scope: str, *,
                          pmt_id: str, approval_id: str) -> dict:
    """Gate 2 + the switch. Both gates run through the REAL PDP
    (action_class=self_modify): eval verdict + approval state must yield
    ALLOW — anything else refuses with the PDP's own reason."""
    from agent.pdp_gate import scope_parts
    from safety.budget import default_budget_snapshot
    from safety.killswitch import read_kill
    from safety.pdp import (AutonomyLevel, CapabilityGrantClaims, CapGrantFsPolicy,
                            CapGrantNetPolicy, PDPRequest, PDPVerdict, decide)
    from shared.events import build_event
    from shared.types.ulid import mint

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT name, version, status, eval_run_id FROM prompt_versions "
                "WHERE id = %s AND scope = %s", (pmt_id, scope),
            )
            row = await cur.fetchone()
            if row is None:
                return {"error": f"prompt version {pmt_id!r} not found"}
            name, version, status, evr_id = row
            if status != "candidate":
                return {"error": f"prompt {pmt_id!r} is {status}, not activatable"}
            eval_verdict = None
            if evr_id:
                await cur.execute("SELECT decision FROM evaluation_runs WHERE id = %s", (evr_id,))
                er = await cur.fetchone()
                eval_verdict = "pass" if (er and er[0] == "promote") else "fail"
            await cur.execute(
                "SELECT state FROM approvals WHERE id = %s AND scope = %s "
                "AND action_class = 'self_modify' AND expires_at > now()",
                (approval_id, scope),
            )
            ap = await cur.fetchone()
            approval_state = ap[0] if ap else None

    kill_global, kill_agent, kill_unreadable = await read_kill(redis, agent="Evolution")
    parts = scope_parts(scope)
    grant = CapabilityGrantClaims(
        grant_id="cap_evolution", agent="agent:Evolution", run_id="run_evolution",
        scope=scope, tools=["self_modify"], fs=CapGrantFsPolicy(), net=CapGrantNetPolicy(),
        memory_scope=[scope], max_autonomy=AutonomyLevel.CONFIRM_EACH,
    )
    decision = decide(PDPRequest(
        action_id=mint("act"), run_id="run_evolution", agent="Evolution",
        workspace_id="ws_user_scope", user_id=parts["user_id"],
        action_class="self_modify", tool="activate_prompt",
        args={"pmt_id": pmt_id},
        agent_default_level="confirm_each", workspace_override_level="confirm_each",
        capability_grant=grant, budget=default_budget_snapshot(),
        kill_global=kill_global, kill_agent=kill_agent, kill_unreadable=kill_unreadable,
        eval_verdict=eval_verdict, approval_state=approval_state,
    ))
    if decision.verdict != PDPVerdict.ALLOW:
        return {"error": f"PDP refused activation: {decision.reason}",
                "verdict": decision.verdict.value}

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE prompt_versions SET status = 'retired' "
                "WHERE name = %s AND status = 'active'", (name,),
            )
            await cur.execute(
                "UPDATE prompt_versions SET status = 'active', approval_id = %s, "
                "activated_at = now() WHERE id = %s", (approval_id, pmt_id),
            )
        ev = build_event(
            short_type="evolution.prompt.activated", subject=pmt_id,
            scope=parts,
            data={"pmt_id": pmt_id, "name": name, "version": version,
                  "approval_id": approval_id, "eval_run_id": evr_id,
                  "pdp_reason": decision.reason},
            actor=f"user:{parts['user_id']}", source="curlyos-core/evolution",
        )
        await publisher.stage(ev, conn)
    return {"pmt_id": pmt_id, "name": name, "version": version, "status": "active",
            "pdp_reason": decision.reason}


async def get_active_prompt(pool: Any, scope: str, name: str, default: str) -> str:
    """Runtime lookup — the hardcoded constant is version 0 / the fallback."""
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT content FROM prompt_versions "
                    "WHERE name = %s AND scope = %s AND status = 'active' LIMIT 1",
                    (name, scope),
                )
                row = await cur.fetchone()
        return row[0] if row else default
    except Exception:  # noqa: BLE001 — evolution must never break planning
        return default


async def list_prompt_versions(pool: Any, scope: str, name: str | None = None) -> list[dict]:
    where, params = ["p.scope = %s"], [scope]
    if name:
        where.append("p.name = %s")
        params.append(name)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT p.id, p.name, p.version, p.status, p.proposed_by, p.notes, "
                "p.created_at, p.activated_at, e.pass_rate, e.decision, p.approval_id "
                "FROM prompt_versions p LEFT JOIN evaluation_runs e ON e.id = p.eval_run_id "
                f"WHERE {' AND '.join(where)} ORDER BY p.name, p.version DESC",
                params,
            )
            rows = await cur.fetchall()
    return [
        {"id": r[0], "name": r[1], "version": r[2], "status": r[3],
         "proposed_by": r[4], "notes": r[5],
         "created_at": r[6].isoformat() if r[6] else None,
         "activated_at": r[7].isoformat() if r[7] else None,
         "pass_rate": float(r[8]) if r[8] is not None else None,
         "eval_decision": r[9], "approval_id": r[10]}
        for r in rows
    ]
