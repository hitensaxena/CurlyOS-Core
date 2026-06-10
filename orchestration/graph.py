"""The Executive graph — hydrate → plan → act-loop → synthesize → record.

Design contract (curlyos-final/06 §2):
  * Nodes are thin adapters; every domain operation is an existing core
    function reached through the tool registry.
  * Every side-effecting action passes the PDP. REQUIRE_APPROVAL parks the run
    via interrupt(); grant/deny resumes it (one resume primitive, the runner).
  * Replay safety: LangGraph re-executes an interrupted node from its top, so
    everything before interrupt() must be idempotent —
      - approvals are FOUND-or-created, keyed (run_id, payload->>'cursor'),
      - tool execution is guarded by an existing-action lookup on the same
        cursor key (a completed action's observation is reused, not re-run).
    Together with the hash-chained tool_calls this gives exactly-once side
    effects across crashes and resumes (the Spike-01 property).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Annotated, Any, Awaitable, Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from orchestration.tools import ToolDeps, execute_tool, planner_tool_block, REGISTRY

log = logging.getLogger("curlyos-core.orchestration.graph")

MAX_STEPS = 6
RUN_AUTONOMY = "confirm_each"  # the Phase-A ceiling, also in agent_runs DDL


def _append(left: list, right: list) -> list:
    return (left or []) + (right or [])


class AgentState(TypedDict, total=False):
    run_id: str
    scope: str
    task: str
    context: str
    subtasks: list[dict]           # [{tool, args, why}]
    cursor: int
    history: Annotated[list[dict], _append]   # [{cursor, tool, args, result|denied}]
    decision: dict | None


LLMFn = Callable[[str, str], Awaitable[str]]  # async (system, user) -> text
DepsFn = Callable[[str, str], Awaitable[ToolDeps]]  # (run_id, scope) -> deps


# ── planner ───────────────────────────────────────────────────────────────────

_PLAN_SYSTEM = """You are the Executive of CurlyOS, the user's cognitive operating system.
Plan the SHORTEST tool sequence that accomplishes the task. Available tools:

{tools}

Reply with ONLY a JSON array (max {max_steps} steps):
[{{"tool": "<name>", "args": {{...}}, "why": "<one line>"}}]
Prefer read tools first to gather context; only write (remember/create_goal/
record_decision/create_sketch) what the task genuinely asks to persist.
Use notify only when the task asks to message the user."""

_PLAN_USER = """TASK: {task}

CONTEXT (memory + identity + goals):
{context}"""


def _extract_json_array(text: str) -> list | None:
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if not m:
        return None
    try:
        out = json.loads(m.group(0))
        return out if isinstance(out, list) else None
    except json.JSONDecodeError:
        return None


def _fallback_plan(task: str) -> list[dict]:
    """No LLM (or unparseable plan): a single recall step so the run still
    produces a grounded answer instead of failing."""
    return [{"tool": "recall", "args": {"query": task[:500]}, "why": "fallback: gather context"}]


def _sanitize_plan(raw: list) -> list[dict]:
    plan: list[dict] = []
    for step in raw[:MAX_STEPS]:
        if not isinstance(step, dict):
            continue
        tool = str(step.get("tool", ""))
        if tool not in REGISTRY:
            continue
        args = step.get("args") if isinstance(step.get("args"), dict) else {}
        plan.append({"tool": tool, "args": args, "why": str(step.get("why", ""))[:300]})
    return plan


# ── graph factory ─────────────────────────────────────────────────────────────

def make_graph(get_deps: DepsFn, llm: LLMFn | None, checkpointer: Any):
    """Build the compiled Executive graph. `get_deps` resolves per-run infra;
    `llm` is the one LLM seam (None → deterministic fallbacks throughout)."""

    async def hydrate(state: AgentState) -> dict:
        deps = await get_deps(state["run_id"], state["scope"])
        parts: list[str] = []
        try:
            rec = await execute_tool("recall", deps, {"query": state["task"]})
            if rec.get("items"):
                parts.append("RELEVANT MEMORY:\n" + "\n".join(
                    f"- {i['content']}" for i in rec["items"][:8]))
        except Exception:  # noqa: BLE001
            log.warning("hydrate: recall failed", exc_info=True)
        try:
            ident = await execute_tool("get_identity", deps, {})
            if ident.get("identity"):
                parts.append(f"IDENTITY: {json.dumps(ident['identity'], default=str)[:1500]}")
        except Exception:  # noqa: BLE001
            log.warning("hydrate: identity failed", exc_info=True)
        try:
            goals = await execute_tool("list_goals", deps, {})
            if goals.get("goals"):
                parts.append("ACTIVE GOALS:\n" + "\n".join(
                    f"- [{g['id']}] {g['title']} (progress {g['progress']})"
                    for g in goals["goals"][:10]))
        except Exception:  # noqa: BLE001
            log.warning("hydrate: goals failed", exc_info=True)
        return {"context": "\n\n".join(parts)[:6000]}

    async def plan(state: AgentState) -> dict:
        subtasks: list[dict] | None = None
        if llm is not None:
            try:
                from orchestration.evolution import get_active_prompt

                deps = await get_deps(state["run_id"], state["scope"])
                template = await get_active_prompt(deps.pool, state["scope"],
                                                   "executive.plan", _PLAN_SYSTEM)
                text = await llm(
                    template.format(tools=planner_tool_block(), max_steps=MAX_STEPS),
                    _PLAN_USER.format(task=state["task"], context=state.get("context", "")),
                )
                raw = _extract_json_array(text)
                if raw is not None:
                    subtasks = _sanitize_plan(raw)
            except Exception:  # noqa: BLE001
                log.warning("plan: LLM failed — falling back", exc_info=True)
        if not subtasks:
            subtasks = _fallback_plan(state["task"])
        return {"subtasks": subtasks, "cursor": 0}

    async def act(state: AgentState) -> dict:
        from agent import hashchain
        from agent.pdp_gate import _create_approval, approval_ttl_seconds, evaluate
        from safety.pdp import PDPVerdict
        from shared.types.ulid import mint

        deps = await get_deps(state["run_id"], state["scope"])
        cursor = state.get("cursor", 0)
        step = state["subtasks"][cursor]
        tool = REGISTRY[step["tool"]]
        cursor_key = str(cursor)

        # replay guard: a completed action for this cursor → reuse its observation
        async with deps.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT o.result FROM actions a JOIN observations o ON o.action_id = a.id "
                    "WHERE a.run_id = %s AND a.payload->>'cursor' = %s LIMIT 1",
                    (state["run_id"], cursor_key),
                )
                done = await cur.fetchone()
        if done is not None:
            return {"cursor": cursor + 1,
                    "history": [{"cursor": cursor, "tool": step["tool"],
                                 "args": step["args"], "result": done[0], "replayed": True}]}

        decision = await evaluate(
            pool=deps.pool, redis=deps.redis, publisher=deps.publisher,
            scope_text=state["scope"], run_id=state["run_id"], action_id=mint("act"),
            action_class=tool.action_class, autonomy_level=RUN_AUTONOMY,
            tool=tool.name, args=step["args"], create_approval=False,
        )

        if decision.verdict == PDPVerdict.REQUIRE_APPROVAL:
            # find-or-create the approval for THIS cursor (idempotent on replay)
            async with deps.pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT id, state FROM approvals WHERE run_id = %s "
                        "AND payload->>'cursor' = %s ORDER BY created_at DESC LIMIT 1",
                        (state["run_id"], cursor_key),
                    )
                    row = await cur.fetchone()
            if row is None:
                apv_id = await _create_approval(
                    deps.pool, deps.publisher, state["scope"],
                    run_id=state["run_id"], action_class=tool.action_class,
                    decision=decision, ttl=approval_ttl_seconds(),
                    payload={"cursor": cursor_key, "tool": tool.name,
                             "args": step["args"], "why": step.get("why", "")},
                )
                apv_state = "pending"
            else:
                apv_id, apv_state = row

            if apv_state == "pending":
                park_payload = {"reason": "approval_required", "apv_id": apv_id,
                                "tool": tool.name, "action_class": tool.action_class,
                                "cursor": cursor}
                # interrupt() RETURNS (with the resume value) when the run is
                # resumed — it only raises on first execution. Grant/deny
                # resumes see the decided state in the SELECT above; a BARE
                # resume (run-page button, no decision) lands here with the
                # approval still pending — re-read, and re-park until a human
                # actually decides. Each loop iteration consumes one resume.
                while True:
                    interrupt(park_payload)
                    async with deps.pool.connection() as conn:
                        async with conn.cursor() as cur:
                            await cur.execute(
                                "SELECT state FROM approvals WHERE id = %s", (apv_id,),
                            )
                            row2 = await cur.fetchone()
                    apv_state = row2[0] if row2 else "expired"
                    if apv_state != "pending":
                        break
            if apv_state == "granted":
                decision = await evaluate(
                    pool=deps.pool, redis=deps.redis, publisher=deps.publisher,
                    scope_text=state["scope"], run_id=state["run_id"], action_id=mint("act"),
                    action_class=tool.action_class, autonomy_level=RUN_AUTONOMY,
                    tool=tool.name, args=step["args"],
                    approval_id=apv_id, create_approval=False,
                )
            else:  # denied / expired — record and move on
                return {"cursor": cursor + 1,
                        "history": [{"cursor": cursor, "tool": step["tool"],
                                     "args": step["args"],
                                     "denied": f"approval_{apv_state}"}]}

        if decision.verdict != PDPVerdict.ALLOW:
            return {"cursor": cursor + 1,
                    "history": [{"cursor": cursor, "tool": step["tool"],
                                 "args": step["args"],
                                 "denied": decision.reason}]}

        # execute + audit rows (action, observation, hash-chained tool_call)
        result = await execute_tool(tool.name, deps, step["args"])
        act_id, obs_id, tcl_id = mint("act"), mint("obs"), mint("tcl")
        result_json = json.dumps(result, default=str)
        result_blob = (result if len(result_json) <= 15_000
                       else {"truncated": True, "preview": result_json[:8000]})
        async with deps.pool.connection() as conn:
            await hashchain.insert_action(conn, act_id, state["run_id"], tool.action_class,
                                          {"tool": tool.name, "args": step["args"],
                                           "cursor": cursor_key})
            await hashchain.insert_observation(conn, obs_id, act_id, result_blob)
            await hashchain.insert_tool_call(conn, state["run_id"], tcl_id, act_id,
                                             tool.name, step["args"], result_blob)
        return {"cursor": cursor + 1,
                "history": [{"cursor": cursor, "tool": tool.name, "args": step["args"],
                             "result": result_blob}]}

    def route_after_act(state: AgentState) -> str:
        return "act" if state.get("cursor", 0) < len(state.get("subtasks", [])) else "synthesize"

    async def synthesize(state: AgentState) -> dict:
        history = state.get("history", [])
        summary: str | None = None
        if llm is not None and history:
            try:
                summary = await llm(
                    "Summarize what the Executive run accomplished for the user, in 2-5 plain "
                    "sentences. Mention anything denied or failed. No preamble.",
                    f"TASK: {state['task']}\n\nSTEPS:\n"
                    + json.dumps(history, default=str)[:8000],
                )
            except Exception:  # noqa: BLE001
                log.warning("synthesize: LLM failed — falling back", exc_info=True)
        if not summary:
            done = sum(1 for h in history if "result" in h)
            denied = sum(1 for h in history if "denied" in h)
            summary = (f"Ran {len(history)} step(s): {done} completed, {denied} denied. "
                       f"Tools: {', '.join(h['tool'] for h in history) or 'none'}.")
        return {"decision": {"summary": summary.strip()[:4000],
                             "steps": len(history),
                             "denied": [h["tool"] for h in history if "denied" in h]}}

    async def record(state: AgentState) -> dict:
        """The run's outcome becomes memory (every run is an episode)."""
        from memory.governance import record_episode

        deps = await get_deps(state["run_id"], state["scope"])
        try:
            await record_episode(
                deps.pool, deps.publisher, state["scope"],
                content=f"[agent run {state['run_id']}] task: {state['task'][:300]} — "
                        f"{state['decision']['summary']}",
                source_ref=f"agent:{state['run_id']}",
            )
        except Exception:  # noqa: BLE001 — the run row still records the outcome
            log.warning("record: episode write failed", exc_info=True)
        return {}

    g = StateGraph(AgentState)
    g.add_node("hydrate", hydrate)
    g.add_node("plan", plan)
    g.add_node("act", act)
    g.add_node("synthesize", synthesize)
    g.add_node("record", record)
    g.add_edge(START, "hydrate")
    g.add_edge("hydrate", "plan")
    g.add_edge("plan", "act")
    g.add_conditional_edges("act", route_after_act, {"act": "act", "synthesize": "synthesize"})
    g.add_edge("synthesize", "record")
    g.add_edge("record", END)
    return g.compile(checkpointer=checkpointer)
