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

from shared.llm import first_json
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from orchestration.tools import ToolDeps, execute_tool, planner_tool_block, REGISTRY

log = logging.getLogger("curlyos-core.orchestration.graph")

MAX_STEPS = 9  # room to read → write the artifact → build/verify → commit
RUN_AUTONOMY = "confirm_each"  # the Phase-A ceiling, also in agent_runs DDL


def _append(left: list, right: list) -> list:
    return (left or []) + (right or [])


class AgentState(TypedDict, total=False):
    run_id: str
    scope: str
    task: str
    context: str
    autonomy: str                  # run autonomy level (confirm_each | full_auto …)
    cursor: int                    # count of actions taken (the loop index)
    finished: bool                 # the agent signalled the deliverable is done
    history: Annotated[list[dict], _append]   # [{cursor, tool, args, result|denied}]
    decision: dict | None


LLMFn = Callable[[str, str], Awaitable[str]]  # async (system, user) -> text
DepsFn = Callable[[str, str], Awaitable[ToolDeps]]  # (run_id, scope) -> deps


# ── the ReAct loop (decide ONE action at a time) ───────────────────────────────
# We do NOT ask for a whole plan upfront: the models in the chain emit a single
# native tool call per turn (some in proprietary markup), and "analyse what you
# retrieve" is impossible if the write content is generated before the read runs.
# So the Executive acts step-by-step — decide → execute → observe → decide — and
# each decision sees the real results of the prior steps.

_DECIDE_SYSTEM = """You are the Executive of CurlyOS, the user's cognitive operating system.
You ACTUALLY DO the task in reality, ONE step at a time. You are given the task,
context, and the steps you have already taken WITH THEIR RESULTS. Decide the
single next action.

Available tools:
{tools}

Reply with ONLY ONE JSON object — either an action:
  {{"tool": "<name>", "args": {{...}}, "why": "<one line>"}}
or, when the task's deliverable genuinely exists and is complete:
  {{"done": true}}

Rules:
  - Gather first (recall / read_file / list_dir), THEN use what you actually found
    — never invent facts you didn't retrieve.
  - Produce real output with write_file (the FULL, substantive content in args) or
    edit_file. Ground the content in the results of your earlier steps.
  - After code/site changes, run_command the build or test (e.g. "npm run build")
    to CHECK your work, and fix what fails.
  - Write deliverables into the WORKING DIRECTORY named in the task, and after
    writing a real output (a doc, file, code change, image) call save_artifact
    (title, path, kind) so it appears in the goal's studio.
  - Use git_commit to save changes locally. NEVER use git_push — publishing is
    gated separately.
  - Use remember/record_decision/create_sketch to persist an insight or analysis
    the task asks you to keep.
  - For anything on the WEB or external (current info, sources, prices, news) use
    web_research; to read a specific page use browse; to make an image use
    generate_image. These delegate to the Hermes agent and may take a while.
  - Do NOT repeat an action that already succeeded. Only say {{"done": true}} once
    the concrete deliverable the task asked for actually exists.
  - Only use the tools listed above."""

_DECIDE_USER = """TASK: {task}

CONTEXT (memory + identity + goals + lessons):
{context}

STEPS TAKEN SO FAR ({n} of max {max_steps}):
{history}
{budget_nudge}
Decide the single next action (or {{"done": true}} if the deliverable is complete).
Reply with ONLY one JSON object."""

# Native tool-call markup some models emit instead of JSON, e.g.:
#   <longcat_tool_call>recall
#   <longcat_arg_key>query</longcat_arg_key><longcat_arg_value>…</longcat_arg_value>
#   </longcat_tool_call>
_TOOLCALL_RE = re.compile(r"<\w+_tool_call>\s*([A-Za-z_][\w]*)(.*?)</\w+_tool_call>", re.S)
_ARG_RE = re.compile(r"<\w+_arg_key>\s*(.*?)\s*</\w+_arg_key>\s*<\w+_arg_value>(.*?)</\w+_arg_value>", re.S)


def parse_next_action(text: str | None) -> dict | None:
    """Parse the model's next action from JSON or native tool-call markup.
    Returns {"tool","args","why"} | {"done": True} | None (unparseable)."""
    if not text:
        return None
    data = first_json(text)
    if isinstance(data, list):  # a plan array — take the first usable step
        data = next((d for d in data if isinstance(d, dict)), None)
    if isinstance(data, dict):
        if data.get("done") or str(data.get("action", "")).lower() in ("done", "finish"):
            return {"done": True}
        tool = data.get("tool") or data.get("name") or data.get("tool_name")
        if tool:
            args = data.get("args") if isinstance(data.get("args"), dict) else (
                data.get("arguments") if isinstance(data.get("arguments"), dict) else {})
            return {"tool": str(tool), "args": args or {}, "why": str(data.get("why", ""))[:300]}
    # native markup fallback
    m = _TOOLCALL_RE.search(text)
    if m:
        tool = m.group(1)
        args: dict = {}
        for k, v in _ARG_RE.findall(m.group(2)):
            args[k.strip()] = v.strip()
        return {"tool": str(tool), "args": args, "why": "native tool call"}
    return None


def _render_history(history: list[dict], budget: int = 4000) -> str:
    """Compact, RESULT-bearing view of prior steps so the next decision is
    grounded in what actually happened (newest kept if we run out of budget)."""
    lines: list[str] = []
    for h in history:
        tool = h.get("tool", "?")
        if "denied" in h:
            lines.append(f"- {tool}: DENIED ({h['denied']})")
            continue
        res = h.get("result")
        r = res if isinstance(res, dict) else {}
        if r.get("error"):
            detail = f"ERROR {r['error']}"
        elif tool == "recall":
            items = r.get("items") or []
            detail = f"{r.get('count', len(items))} memories" + (
                ": " + " | ".join(str(i.get("content", ""))[:120] for i in items[:4]) if items else "")
        elif tool in ("write_file", "edit_file"):
            detail = f"{r.get('action', 'edited')} {r.get('path', '')}"
        elif tool in ("run_command", "git_commit"):
            detail = f"exit={r.get('exit_code')} {(r.get('stdout') or r.get('stderr') or '')[:200]}"
        else:
            detail = json.dumps(r, default=str)[:200]
        lines.append(f"- {tool}({json.dumps(h.get('args', {}), default=str)[:160]}) -> {detail}")
    text = "\n".join(lines) if lines else "(nothing yet — this is the first step)"
    if len(text) > budget:  # keep the most recent, drop oldest
        text = "…(earlier steps elided)…\n" + text[-budget:]
    return text


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
        try:
            lessons = await execute_tool("recall_lessons", deps, {"query": state["task"]})
            if lessons.get("lessons"):
                parts.append("LESSONS FROM PAST DECISIONS:\n" + "\n".join(
                    f"- {ls['statement']} (confidence {round(ls['confidence'], 2)})"
                    for ls in lessons["lessons"][:5]))
        except Exception:  # noqa: BLE001
            log.warning("hydrate: lessons failed", exc_info=True)
        return {"context": "\n\n".join(parts)[:6000], "cursor": 0, "finished": False}

    async def _decide_next(state: AgentState) -> dict | None:
        """Ask the model for the SINGLE next action given everything done so far.
        Returns {"tool","args","why"} | {"done": True} | None. A first-step
        deterministic fallback (recall) keeps a run useful if the LLM is down."""
        cursor = state.get("cursor", 0)
        if llm is None:
            return ({"tool": "recall", "args": {"query": state["task"][:500]}, "why": "gather context"}
                    if cursor == 0 else {"done": True})
        try:
            from orchestration.evolution import get_active_prompt
            deps = await get_deps(state["run_id"], state["scope"])
            template = await get_active_prompt(deps.pool, state["scope"],
                                               "executive.act", _DECIDE_SYSTEM)
            remaining = MAX_STEPS - cursor
            # Stop the model over-gathering: once it has read enough, push it to
            # PRODUCE the deliverable before the step budget runs out.
            nudge = ""
            if cursor >= 2 and remaining <= 4:
                nudge = (f"\n⚠ Only {remaining} step(s) left. If you already have enough to "
                         f"produce the deliverable, WRITE IT NOW (write_file / edit_file / the "
                         f"persist tool the task asks for) instead of gathering more.\n")
            text = await llm(
                template.format(tools=planner_tool_block(), max_steps=MAX_STEPS),
                _DECIDE_USER.format(task=state["task"], context=state.get("context", ""),
                                    n=cursor, max_steps=MAX_STEPS, budget_nudge=nudge,
                                    history=_render_history(state.get("history", []))),
            )
            action = parse_next_action(text)
        except Exception:  # noqa: BLE001
            log.warning("decide: LLM failed", exc_info=True)
            action = None
        if action is None:  # unparseable: recall on the first step, else stop
            return ({"tool": "recall", "args": {"query": state["task"][:500]}, "why": "fallback gather"}
                    if cursor == 0 else {"done": True})
        return action

    async def act(state: AgentState) -> dict:
        from agent import hashchain
        from agent.pdp_gate import _create_approval, approval_ttl_seconds, evaluate
        from safety.pdp import PDPVerdict
        from shared.types.ulid import mint

        deps = await get_deps(state["run_id"], state["scope"])
        cursor = state.get("cursor", 0)
        cursor_key = str(cursor)
        autonomy = state.get("autonomy") or RUN_AUTONOMY  # bypass runs pass full_auto

        # replay guard: a completed action for this cursor → reuse its observation
        async with deps.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT a.payload->>'tool', a.payload->'args', o.result "
                    "FROM actions a JOIN observations o ON o.action_id = a.id "
                    "WHERE a.run_id = %s AND a.payload->>'cursor' = %s LIMIT 1",
                    (state["run_id"], cursor_key),
                )
                done = await cur.fetchone()
        if done is not None:
            return {"cursor": cursor + 1,
                    "history": [{"cursor": cursor, "tool": done[0],
                                 "args": done[1] or {}, "result": done[2], "replayed": True}]}

        # An approval already exists for this cursor → reconstruct the SAME action
        # from its payload (deterministic on replay; no second LLM call).
        async with deps.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, state, payload FROM approvals WHERE run_id = %s "
                    "AND payload->>'cursor' = %s ORDER BY created_at DESC LIMIT 1",
                    (state["run_id"], cursor_key),
                )
                apv_row = await cur.fetchone()

        if apv_row is not None:
            apv_id, apv_state, payload = apv_row
            action = {"tool": payload.get("tool"), "args": payload.get("args") or {},
                      "why": payload.get("why", "")}
        else:
            apv_id = apv_state = None
            action = await _decide_next(state)
            if action is None or action.get("done"):
                return {"finished": True}
            if action.get("tool") not in REGISTRY:
                # hallucinated/unavailable tool — record it so the next decision
                # can adapt, and move on (the step cap bounds any thrashing).
                return {"cursor": cursor + 1,
                        "history": [{"cursor": cursor, "tool": str(action.get("tool")),
                                     "args": action.get("args", {}),
                                     "result": {"error": "tool not available",
                                                "available": list(REGISTRY)}}]}

        tool = REGISTRY[action["tool"]]
        args = action.get("args") or {}
        why = action.get("why", "")
        # net_egress tools (Hermes delegation) carry an allowed host for the PDP
        egress_host = getattr(tool, "egress_host", None)
        egress_allow = [egress_host] if egress_host else None

        decision = await evaluate(
            pool=deps.pool, redis=deps.redis, publisher=deps.publisher,
            scope_text=state["scope"], run_id=state["run_id"], action_id=mint("act"),
            action_class=tool.action_class, autonomy_level=autonomy,
            tool=tool.name, args=args, create_approval=False,
            host=egress_host, egress_allow=egress_allow,
        )

        if decision.verdict == PDPVerdict.REQUIRE_APPROVAL:
            if apv_row is None:  # first encounter → persist the action in the approval
                apv_id = await _create_approval(
                    deps.pool, deps.publisher, state["scope"],
                    run_id=state["run_id"], action_class=tool.action_class,
                    decision=decision, ttl=approval_ttl_seconds(),
                    payload={"cursor": cursor_key, "tool": tool.name, "args": args, "why": why},
                )
                apv_state = "pending"

            if apv_state == "pending":
                park_payload = {"reason": "approval_required", "apv_id": apv_id,
                                "tool": tool.name, "action_class": tool.action_class,
                                "cursor": cursor}
                # interrupt() RETURNS (with the resume value) when the run is
                # resumed — it only raises on first execution. A bare resume with
                # the approval still pending re-parks until a human decides.
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
                    action_class=tool.action_class, autonomy_level=autonomy,
                    tool=tool.name, args=args, approval_id=apv_id, create_approval=False,
                    host=egress_host, egress_allow=egress_allow,
                )
            else:  # denied / expired — record and move on
                return {"cursor": cursor + 1,
                        "history": [{"cursor": cursor, "tool": tool.name, "args": args,
                                     "denied": f"approval_{apv_state}"}]}

        if decision.verdict != PDPVerdict.ALLOW:
            return {"cursor": cursor + 1,
                    "history": [{"cursor": cursor, "tool": tool.name, "args": args,
                                 "denied": decision.reason}]}

        # execute + audit rows (action, observation, hash-chained tool_call)
        result = await execute_tool(tool.name, deps, args)
        act_id, obs_id, tcl_id = mint("act"), mint("obs"), mint("tcl")
        result_json = json.dumps(result, default=str)
        result_blob = (result if len(result_json) <= 15_000
                       else {"truncated": True, "preview": result_json[:8000]})
        async with deps.pool.connection() as conn:
            await hashchain.insert_action(conn, act_id, state["run_id"], tool.action_class,
                                          {"tool": tool.name, "args": args, "cursor": cursor_key})
            await hashchain.insert_observation(conn, obs_id, act_id, result_blob)
            await hashchain.insert_tool_call(conn, state["run_id"], tcl_id, act_id,
                                             tool.name, args, result_blob)
        return {"cursor": cursor + 1,
                "history": [{"cursor": cursor, "tool": tool.name, "args": args,
                             "result": result_blob}]}

    def route_after_act(state: AgentState) -> str:
        if state.get("finished") or state.get("cursor", 0) >= MAX_STEPS:
            return "synthesize"
        return "act"

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
    g.add_node("act", act)
    g.add_node("synthesize", synthesize)
    g.add_node("record", record)
    g.add_edge(START, "hydrate")
    g.add_edge("hydrate", "act")
    g.add_conditional_edges("act", route_after_act, {"act": "act", "synthesize": "synthesize"})
    g.add_edge("synthesize", "record")
    g.add_edge("record", END)
    return g.compile(checkpointer=checkpointer)
