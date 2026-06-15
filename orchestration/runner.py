"""The run executor — owns agent_runs row lifecycle, bounded concurrency,
drain-on-shutdown, and startup recovery (curlyos-final/06).

ONE resume primitive: `Runner.resume(run_id)` invokes the checkpointed graph
with Command(resume=...) on thread_id=run_id. The approvals grant/deny API and
the admin endpoint both call it; nothing else resumes runs.

Run-row state machine (runner-owned; the graph owns cognition):
    running → parked (interrupt) → running (resume) → completed | failed
    running → cancelled (cancel_parked / drain refusal)
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

log = logging.getLogger("curlyos-core.orchestration.runner")


class Runner:
    def __init__(
        self,
        *,
        dsn: str,
        scope: str,
        pool_factory: Callable[[], Awaitable[Any]],
        publisher_factory: Callable[[], Any],
        redis_factory: Callable[[], Any],
        embedder_factory: Callable[[], Awaitable[Any]],
        llm: Callable[[str, str], Awaitable[str]] | None,
        notifier: Any,
        max_concurrent: int = 2,
        on_run_event: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self.dsn = dsn
        self.scope = scope
        self._pool_factory = pool_factory
        self._publisher_factory = publisher_factory
        self._redis_factory = redis_factory
        self._embedder_factory = embedder_factory
        self._llm = llm
        self._notifier = notifier
        # Optional hook: called as on_run_event(run_id, status) when a run parks
        # or reaches a terminal state. Lets a higher layer (scheduled jobs)
        # deliver output without the runner knowing anything about it. Never
        # allowed to break a run — callers wrap their own bodies defensively.
        self._on_run_event = on_run_event
        self._sem = asyncio.Semaphore(max_concurrent)
        self._tasks: dict[str, asyncio.Task] = {}
        self._graph: Any = None
        self._saver_pool: Any = None
        self._draining = False

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self) -> None:
        import psycopg_pool
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row

        from orchestration.graph import make_graph
        from orchestration.tools import ToolDeps

        # the checkpointer gets its own small pool (one shared connection is
        # not safe across concurrent runs); kwargs per checkpoint-postgres docs
        self._saver_pool = psycopg_pool.AsyncConnectionPool(
            self.dsn, min_size=1, max_size=4, open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        await self._saver_pool.open()
        saver = AsyncPostgresSaver(self._saver_pool)
        await saver.setup()

        async def get_deps(run_id: str, scope: str) -> ToolDeps:
            return ToolDeps(
                pool=await self._pool_factory(),
                publisher=self._publisher_factory(),
                redis=self._redis_factory(),
                notifier=self._notifier,
                scope=scope,
                run_id=run_id,
                embedder_factory=self._embedder_factory,
            )

        self._graph = make_graph(get_deps, self._llm, saver)
        await self._recover()
        log.info("runner started (max_concurrent=%d, llm=%s)",
                 self._sem._value, bool(self._llm))  # noqa: SLF001

    async def stop(self) -> None:
        """Drain: stop accepting, let in-flight node boundaries checkpoint,
        then cancel. Parked/running runs recover at next start()."""
        self._draining = True
        for t in self._tasks.values():
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        if getattr(self, "_saver_pool", None) is not None:
            try:
                await self._saver_pool.close()
            except Exception:  # noqa: BLE001
                pass

    async def _recover(self) -> None:
        """Runs left 'running' by a crash resume from their last checkpoint;
        'parked' runs stay parked (they're waiting on a human, not on us)."""
        pool = await self._pool_factory()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id FROM agent_runs WHERE agent = 'Executive' AND status = 'running'",
                )
                stuck = [r[0] for r in await cur.fetchall()]
        for run_id in stuck:
            log.info("recovering interrupted run %s", run_id)
            self._spawn(run_id, None)  # None input → continue from checkpoint

    # ── public API ───────────────────────────────────────────────────────────
    async def start_run(self, task: str, *, source: str = "api",
                        goal_id: str | None = None, autonomy: str | None = None) -> str:
        if self._draining:
            raise RuntimeError("runner is draining — try again after restart")
        from shared.events import build_event
        from shared.types.ulid import mint

        from agent.pdp_gate import scope_parts

        # Resolve the run's autonomy. None → read the global bypass toggle:
        # bypass on → full_auto (side effects auto-allow, except the hard floors
        # self_modify / memory_forget_hard / kill-switch which the PDP keeps).
        if autonomy is None:
            autonomy = await self._resolve_autonomy()

        run_id = mint("run")
        pool = await self._pool_factory()
        pub = self._publisher_factory()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO agent_runs (id, agent, scope, task, status, autonomy_level, goal_id) "
                    "VALUES (%s, 'Executive', %s, %s, 'running', %s, %s)",
                    (run_id, self.scope, task[:2000], autonomy, goal_id),
                )
            ev = build_event(
                short_type="agent.run.started", subject=run_id,
                scope=scope_parts(self.scope),
                data={"run_id": run_id, "agent": "Executive", "source": source,
                      "task": task[:300]},
                actor="system", source="curlyos-core/runner",
            )
            await pub.stage(ev, conn)
        self._spawn(run_id, {"run_id": run_id, "scope": self.scope, "task": task,
                             "history": [], "decision": None, "autonomy": autonomy})
        return run_id

    async def _resolve_autonomy(self) -> str:
        """confirm_each by default; full_auto when the global bypass toggle is on."""
        try:
            from shared.settings import AGENT_BYPASS, get_setting
            pool = await self._pool_factory()
            return "full_auto" if await get_setting(pool, AGENT_BYPASS, False) else "confirm_each"
        except Exception:  # noqa: BLE001 — never block a run on a settings read
            return "confirm_each"

    async def resume(self, run_id: str) -> bool:
        """Wake a parked run (after grant OR deny — the act node reads the
        approval's state and proceeds accordingly). Returns False if the run
        isn't parked."""
        from langgraph.types import Command

        pool = await self._pool_factory()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE agent_runs SET status = 'running' "
                    "WHERE id = %s AND status = 'parked' RETURNING id",
                    (run_id,),
                )
                if await cur.fetchone() is None:
                    return False
        self._spawn(run_id, Command(resume="wake"))
        return True

    async def cancel(self, run_id: str, reason: str = "user_cancelled") -> bool:
        t = self._tasks.get(run_id)
        if t:
            t.cancel()
        pool = await self._pool_factory()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE agent_runs SET status = 'cancelled', error = %s, finished_at = now() "
                    "WHERE id = %s AND status IN ('running','parked') RETURNING id",
                    (reason, run_id),
                )
                return await cur.fetchone() is not None

    # ── drive ────────────────────────────────────────────────────────────────
    def _spawn(self, run_id: str, graph_input: Any) -> None:
        task = asyncio.create_task(self._drive(run_id, graph_input),
                                   name=f"run-{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(run_id, None))

    async def _drive(self, run_id: str, graph_input: Any) -> None:
        cfg = {"configurable": {"thread_id": run_id}, "recursion_limit": 50}
        async with self._sem:
            try:
                result = await self._graph.ainvoke(graph_input, config=cfg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("run %s failed", run_id)
                await self._finish(run_id, "failed", None, f"{type(exc).__name__}: {exc}")
                return

        if "__interrupt__" in result:
            intr = result["__interrupt__"][0].value if result["__interrupt__"] else {}
            await self._park(run_id, intr)
            return
        await self._finish(run_id, "completed", result.get("decision"), None)

    async def _park(self, run_id: str, intr: dict) -> None:
        pool = await self._pool_factory()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE agent_runs SET status = 'parked' "
                    "WHERE id = %s AND status = 'running'", (run_id,),
                )
        apv_id = intr.get("apv_id")
        try:
            await self._notifier.notify(
                f"CurlyOS run {run_id} needs approval: {intr.get('tool')} "
                f"({intr.get('action_class')}) — reply 'approve {apv_id}' or "
                f"'deny {apv_id}' here, or decide in Mission Control.",
                approval_id=apv_id, run_id=run_id,
            )
        except Exception:  # noqa: BLE001
            log.warning("park notification failed for %s", run_id)
        await self._emit_run_event(run_id, "parked")
        log.info("run %s parked on %s", run_id, apv_id)

    async def _finish(self, run_id: str, status: str, decision: dict | None,
                      error: str | None) -> None:
        from psycopg.types.json import Jsonb

        from agent.pdp_gate import scope_parts
        from shared.events import build_event

        pool = await self._pool_factory()
        pub = self._publisher_factory()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE agent_runs SET status = %s, result = %s, error = %s, "
                    "finished_at = now() WHERE id = %s",
                    (status, Jsonb(decision) if decision else None, error, run_id),
                )
            ev = build_event(
                short_type=f"agent.run.{status if status == 'completed' else 'failed'}",
                subject=run_id, scope=scope_parts(self.scope),
                data={"run_id": run_id, "agent": "Executive",
                      **({"error": (error or '')[:500]} if error else {}),
                      **({"summary": decision.get("summary", "")[:500]} if decision else {})},
                actor="system", source="curlyos-core/runner",
            )
            await pub.stage(ev, conn)
        if error and self._notifier is not None:
            try:
                await self._notifier.notify(f"CurlyOS run {run_id} failed: {error[:300]}",
                                            run_id=run_id)
            except Exception:  # noqa: BLE001
                pass
        await self._emit_run_event(run_id, status)

    async def _emit_run_event(self, run_id: str, status: str) -> None:
        """Fire the optional run-event hook, isolating its failures."""
        if self._on_run_event is None:
            return
        try:
            await self._on_run_event(run_id, status)
        except Exception:  # noqa: BLE001 — a hook must never break the run loop
            log.exception("run-event hook failed for %s (%s)", run_id, status)

    # ── observability ────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        return {"in_flight": sorted(self._tasks.keys()), "draining": self._draining,
                "llm": bool(self._llm)}
