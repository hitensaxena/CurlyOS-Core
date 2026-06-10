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


class CouncilRequest(BaseModel):
    pass  # no body — the decision row is the input


class DenyReason(BaseModel):
    reason: str = Field(default="", max_length=500)


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

    return router
