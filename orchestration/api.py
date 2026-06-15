"""Agents API — runs, inbound tasks, and the SSE event stream.

Router-factory pattern (set by goals/api.py): api_server builds this with its
shared helpers; the runner instance is resolved from app.state at request time
(it's created in the lifespan, after routers are included).
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field


class StartRunRequest(BaseModel):
    task: str = Field(min_length=1, max_length=4000)


class InboundRequest(BaseModel):
    task: str = Field(min_length=1, max_length=4000)
    source: str = Field(default="hermes", max_length=50)
    session_ref: str | None = Field(default=None, max_length=200)


class ProposePromptRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=20, max_length=20_000)
    notes: str = Field(default="", max_length=1000)


class ActivatePromptRequest(BaseModel):
    approval_id: str = Field(min_length=5, max_length=60)


class DenyReason(BaseModel):
    reason: str = Field(default="", max_length=500)


_CADENCE_RE = "^(every|daily_at|weekly_at|monthly_at)$"


class ScheduledJobCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    task: str = Field(min_length=1, max_length=4000)
    cadence_type: str = Field(pattern=_CADENCE_RE)
    cadence_json: dict = Field(default_factory=dict)
    enabled: bool = True


class ScheduledJobUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    task: str | None = Field(default=None, max_length=4000)
    cadence_type: str | None = Field(default=None, pattern=_CADENCE_RE)
    cadence_json: dict | None = None
    enabled: bool | None = None


class DecomposeRequest(BaseModel):
    guidance: str | None = Field(default=None, max_length=2000)


class OrchestratorChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    goal_id: str | None = Field(default=None, max_length=60)
    project_id: str | None = Field(default=None, max_length=60)


class BypassRequest(BaseModel):
    enabled: bool


def make_router(
    *,
    pool_factory: Callable[[], Awaitable[Any]],
    scope: str,
    publisher_factory: Callable[[], Any] | None = None,
    redis_factory: Callable[[], Any] | None = None,
    embedder_factory: Callable[[], Awaitable[Any]] | None = None,
    llm_factory: Callable[[], Any] | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api")

    async def _workflow_deps() -> dict:
        return {
            "pool": await pool_factory(),
            "publisher": publisher_factory() if publisher_factory else None,
            "redis": redis_factory() if redis_factory else None,
            "embedder": (await embedder_factory()) if embedder_factory else None,
            "llm": llm_factory() if llm_factory else None,
            "scope": scope,
        }

    # ── exploration workflows (Phase X) ──────────────────────────────────────
    @router.post("/discovery/scan")
    async def discovery_scan_now():
        """Manual trigger of the opportunity scan (also scheduled weekly)."""
        from orchestration.workflows import discovery_scan

        d = await _workflow_deps()
        result = await discovery_scan(**d)
        if result.get("error"):
            raise HTTPException(503, result["error"])
        return result

    @router.post("/simulation/runs/{sim_id}/execute")
    async def execute_simulation(sim_id: str):
        """Run a created simulation: scenarios + possible_world memories in a
        scenario:<id> scope (invisible to default recall, never promoted)."""
        from orchestration.workflows import run_simulation

        d = await _workflow_deps()
        result = await run_simulation(**d, sim_id=sim_id)
        if result.get("error"):
            code = 404 if "not found" in result["error"] else (
                409 if "already" in result["error"] else 503)
            raise HTTPException(code, result["error"])
        return result

    @router.post("/decisions/{dec_id}/council")
    async def council_decision(dec_id: str):
        """Stress-test a decision with a 4-perspective council; the synthesis
        lands on the decision row (properties.council)."""
        from orchestration.workflows import council

        d = await _workflow_deps()
        result = await council(pool=d["pool"], publisher=d["publisher"],
                               llm=d["llm"], scope=scope, dec_id=dec_id)
        if result.get("error"):
            raise HTTPException(404 if "not found" in result["error"] else 503,
                                result["error"])
        return result

    def _runner(request: Request):
        runner = getattr(request.app.state, "runner", None)
        if runner is None:
            raise HTTPException(503, "runner disabled (CURLYOS_RUNNER=0 or failed to start)")
        return runner

    # ── evolution (Phase E) ──────────────────────────────────────────────────
    @router.get("/evolution/prompts")
    async def evolution_prompts(name: str | None = None):
        from orchestration.evolution import list_prompt_versions

        pool = await pool_factory()
        items = await list_prompt_versions(pool, scope, name)
        return {"items": items, "count": len(items)}

    @router.post("/evolution/prompts")
    async def evolution_propose(body: ProposePromptRequest):
        from orchestration.evolution import propose_prompt

        pool = await pool_factory()
        return await propose_prompt(
            pool, publisher_factory() if publisher_factory else None, scope,
            name=body.name, content=body.content, notes=body.notes,
            proposed_by="manual",
        )

    @router.post("/evolution/prompts/{pmt_id}/evaluate")
    async def evolution_evaluate(pmt_id: str):
        from orchestration.evolution import evaluate_prompt

        pool = await pool_factory()
        result = await evaluate_prompt(
            pool, publisher_factory() if publisher_factory else None, scope,
            pmt_id=pmt_id, llm=llm_factory() if llm_factory else None,
        )
        if result.get("error"):
            raise HTTPException(404 if "not found" in result["error"] else 503,
                                result["error"])
        return result

    @router.post("/evolution/prompts/{pmt_id}/activate")
    async def evolution_activate(pmt_id: str, body: ActivatePromptRequest):
        """Both gates via the real PDP: eval pass AND granted self_modify
        approval (create one via POST /api/approvals, grant it, pass its id)."""
        from orchestration.evolution import activate_prompt

        pool = await pool_factory()
        result = await activate_prompt(
            pool, publisher_factory() if publisher_factory else None,
            redis_factory() if redis_factory else None, scope,
            pmt_id=pmt_id, approval_id=body.approval_id,
        )
        if result.get("error"):
            raise HTTPException(404 if "not found" in result["error"] else 409,
                                result["error"])
        return result

    @router.get("/evolution/timeline")
    async def evolution_timeline(limit: int = 50):
        """The /evolution page feed: evolution.* events, newest first."""
        pool = await pool_factory()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT seq, type, subject, data, created_at FROM events "
                    "WHERE type LIKE %s ORDER BY seq DESC LIMIT %s",
                    ("%curlyos.evolution.%", min(limit, 200)),
                )
                rows = await cur.fetchall()
        return {"items": [
            {"seq": r[0], "type": r[1].split("curlyos.")[-1], "subject": r[2],
             "data": r[3], "at": r[4].isoformat() if r[4] else None}
            for r in rows
        ]}

    # ── runs ─────────────────────────────────────────────────────────────────
    @router.post("/agents/runs")
    async def start_run(body: StartRunRequest, request: Request):
        run_id = await _runner(request).start_run(body.task, source="api")
        return {"run_id": run_id, "status": "running"}

    @router.post("/agents/inbound")
    async def inbound(body: InboundRequest, request: Request):
        """Hermes-facing task intake. Hermes sessions never hold run state —
        the run id is the only thing that crosses the boundary."""
        run_id = await _runner(request).start_run(
            body.task, source=f"{body.source}:{body.session_ref or ''}".rstrip(":"),
        )
        return {"run_id": run_id, "status": "running"}

    @router.get("/agents/runs")
    async def list_runs(status: str | None = None, agent: str | None = None,
                        limit: int = 50):
        pool = await pool_factory()
        where, params = ["scope = %s"], [scope]
        if status:
            where.append("status = %s")
            params.append(status)
        if agent:
            where.append("agent LIKE %s")
            params.append(agent + "%")
        params.append(min(limit, 200))
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, agent, task, status, result, error, created_at, finished_at "
                    f"FROM agent_runs WHERE {' AND '.join(where)} "
                    "ORDER BY created_at DESC LIMIT %s",
                    params,
                )
                rows = await cur.fetchall()
        return {"items": [
            {"id": r[0], "agent": r[1], "task": r[2], "status": r[3],
             "result": r[4], "error": r[5],
             "created_at": r[6].isoformat() if r[6] else None,
             "finished_at": r[7].isoformat() if r[7] else None}
            for r in rows
        ], "count": len(rows)}

    @router.get("/agents/runs/{run_id}")
    async def get_run(run_id: str):
        """The full execution trace: run + actions→observations + tool_calls
        chain + approvals — the /runs/[id] payload."""
        pool = await pool_factory()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, agent, task, status, result, error, created_at, finished_at "
                    "FROM agent_runs WHERE id = %s AND scope = %s",
                    (run_id, scope),
                )
                run = await cur.fetchone()
                if run is None:
                    raise HTTPException(404, f"run {run_id!r} not found")
                await cur.execute(
                    "SELECT a.id, a.kind, a.payload, a.created_at, o.result "
                    "FROM actions a LEFT JOIN observations o ON o.action_id = a.id "
                    "WHERE a.run_id = %s ORDER BY a.created_at",
                    (run_id,),
                )
                actions = await cur.fetchall()
                await cur.execute(
                    "SELECT tc.id, tc.tool, tc.args, encode(tc.entry_hash, 'hex'), tc.created_at "
                    "FROM tool_calls tc JOIN actions a ON tc.action_id = a.id "
                    "WHERE a.run_id = %s ORDER BY tc.created_at",
                    (run_id,),
                )
                tool_calls = await cur.fetchall()
                await cur.execute(
                    "SELECT id, action_class, payload, state, origin, created_at, decided_at "
                    "FROM approvals WHERE run_id = %s ORDER BY created_at",
                    (run_id,),
                )
                approvals = await cur.fetchall()
        return {
            "id": run[0], "agent": run[1], "task": run[2], "status": run[3],
            "result": run[4], "error": run[5],
            "created_at": run[6].isoformat() if run[6] else None,
            "finished_at": run[7].isoformat() if run[7] else None,
            "actions": [
                {"id": a[0], "kind": a[1], "payload": a[2],
                 "created_at": a[3].isoformat() if a[3] else None,
                 "observation": a[4]}
                for a in actions
            ],
            "tool_calls": [
                {"id": t[0], "tool": t[1], "args": t[2], "entry_hash": t[3],
                 "created_at": t[4].isoformat() if t[4] else None}
                for t in tool_calls
            ],
            "approvals": [
                {"apv_id": p[0], "action_class": p[1], "payload": p[2], "state": p[3],
                 "origin": p[4],
                 "created_at": p[5].isoformat() if p[5] else None,
                 "decided_at": p[6].isoformat() if p[6] else None}
                for p in approvals
            ],
        }

    @router.post("/agents/runs/{run_id}/resume")
    async def resume_run(run_id: str, request: Request):
        ok = await _runner(request).resume(run_id)
        if not ok:
            raise HTTPException(409, f"run {run_id!r} is not parked")
        return {"run_id": run_id, "status": "running"}

    @router.post("/agents/runs/{run_id}/cancel")
    async def cancel_run(run_id: str, request: Request):
        ok = await _runner(request).cancel(run_id)
        if not ok:
            raise HTTPException(409, f"run {run_id!r} is not running or parked")
        return {"run_id": run_id, "status": "cancelled"}

    # ── SSE ──────────────────────────────────────────────────────────────────
    @router.get("/events/stream")
    async def events_stream(request: Request, types: str | None = None,
                            last_seq: int = 0):
        """Server-sent events over the events table (2s poll — the webapp's
        live-update feed). `types` = comma-separated short-type prefixes."""
        prefixes = tuple(f"art.curlybrackets.curlyos.{p.strip()}"
                         for p in types.split(",")) if types else None

        async def gen():
            seq = last_seq
            if seq == 0:  # start at the tip — SSE is live-updates, not history
                pool0 = await pool_factory()
                async with pool0.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute("SELECT COALESCE(max(seq), 0) FROM events")
                        seq = (await cur.fetchone())[0]
            yield f"event: hello\ndata: {json.dumps({'seq': seq})}\n\n"
            while not await request.is_disconnected():
                pool = await pool_factory()
                async with pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT seq, type, subject, data, created_at FROM events "
                            "WHERE seq > %s ORDER BY seq LIMIT 100",
                            (seq,),
                        )
                        rows = await cur.fetchall()
                for r in rows:
                    seq = r[0]
                    if prefixes and not str(r[1]).startswith(prefixes):
                        continue
                    payload = {"seq": r[0], "type": r[1], "subject": r[2], "data": r[3],
                               "at": r[4].isoformat() if r[4] else None}
                    yield f"id: {seq}\ndata: {json.dumps(payload, default=str)}\n\n"
                yield ": keepalive\n\n"
                await asyncio.sleep(2)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    # ── scheduled (user-defined) jobs ─────────────────────────────────────────
    #
    # Persisted in scheduled_jobs; each becomes a live scheduler.Job that routes
    # its NL task through the Executive agent and delivers output to the inbox.

    _JOB_COLS = ("id", "name", "task", "cadence_type", "cadence_json", "delivery",
                 "enabled", "last_fired", "last_status", "last_run_id", "last_error",
                 "created_at", "updated_at")

    def _scheduler(request: Request):
        return getattr(request.app.state, "scheduler", None)

    def _job_dict(r: tuple, sched) -> dict:
        from orchestration.user_jobs import cadence_display, find_job
        d = dict(zip(_JOB_COLS, r))
        for k in ("last_fired", "created_at", "updated_at"):
            d[k] = d[k].isoformat() if d[k] else None
        d["cadence_display"] = cadence_display(d["cadence_type"], d["cadence_json"])
        live = find_job(sched, d["id"]) if sched is not None else None
        d["next_due"] = live.next_due.isoformat() if live and live.next_due else None
        d["registered"] = live is not None
        return d

    async def _fetch_job(pool, job_id: str) -> tuple | None:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {', '.join(_JOB_COLS)} FROM scheduled_jobs "
                    "WHERE id = %s AND scope = %s",
                    (job_id, scope),
                )
                return await cur.fetchone()

    def _register_live(request: Request, row: tuple) -> None:
        """Best-effort live (re)registration. If the scheduler isn't up the row
        still persists and loads at next boot."""
        from orchestration.user_jobs import build_job, register_job
        sched = _scheduler(request)
        if sched is None:
            return
        d = dict(zip(_JOB_COLS, row))
        if not d["enabled"]:
            from orchestration.user_jobs import unregister_job
            unregister_job(sched, d["id"])
            return
        job = build_job(
            {"id": d["id"], "scope": scope, "name": d["name"], "task": d["task"],
             "cadence_type": d["cadence_type"], "cadence_json": d["cadence_json"],
             "enabled": d["enabled"]},
            get_runner=lambda: getattr(request.app.state, "runner", None),
            pool_factory=pool_factory,
        )
        register_job(sched, job)

    @router.post("/scheduled-jobs")
    async def create_job(body: ScheduledJobCreate, request: Request):
        from orchestration.user_jobs import parse_cadence
        from shared.types.ulid import mint

        try:  # validate cadence shape up front
            parse_cadence(body.cadence_type, body.cadence_json)
        except (ValueError, KeyError) as e:
            raise HTTPException(400, f"invalid cadence: {e}")

        from psycopg.types.json import Jsonb
        job_id = mint("sjob")
        pool = await pool_factory()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                try:
                    await cur.execute(
                        "INSERT INTO scheduled_jobs "
                        "(id, scope, name, task, cadence_type, cadence_json, enabled) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (job_id, scope, body.name, body.task, body.cadence_type,
                         Jsonb(body.cadence_json), body.enabled),
                    )
                except Exception as e:  # noqa: BLE001 — likely the (scope,name) unique
                    if "scheduled_jobs_scope_name" in str(e) or "duplicate key" in str(e):
                        raise HTTPException(409, f"a job named {body.name!r} already exists")
                    raise
        row = await _fetch_job(pool, job_id)
        _register_live(request, row)
        return _job_dict(row, _scheduler(request))

    @router.get("/scheduled-jobs")
    async def list_jobs(request: Request):
        pool = await pool_factory()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {', '.join(_JOB_COLS)} FROM scheduled_jobs "
                    "WHERE scope = %s ORDER BY created_at DESC",
                    (scope,),
                )
                rows = await cur.fetchall()
        sched = _scheduler(request)
        return {"items": [_job_dict(r, sched) for r in rows], "count": len(rows)}

    @router.get("/scheduled-jobs/{job_id}")
    async def get_job(job_id: str, request: Request):
        pool = await pool_factory()
        row = await _fetch_job(pool, job_id)
        if row is None:
            raise HTTPException(404, f"job {job_id!r} not found")
        return _job_dict(row, _scheduler(request))

    @router.patch("/scheduled-jobs/{job_id}")
    async def update_job(job_id: str, body: ScheduledJobUpdate, request: Request):
        from orchestration.user_jobs import parse_cadence
        from psycopg.types.json import Jsonb

        pool = await pool_factory()
        existing = await _fetch_job(pool, job_id)
        if existing is None:
            raise HTTPException(404, f"job {job_id!r} not found")
        cur_d = dict(zip(_JOB_COLS, existing))

        # Resolve the post-update cadence and validate it.
        new_type = body.cadence_type or cur_d["cadence_type"]
        new_json = body.cadence_json if body.cadence_json is not None else cur_d["cadence_json"]
        if body.cadence_type is not None or body.cadence_json is not None:
            try:
                parse_cadence(new_type, new_json)
            except (ValueError, KeyError) as e:
                raise HTTPException(400, f"invalid cadence: {e}")

        sets, params = [], []
        for col, val in (("name", body.name), ("task", body.task),
                         ("enabled", body.enabled)):
            if val is not None:
                sets.append(f"{col} = %s")
                params.append(val)
        if body.cadence_type is not None:
            sets.append("cadence_type = %s"); params.append(body.cadence_type)
        if body.cadence_json is not None:
            sets.append("cadence_json = %s"); params.append(Jsonb(body.cadence_json))
        if not sets:
            return _job_dict(existing, _scheduler(request))
        sets.append("updated_at = now()")
        params += [job_id, scope]
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"UPDATE scheduled_jobs SET {', '.join(sets)} "
                    "WHERE id = %s AND scope = %s",
                    params,
                )
        row = await _fetch_job(pool, job_id)
        _register_live(request, row)  # rebuilds the live job from the new row
        return _job_dict(row, _scheduler(request))

    @router.delete("/scheduled-jobs/{job_id}")
    async def delete_job(job_id: str, request: Request):
        from orchestration.user_jobs import unregister_job
        pool = await pool_factory()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM scheduled_jobs WHERE id = %s AND scope = %s RETURNING id",
                    (job_id, scope),
                )
                if await cur.fetchone() is None:
                    raise HTTPException(404, f"job {job_id!r} not found")
        sched = _scheduler(request)
        if sched is not None:
            unregister_job(sched, job_id)
        return {"id": job_id, "deleted": True}

    @router.post("/scheduled-jobs/{job_id}/run-now")
    async def run_job_now(job_id: str, request: Request):
        """Fire a job immediately (off-cadence): runs the same fn, delivers to
        the inbox, updates last_*. Returns once the run has been STARTED."""
        from orchestration.user_jobs import make_job_fn
        pool = await pool_factory()
        row = await _fetch_job(pool, job_id)
        if row is None:
            raise HTTPException(404, f"job {job_id!r} not found")
        d = dict(zip(_JOB_COLS, row))
        fn = make_job_fn(
            job_id=d["id"], scope=scope, name=d["name"], task=d["task"],
            get_runner=lambda: getattr(request.app.state, "runner", None),
            pool_factory=pool_factory,
        )
        # Fire-and-forget: the fn polls the Executive run to completion itself.
        asyncio.create_task(fn(), name=f"run-now-{job_id}")
        return {"id": job_id, "status": "started"}

    # ── delivery inbox ────────────────────────────────────────────────────────

    @router.get("/inbox")
    async def list_inbox(unread: bool = False, job: str | None = None, limit: int = 100):
        pool = await pool_factory()
        where, params = ["i.scope = %s"], [scope]
        if unread:
            where.append("i.read_at IS NULL")
        if job:
            where.append("i.job_id = %s")
            params.append(job)
        params.append(min(limit, 300))
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT i.id, i.job_id, j.name, i.run_id, i.title, i.body, i.meta, "
                    "i.read_at, i.created_at "
                    "FROM inbox_items i LEFT JOIN scheduled_jobs j ON j.id = i.job_id "
                    f"WHERE {' AND '.join(where)} "
                    "ORDER BY (i.read_at IS NULL) DESC, i.created_at DESC LIMIT %s",
                    params,
                )
                rows = await cur.fetchall()
        return {"items": [
            {"id": r[0], "job_id": r[1], "job_name": r[2], "run_id": r[3], "title": r[4],
             "body": r[5], "meta": r[6], "read": r[7] is not None,
             "read_at": r[7].isoformat() if r[7] else None,
             "created_at": r[8].isoformat() if r[8] else None}
            for r in rows
        ], "count": len(rows)}

    @router.get("/inbox/unread-count")
    async def inbox_unread_count():
        pool = await pool_factory()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT count(*) FROM inbox_items WHERE scope = %s AND read_at IS NULL",
                    (scope,),
                )
                n = (await cur.fetchone())[0]
        return {"unread": n}

    @router.post("/inbox/{item_id}/read")
    async def mark_inbox_read(item_id: str):
        pool = await pool_factory()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE inbox_items SET read_at = COALESCE(read_at, now()) "
                    "WHERE id = %s AND scope = %s RETURNING id",
                    (item_id, scope),
                )
                if await cur.fetchone() is None:
                    raise HTTPException(404, f"inbox item {item_id!r} not found")
        return {"id": item_id, "read": True}

    # ── goal-execution orchestrator ───────────────────────────────────────────
    #
    # Decompose a goal into a plan of tasks (proposed) → approve → dispatch
    # workers (Executive runs tagged with goal_id) → progress aggregates back.

    def _llm():
        return llm_factory() if llm_factory else None

    def _pub():
        return publisher_factory() if publisher_factory else None

    @router.post("/goals/{goal_id}/decompose")
    async def decompose(goal_id: str, body: DecomposeRequest):
        from orchestration.orchestrator import decompose_goal
        result = await decompose_goal(
            pool=await pool_factory(), publisher=_pub(), llm=_llm(),
            scope=scope, goal_id=goal_id, guidance=body.guidance,
        )
        if result.get("error"):
            code = 404 if "not found" in result["error"] else 503
            raise HTTPException(code, result["error"])
        return result

    @router.get("/goals/{goal_id}/plan")
    async def goal_plan(goal_id: str):
        from orchestration.orchestrator import get_plan
        plan = await get_plan(await pool_factory(), scope, goal_id)
        if plan is None:
            return {"plan": None}
        return {"plan": plan}

    @router.post("/goal-plans/{plan_id}/approve")
    async def approve(plan_id: str):
        from orchestration.orchestrator import approve_plan
        result = await approve_plan(
            pool=await pool_factory(), publisher=_pub(), scope=scope, plan_id=plan_id,
        )
        if result.get("error"):
            raise HTTPException(409, result["error"])
        return result

    @router.post("/goal-tasks/{task_id}/dispatch")
    async def dispatch_one(task_id: str, request: Request):
        from orchestration.orchestrator import dispatch_task
        result = await dispatch_task(
            pool=await pool_factory(), publisher=_pub(), runner=_runner(request),
            scope=scope, task_id=task_id,
        )
        if result.get("error") and not result.get("run_id"):
            code = 404 if "not found" in result["error"] else 409
            raise HTTPException(code, result["error"])
        return result

    @router.post("/goal-plans/{plan_id}/dispatch-all")
    async def dispatch_all(plan_id: str, request: Request):
        from orchestration.orchestrator import dispatch_plan
        return await dispatch_plan(
            pool=await pool_factory(), publisher=_pub(), runner=_runner(request),
            scope=scope, plan_id=plan_id,
        )

    @router.get("/orchestrator/overview")
    async def orchestrator_overview():
        from orchestration.orchestrator import overview
        return await overview(await pool_factory(), scope)

    @router.get("/orchestrator/messages")
    async def orchestrator_messages(goal_id: str | None = None,
                                    project_id: str | None = None, limit: int = 100):
        from orchestration.orchestrator import list_messages
        items = await list_messages(await pool_factory(), scope, goal_id,
                                    project_id=project_id, limit=limit)
        return {"items": items, "count": len(items)}

    @router.post("/orchestrator/chat")
    async def orchestrator_chat_route(body: OrchestratorChatRequest, request: Request):
        from orchestration.orchestrator import orchestrator_chat
        runner = getattr(request.app.state, "runner", None)
        result = await orchestrator_chat(
            pool=await pool_factory(), publisher=_pub(), llm=_llm(), runner=runner,
            scope=scope, message=body.message, goal_id=body.goal_id,
            project_id=body.project_id,
        )
        if result.get("error"):
            raise HTTPException(400, result["error"])
        return result

    # ── bypass mode (run agent side effects without approval) ─────────────────

    @router.get("/settings/agent-bypass")
    async def get_bypass():
        from shared.settings import AGENT_BYPASS, get_setting
        on = await get_setting(await pool_factory(), AGENT_BYPASS, False)
        return {"bypass": bool(on)}

    @router.post("/settings/agent-bypass")
    async def set_bypass(body: BypassRequest):
        from shared.settings import AGENT_BYPASS, set_setting
        await set_setting(await pool_factory(), AGENT_BYPASS, bool(body.enabled))
        return {"bypass": bool(body.enabled)}

    @router.get("/settings/auto-plan")
    async def get_autoplan():
        from shared.settings import get_setting
        on = await get_setting(await pool_factory(), "auto_plan", True)
        return {"auto_plan": bool(on)}

    @router.post("/settings/auto-plan")
    async def set_autoplan(body: BypassRequest):
        from shared.settings import set_setting
        await set_setting(await pool_factory(), "auto_plan", bool(body.enabled))
        return {"auto_plan": bool(body.enabled)}

    @router.get("/settings/auto-promote")
    async def get_autopromote():
        from shared.settings import get_setting
        on = await get_setting(await pool_factory(), "auto_promote", True)
        return {"auto_promote": bool(on)}

    @router.post("/settings/auto-promote")
    async def set_autopromote(body: BypassRequest):
        from shared.settings import set_setting
        await set_setting(await pool_factory(), "auto_promote", bool(body.enabled))
        return {"auto_promote": bool(body.enabled)}

    # ── plan execute / artifacts / autoplan ───────────────────────────────────

    @router.post("/goal-plans/{plan_id}/execute")
    async def execute(plan_id: str, request: Request):
        from orchestration.orchestrator import execute_plan
        result = await execute_plan(
            pool=await pool_factory(), publisher=_pub(), runner=_runner(request),
            scope=scope, plan_id=plan_id,
        )
        if result.get("error"):
            code = 404 if "not found" in result["error"] else 409
            raise HTTPException(code, result["error"])
        return result

    @router.get("/goals/{goal_id}/artifacts")
    async def goal_artifacts(goal_id: str):
        from orchestration.orchestrator import get_artifacts
        items = await get_artifacts(await pool_factory(), scope, goal_id)
        return {"items": items, "count": len(items)}

    @router.post("/orchestrator/autoplan")
    async def run_autoplan():
        from orchestration.orchestrator import autoplan_sweep
        return await autoplan_sweep(
            pool=await pool_factory(), publisher=_pub(), llm=_llm(), scope=scope,
        )

    @router.post("/orchestrator/promote")
    async def run_promote():
        """Manually run the opportunity→goal promotion sweep (full lifecycle front)."""
        from orchestration.orchestrator import promote_opportunities_sweep
        return await promote_opportunities_sweep(
            pool=await pool_factory(), publisher=_pub(), scope=scope,
        )

    return router
