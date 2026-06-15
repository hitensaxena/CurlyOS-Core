"""The goal-execution orchestrator.

Turns a GOAL into a PLAN of concrete tasks, dispatches each task to a WORKER
(an Executive `agent_run`), and aggregates worker outcomes back into the goal's
progress. A command-chat lets the user steer it in natural language.

Design:
  * Plan-then-approve. `decompose_goal` proposes a plan (status 'proposed');
    nothing runs until `approve_plan`, then `dispatch_*` starts workers. Worker
    side effects still pass the PDP (park for approval) exactly as any run.
  * Workers are ordinary `Runner.start_run` Executive runs, tagged with
    `goal_id`. Their completion flows back through `on_worker_done` (wired to the
    runner's on_run_event hook in api_server) which updates the goal_task and
    recomputes goal progress — so progress is driven by real run outcomes.
  * Every state change emits an event (goal.plan.*, goal.task.*, goal.progress)
    so the webapp's live feed shows the orchestrator working.
  * LLM calls are guarded: an empty completion (provider rate-limit) surfaces a
    retryable error instead of silently doing nothing.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Awaitable, Callable

from shared.llm import first_json, json_records

log = logging.getLogger("curlyos-core.orchestrator")

PoolFactory = Callable[[], Awaitable[Any]]

# Run-status → task-status mapping.
_TASK_STATUS = {
    "completed": "completed", "failed": "failed",
    "parked": "parked", "cancelled": "skipped",
}


# ── prompts ───────────────────────────────────────────────────────────────────

DECOMPOSE_SYSTEM = """You are the Orchestrator of CurlyOS. You turn a GOAL into a small plan of
concrete, independently-executable TASKS that worker agents will carry out.

Each worker agent can: search the user's memory (recall), search their knowledge
graph, list goals, read identity, remember new facts, record decisions, create
sub-goals, write notes to the studio, and send the user a notification. Workers
CANNOT browse the web or act on external systems beyond a notification.

Produce 3-6 tasks. Each task must be:
  - self-contained and doable by one worker in a few steps,
  - phrased as a direct instruction to the worker (e.g. "Search memory for X and
    summarize the relevant threads", "Draft a concrete plan for…", "Record a
    decision about…"),
  - clearly advancing the goal.

Reply ONLY JSON, no prose:
{"rationale": "<1-2 sentences on the overall approach>",
 "tasks": [{"title": "<short label>", "task": "<instruction to the worker>",
            "why": "<how it advances the goal>"}]}"""

CHAT_SYSTEM = """You are the Orchestrator of CurlyOS, managing worker agents that execute the
user's goals. The user sends a command or question about a GOAL and its plan.
Pick the single best ACTION and write a short reply.

Actions:
  - "decompose": break the goal into a fresh task plan. Use when there is no plan
    yet, or the user asks to (re)plan / break it down / figure out how.
  - "approve": approve the current proposed plan so workers can start.
  - "dispatch": start worker agents on the approved plan (do the work now).
  - "status": report current progress only; change nothing.
  - "none": just answer the user; take no action.

Consider the CURRENT STATE provided. Reply ONLY JSON:
{"action": "<one of the actions>", "reply": "<1-3 sentence reply to the user>"}"""


# ── decompose ─────────────────────────────────────────────────────────────────

async def decompose_goal(
    *, pool: Any, publisher: Any, llm: Any, scope: str,
    goal_id: str, guidance: str | None = None, notify_inbox: bool = True,
) -> dict:
    """LLM-decompose a goal into a proposed plan of worker tasks."""
    from shared.types.ulid import mint

    if llm is None:
        return {"error": "decomposition requires an LLM"}
    goal = await _load_goal(pool, scope, goal_id)
    if goal is None:
        return {"error": f"goal {goal_id!r} not found"}

    text = await llm(DECOMPOSE_SYSTEM, _goal_block(goal, guidance))
    if not text or not str(text).strip():
        return {"error": "the model returned nothing (likely rate-limited) — try again shortly"}

    data = first_json(text)
    rationale = ""
    tasks_raw: list = []
    if isinstance(data, dict):
        rationale = str(data.get("rationale", ""))[:2000]
        tasks_raw = data.get("tasks") or []
    if not tasks_raw:  # salvage records from a truncated/fenced reply
        tasks_raw = json_records(text) or []
    tasks = [t for t in tasks_raw if isinstance(t, dict) and str(t.get("task", "")).strip()]
    if not tasks:
        return {"error": "could not parse a task plan from the model — try again"}

    plan_id = mint("gpl")
    created: list[str] = []
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            # supersede any live plan for this goal
            await cur.execute(
                "UPDATE goal_plans SET status='abandoned', updated_at=now() "
                "WHERE goal_id=%s AND status IN ('proposed','approved','executing')",
                (goal_id,),
            )
            await cur.execute(
                "INSERT INTO goal_plans (id, scope, goal_id, status, rationale) "
                "VALUES (%s, %s, %s, 'proposed', %s)",
                (plan_id, scope, goal_id, rationale),
            )
            for i, t in enumerate(tasks[:6]):
                await cur.execute(
                    "INSERT INTO goal_tasks (id, scope, plan_id, goal_id, seq, title, task, why) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (mint("gtk"), scope, plan_id, goal_id, i,
                     str(t.get("title") or f"Task {i + 1}")[:200],
                     str(t["task"])[:2000], str(t.get("why", ""))[:1000]),
                )
                created.append(t["task"])
    await _emit(pool, publisher, scope, "goal.plan.proposed", goal_id,
                {"plan_id": plan_id, "tasks": len(created)})
    if notify_inbox:
        titles = [str(t.get("title") or t["task"])[:120] for t in tasks[:6]]
        await _deliver_plan_inbox(pool, scope, goal_id, goal["title"], plan_id, titles, rationale)
    log.info("orchestrator: proposed plan %s for goal %s (%d tasks)", plan_id, goal_id, len(created))
    return {"plan_id": plan_id, "goal_id": goal_id, "rationale": rationale,
            "task_count": len(created)}


async def _deliver_plan_inbox(
    pool, scope, goal_id, goal_title, plan_id, task_titles: list[str], rationale: str,
) -> None:
    """Drop a 'plan ready' item in the inbox with the exact plan + an execute hook
    (meta.kind='plan' so the inbox UI renders the task list + an Execute button)."""
    from psycopg.types.json import Jsonb
    from shared.types.ulid import mint
    body_lines = [rationale.strip()] if rationale.strip() else []
    body_lines += [f"{i + 1}. {t}" for i, t in enumerate(task_titles)]
    body = "\n".join(body_lines) or "(plan ready)"
    title = f"Plan ready: {goal_title} ({len(task_titles)} task{'s' if len(task_titles) != 1 else ''})"
    meta = {"kind": "plan", "plan_id": plan_id, "goal_id": goal_id,
            "goal_title": goal_title, "tasks": task_titles}
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO inbox_items (id, scope, job_id, run_id, title, body, meta) "
                "VALUES (%s, %s, NULL, NULL, %s, %s, %s)",
                (mint("inb"), scope, title, body, Jsonb(meta)),
            )


# ── approve / dispatch ────────────────────────────────────────────────────────

async def approve_plan(*, pool: Any, publisher: Any, scope: str, plan_id: str) -> dict:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE goal_plans SET status='approved', updated_at=now() "
                "WHERE id=%s AND scope=%s AND status='proposed' RETURNING goal_id",
                (plan_id, scope),
            )
            row = await cur.fetchone()
    if row is None:
        return {"error": "plan not found or not in 'proposed' state"}
    await _emit(pool, publisher, scope, "goal.plan.approved", row[0], {"plan_id": plan_id})
    return {"plan_id": plan_id, "status": "approved"}


async def dispatch_task(
    *, pool: Any, publisher: Any, runner: Any, scope: str, task_id: str,
    autonomy: str | None = None,
) -> dict:
    """Start a worker (Executive run) for one task in an approved plan.

    `autonomy` overrides the run's autonomy level (None → the global bypass
    default). The execute-plan path passes 'full_auto' so a plan the user chose
    to execute runs end-to-end without per-action approval.
    """
    if runner is None:
        return {"error": "runner unavailable"}
    task = await _load_task(pool, scope, task_id)
    if task is None:
        return {"error": "task not found"}
    if task["status"] in ("dispatched", "running", "completed"):
        return {"error": f"task already {task['status']}", "run_id": task["run_id"]}
    if task["plan_status"] not in ("approved", "executing"):
        return {"error": "approve the plan before dispatching its tasks"}

    run_id = await runner.start_run(
        task["task"], source=f"goal:{task['goal_id']}", goal_id=task["goal_id"],
        autonomy=autonomy,
    )
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE goal_tasks SET status='running', run_id=%s, updated_at=now() WHERE id=%s",
                (run_id, task_id),
            )
            await cur.execute(
                "UPDATE goal_plans SET status='executing', updated_at=now() "
                "WHERE id=%s AND status='approved'",
                (task["plan_id"],),
            )
    await _emit(pool, publisher, scope, "goal.task.dispatched", task["goal_id"],
                {"task_id": task_id, "run_id": run_id})
    return {"task_id": task_id, "run_id": run_id, "status": "running"}


async def dispatch_plan(
    *, pool: Any, publisher: Any, runner: Any, scope: str, plan_id: str,
    autonomy: str | None = None,
) -> dict:
    """Dispatch every still-pending task in an approved plan."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM goal_tasks WHERE plan_id=%s AND scope=%s AND status='pending' "
                "ORDER BY seq",
                (plan_id, scope),
            )
            task_ids = [r[0] for r in await cur.fetchall()]
    dispatched = []
    for tid in task_ids:
        r = await dispatch_task(pool=pool, publisher=publisher, runner=runner,
                                scope=scope, task_id=tid, autonomy=autonomy)
        if r.get("run_id"):
            dispatched.append(r)
    return {"plan_id": plan_id, "dispatched": len(dispatched), "runs": dispatched}


async def execute_plan(
    *, pool: Any, publisher: Any, runner: Any, scope: str, plan_id: str,
) -> dict:
    """Approve (if needed) and dispatch a whole plan AUTONOMOUSLY (full_auto).

    The one-click path from the inbox/chat: the user chose to execute, so workers
    run end-to-end without per-action approval regardless of the global bypass.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE goal_plans SET status='approved', updated_at=now() "
                "WHERE id=%s AND scope=%s AND status='proposed'",
                (plan_id, scope),
            )
            await cur.execute(
                "SELECT goal_id, status FROM goal_plans WHERE id=%s AND scope=%s",
                (plan_id, scope),
            )
            row = await cur.fetchone()
    if row is None:
        return {"error": "plan not found"}
    if row[1] not in ("approved", "executing"):
        return {"error": f"plan is {row[1]} — cannot execute"}
    await _emit(pool, publisher, scope, "goal.plan.approved", row[0], {"plan_id": plan_id})
    return await dispatch_plan(pool=pool, publisher=publisher, runner=runner,
                               scope=scope, plan_id=plan_id, autonomy="full_auto")


# ── autonomous planning sweep ─────────────────────────────────────────────────

async def autoplan_sweep(
    *, pool: Any, publisher: Any, llm: Any, scope: str, max_goals: int = 3,
) -> dict:
    """Pull active goals that have no live plan and decompose them (capped per
    sweep to bound LLM cost). Each new plan lands in the inbox. Respects the
    `auto_plan` setting (default on)."""
    from shared.settings import get_setting
    if not await get_setting(pool, "auto_plan", True):
        return {"skipped": "auto_plan_off", "planned": []}

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT g.id, g.title FROM goals g "
                "WHERE g.scope=%s AND g.status='active' AND g.valid_to IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM goal_plans p WHERE p.goal_id=g.id "
                "                AND p.status IN ('proposed','approved','executing')) "
                "ORDER BY g.progress ASC, g.valid_from ASC LIMIT %s",
                (scope, max_goals),
            )
            candidates = await cur.fetchall()

    planned = []
    for gid, _title in candidates:
        r = await decompose_goal(pool=pool, publisher=publisher, llm=llm, scope=scope,
                                 goal_id=gid, notify_inbox=True)
        if not r.get("error"):
            planned.append({"goal_id": gid, "plan_id": r["plan_id"],
                            "task_count": r["task_count"]})
        else:
            log.info("autoplan: skip %s (%s)", gid, r["error"])
    if planned:
        log.info("autoplan: planned %d/%d candidate goal(s)", len(planned), len(candidates))
    return {"planned": planned, "candidates": len(candidates)}


# ── artifacts produced by a goal's runs ───────────────────────────────────────

_WRITE_TOOLS = {
    "remember": "memory", "record_decision": "decision", "review_decision": "decision",
    "create_goal": "subgoal", "create_sketch": "sketch", "notify": "notification",
}


async def get_artifacts(pool: Any, scope: str, goal_id: str) -> list[dict]:
    """Everything the goal's worker runs produced — the write-tool calls with
    their content and the id of what they created."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT tc.tool, tc.args, tc.created_at, a.run_id, o.result "
                "FROM tool_calls tc JOIN actions a ON tc.action_id = a.id "
                "JOIN agent_runs r ON r.id = a.run_id "
                "LEFT JOIN observations o ON o.action_id = a.id "
                "WHERE r.goal_id = %s AND r.scope = %s ORDER BY tc.created_at",
                (goal_id, scope),
            )
            rows = await cur.fetchall()
    arts = []
    for tool, args, at, run_id, result in rows:
        if tool not in _WRITE_TOOLS:
            continue
        arts.append({
            "type": _WRITE_TOOLS[tool], "tool": tool,
            "summary": _artifact_summary(tool, args),
            "ref": _artifact_ref(result),
            "run_id": run_id,
            "created_at": at.isoformat() if at else None,
        })
    return arts


def _artifact_summary(tool: str, args: Any) -> str:
    a = args if isinstance(args, dict) else {}
    key = {"remember": "statement", "record_decision": "title", "review_decision": "outcome",
           "create_goal": "title", "create_sketch": "content", "notify": "text"}.get(tool)
    val = a.get(key) if key else None
    if not val and tool == "record_decision":
        val = a.get("chosen")
    return str(val or "")[:400]


def _artifact_ref(result: Any) -> str | None:
    if isinstance(result, dict):
        for k in ("sketch_id", "dec_id", "goal_id", "mem_id", "id"):
            if result.get(k):
                return str(result[k])
    return None


# ── worker completion → progress (runner hook) ────────────────────────────────

async def on_worker_done(
    pool_factory: PoolFactory, publisher_factory: Callable[[], Any],
    scope: str, run_id: str, status: str,
) -> None:
    """Runner on_run_event hook: if this run is a goal-task worker, update the
    task and recompute the goal's progress. No-op for non-goal runs. Idempotent
    and defensive — never raises into the run loop."""
    try:
        pool = await pool_factory()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, goal_id, plan_id FROM goal_tasks WHERE run_id=%s", (run_id,)
                )
                row = await cur.fetchone()
        if row is None:
            return  # not a goal-task worker
        task_id, goal_id, plan_id = row

        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT status, result, error FROM agent_runs WHERE id=%s", (run_id,)
                )
                r = await cur.fetchone()
        run_status = r[0] if r else status
        summary = _extract_summary(r[1]) if r else None
        error = r[2] if r else None
        task_status = _TASK_STATUS.get(run_status, run_status)

        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE goal_tasks SET status=%s, result_summary=%s, updated_at=now() "
                    "WHERE id=%s",
                    (task_status, (summary or error or "")[:4000], task_id),
                )

        publisher = publisher_factory() if publisher_factory else None
        if run_status in ("completed", "failed", "cancelled", "parked"):
            await _recompute_progress(pool, publisher, scope, goal_id, plan_id)
        log.info("orchestrator: worker %s for goal %s → task %s (%s)",
                 run_id, goal_id, task_id, task_status)
    except Exception:  # noqa: BLE001
        log.exception("orchestrator: on_worker_done failed for run %s", run_id)


async def _recompute_progress(pool, publisher, scope, goal_id, plan_id) -> None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status, count(*) FROM goal_tasks WHERE plan_id=%s GROUP BY status",
                (plan_id,),
            )
            counts = {s: n for s, n in await cur.fetchall()}
    total = sum(counts.values())
    if total == 0:
        return
    completed = counts.get("completed", 0)
    terminal = completed + counts.get("failed", 0) + counts.get("skipped", 0)
    progress = round(completed / total, 4)
    plan_done = terminal >= total
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE goals SET progress=%s WHERE id=%s AND scope=%s",
                (progress, goal_id, scope),
            )
            if plan_done:
                await cur.execute(
                    "UPDATE goal_plans SET status='done', updated_at=now() "
                    "WHERE id=%s AND status='executing'",
                    (plan_id,),
                )
    await _emit(pool, publisher, scope, "goal.progress", goal_id,
                {"progress": progress, "completed": completed, "total": total,
                 "plan_done": plan_done})


# ── command chat ──────────────────────────────────────────────────────────────

async def orchestrator_chat(
    *, pool: Any, publisher: Any, llm: Any, runner: Any, scope: str,
    message: str, goal_id: str | None = None,
) -> dict:
    """Interpret a natural-language command and act: decompose / approve /
    dispatch / status / answer. Persists both sides of the exchange."""
    message = (message or "").strip()
    if not message:
        return {"error": "empty message"}
    await _save_message(pool, scope, goal_id, "user", message)

    state = await _goal_state(pool, scope, goal_id) if goal_id else None
    action, reply = await _interpret(llm, message, state)

    meta: dict = {"action": action}
    if action == "decompose" and goal_id:
        r = await decompose_goal(pool=pool, publisher=publisher, llm=llm, scope=scope,
                                 goal_id=goal_id, guidance=message)
        meta["result"] = r
        reply = (f"I couldn't build a plan: {r['error']}" if r.get("error")
                 else f"I broke this goal into {r['task_count']} tasks. Review them and say "
                      f"'approve' to let me start.")
    elif action == "approve" and goal_id:
        plan = await _current_plan(pool, scope, goal_id)
        if plan and plan["status"] == "proposed":
            r = await approve_plan(pool=pool, publisher=publisher, scope=scope, plan_id=plan["id"])
            meta["result"] = r
            reply = reply or "Plan approved. Say 'start' to dispatch the workers."
        else:
            reply = "There's no proposed plan to approve — try 'break this down' first."
    elif action == "dispatch" and goal_id:
        plan = await _current_plan(pool, scope, goal_id)
        if plan and plan["status"] in ("approved", "executing"):
            r = await dispatch_plan(pool=pool, publisher=publisher, runner=runner,
                                    scope=scope, plan_id=plan["id"])
            meta["result"] = r
            reply = reply or f"Dispatched {r['dispatched']} worker(s). Watch them in the runs feed."
        elif plan and plan["status"] == "proposed":
            reply = "Approve the plan first ('approve'), then I'll dispatch the workers."
        else:
            reply = "There's no plan to dispatch yet — try 'break this down' first."
    elif action == "status":
        reply = _status_text(state)
    # action == "none" → reply stands as the LLM's answer

    await _save_message(pool, scope, goal_id, "orchestrator", reply, meta=meta)
    return {"reply": reply, "action": action, "meta": meta}


async def _interpret(llm: Any, message: str, state: dict | None) -> tuple[str, str]:
    """Return (action, reply). LLM-classified, with a keyword fallback."""
    if llm is not None:
        try:
            ctx = "CURRENT STATE:\n" + _status_text(state) if state else "No goal selected."
            text = await llm(CHAT_SYSTEM, f"{ctx}\n\nUSER: {message}")
            data = first_json(text) if text else None
            if isinstance(data, dict) and data.get("action"):
                action = str(data["action"]).lower().strip()
                if action in ("decompose", "approve", "dispatch", "status", "none"):
                    return action, str(data.get("reply", "")).strip()
        except Exception:  # noqa: BLE001
            log.warning("orchestrator: chat interpret failed — using heuristic", exc_info=True)
    return _heuristic_action(message), ""


def _heuristic_action(message: str) -> str:
    m = f" {message.lower()} "
    if any(k in m for k in ("break", "decompose", "plan it", "figure out how",
                            "how should", "make a plan", "re-plan", "replan")):
        return "decompose"
    # status BEFORE dispatch so "how's it going" doesn't match a 'go'-like verb
    if any(k in m for k in ("status", "progress", "how's it", "how is it", "where are we",
                            "update me", "going")):
        return "status"
    if any(k in m for k in ("approve", "looks good", "go ahead", "sounds good", "ok do it")):
        return "approve"
    if any(k in m for k in ("dispatch", "start", "run them", "begin", "execute", "do it now",
                            "kick off", "get going")):
        return "dispatch"
    return "none"


# ── reads / overview ──────────────────────────────────────────────────────────

async def get_plan(pool: Any, scope: str, goal_id: str) -> dict | None:
    """Current (latest non-abandoned) plan + its tasks for a goal."""
    plan = await _current_plan(pool, scope, goal_id)
    if plan is None:
        return None
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, seq, title, task, why, status, run_id, result_summary, updated_at "
                "FROM goal_tasks WHERE plan_id=%s ORDER BY seq",
                (plan["id"],),
            )
            cols = [c.name for c in cur.description]
            tasks = [dict(zip(cols, r)) for r in await cur.fetchall()]
    for t in tasks:
        t["updated_at"] = t["updated_at"].isoformat() if t["updated_at"] else None
    return {**plan, "tasks": tasks}


async def overview(pool: Any, scope: str) -> dict:
    """Everything the command center shows: goals under execution with progress,
    active worker runs, and the pending-approval count."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT g.id, g.title, g.status, g.progress, "
                "  p.id, p.status, "
                "  count(t.id), "
                "  count(*) FILTER (WHERE t.status='completed'), "
                "  count(*) FILTER (WHERE t.status IN ('running','dispatched','parked')) "
                "FROM goal_plans p "
                "JOIN goals g ON g.id = p.goal_id "
                "LEFT JOIN goal_tasks t ON t.plan_id = p.id "
                "WHERE p.scope=%s AND p.status <> 'abandoned' "
                "  AND p.created_at = (SELECT max(created_at) FROM goal_plans p2 "
                "                      WHERE p2.goal_id = p.goal_id AND p2.status <> 'abandoned') "
                "GROUP BY g.id, g.title, g.status, g.progress, p.id, p.status "
                "ORDER BY g.progress ASC",
                (scope,),
            )
            goals = [
                {"goal_id": r[0], "title": r[1], "goal_status": r[2], "progress": r[3],
                 "plan_id": r[4], "plan_status": r[5], "total_tasks": r[6],
                 "completed_tasks": r[7], "active_tasks": r[8]}
                for r in await cur.fetchall()
            ]
            await cur.execute(
                "SELECT id, task, status, goal_id, created_at FROM agent_runs "
                "WHERE scope=%s AND goal_id IS NOT NULL AND status IN ('running','parked') "
                "ORDER BY created_at DESC LIMIT 20",
                (scope,),
            )
            active = [
                {"run_id": r[0], "task": r[1], "status": r[2], "goal_id": r[3],
                 "created_at": r[4].isoformat() if r[4] else None}
                for r in await cur.fetchall()
            ]
            await cur.execute(
                "SELECT count(*) FROM approvals WHERE state='pending'"
            )
            pending = (await cur.fetchone())[0]
    return {"goals": goals, "active_runs": active, "pending_approvals": pending}


async def list_messages(pool: Any, scope: str, goal_id: str | None, limit: int = 100) -> list[dict]:
    where = ["scope=%s"]
    params: list = [scope]
    if goal_id:
        where.append("goal_id=%s")
        params.append(goal_id)
    else:
        where.append("goal_id IS NULL")
    params.append(min(limit, 300))
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, role, content, meta, created_at FROM orchestrator_messages "
                f"WHERE {' AND '.join(where)} ORDER BY created_at DESC LIMIT %s",
                params,
            )
            rows = await cur.fetchall()
    return [
        {"id": r[0], "role": r[1], "content": r[2], "meta": r[3],
         "created_at": r[4].isoformat() if r[4] else None}
        for r in reversed(rows)
    ]


# ── helpers ───────────────────────────────────────────────────────────────────

async def _load_goal(pool, scope, goal_id) -> dict | None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, title, description, success_criteria, horizon, status, progress "
                "FROM goals WHERE id=%s AND scope=%s AND valid_to IS NULL",
                (goal_id, scope),
            )
            r = await cur.fetchone()
    if r is None:
        return None
    return {"id": r[0], "title": r[1], "description": r[2], "success_criteria": r[3],
            "horizon": r[4], "status": r[5], "progress": r[6]}


def _goal_block(goal: dict, guidance: str | None) -> str:
    parts = [f"GOAL: {goal['title']}"]
    if goal.get("description"):
        parts.append(f"DESCRIPTION: {goal['description']}")
    if goal.get("success_criteria"):
        parts.append(f"SUCCESS CRITERIA: {goal['success_criteria']}")
    if goal.get("horizon"):
        parts.append(f"HORIZON: {goal['horizon']}")
    if guidance:
        parts.append(f"USER GUIDANCE: {guidance}")
    return "\n".join(parts)


async def _load_task(pool, scope, task_id) -> dict | None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT t.id, t.plan_id, t.goal_id, t.task, t.status, t.run_id, p.status "
                "FROM goal_tasks t JOIN goal_plans p ON p.id = t.plan_id "
                "WHERE t.id=%s AND t.scope=%s",
                (task_id, scope),
            )
            r = await cur.fetchone()
    if r is None:
        return None
    return {"id": r[0], "plan_id": r[1], "goal_id": r[2], "task": r[3],
            "status": r[4], "run_id": r[5], "plan_status": r[6]}


async def _current_plan(pool, scope, goal_id) -> dict | None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, goal_id, status, rationale, created_at FROM goal_plans "
                "WHERE goal_id=%s AND scope=%s AND status <> 'abandoned' "
                "ORDER BY created_at DESC LIMIT 1",
                (goal_id, scope),
            )
            r = await cur.fetchone()
    if r is None:
        return None
    return {"id": r[0], "goal_id": r[1], "status": r[2], "rationale": r[3],
            "created_at": r[4].isoformat() if r[4] else None}


async def _goal_state(pool, scope, goal_id) -> dict | None:
    goal = await _load_goal(pool, scope, goal_id)
    if goal is None:
        return None
    plan = await get_plan(pool, scope, goal_id)
    return {"goal": goal, "plan": plan}


def _status_text(state: dict | None) -> str:
    if not state:
        return "No goal is selected."
    goal = state["goal"]
    plan = state.get("plan")
    if not plan:
        return (f"Goal '{goal['title']}' has no execution plan yet. "
                f"Say 'break this down' and I'll propose one.")
    tasks = plan.get("tasks", [])
    done = sum(1 for t in tasks if t["status"] == "completed")
    running = sum(1 for t in tasks if t["status"] in ("running", "dispatched", "parked"))
    return (f"Goal '{goal['title']}' — plan is {plan['status']}, "
            f"{done}/{len(tasks)} tasks done"
            + (f", {running} in progress" if running else "")
            + f" (progress {round((goal.get('progress') or 0) * 100)}%).")


async def _save_message(pool, scope, goal_id, role, content, meta: dict | None = None) -> None:
    from psycopg.types.json import Jsonb
    from shared.types.ulid import mint
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO orchestrator_messages (id, scope, goal_id, role, content, meta) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (mint("omsg"), scope, goal_id, role, content[:8000], Jsonb(meta or {})),
            )


async def _emit(pool, publisher, scope, short_type, subject, data: dict) -> None:
    if publisher is None:
        return
    try:
        from agent.pdp_gate import scope_parts
        from shared.events import build_event
        ev = build_event(short_type=short_type, subject=subject, scope=scope_parts(scope),
                         data=data, actor="system", source="curlyos-core/orchestrator")
        async with pool.connection() as conn:
            await publisher.stage(ev, conn)
    except Exception:  # noqa: BLE001
        log.warning("orchestrator: event emit failed (%s)", short_type, exc_info=True)


def _extract_summary(result: Any) -> str | None:
    """Reuse the jobs delivery extractor — pull synthesized text from a run result."""
    from orchestration.user_jobs import _extract_summary as _ex
    return _ex(result)
