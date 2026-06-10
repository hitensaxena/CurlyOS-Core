"""The in-process scheduler — the OS's cognitive heartbeat.

One code-defined job table (registered by api_server's lifespan) drives every
background behavior: consolidation, reflection, meta-audit, narrative,
attention, approval housekeeping. Design contract (curlyos-final/06 §3):

  * Every firing is wrapped in an `agent_runs` row (agent = "workflow:<name>")
    plus agent.run.started/completed/failed events — Mission Control sees
    background cognition with the same trace surface as interactive runs.
  * Per-job single-flight via Redis `lock:sched:<name>` (SET NX EX). With
    Redis absent the scheduler still runs (single process) — locks only guard
    the Hermes-cron overlap window and accidental double-processes.
  * Jobs may carry an OUTPUT-BASED period guard ("a weekly report already
    exists for this ISO week → skip") so double-triggering — by Hermes cron
    during the migration overlap, by manual POSTs, or by restarts — never
    double-spends LLM calls.
  * Failures never kill the loop: the run row records the error, the notifier
    gets one ping, and the job just waits for its next slot.

Cadences are computed in HOST LOCAL TIME (the box runs IST; the Hermes cron
entries being replaced were local too).
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

log = logging.getLogger("curlyos-core.scheduler")

_LOCK_TTL_S = 3600  # a job firing holds its lock for at most an hour


# ── cadences ──────────────────────────────────────────────────────────────────

def _parse_hhmm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


@dataclass(frozen=True)
class Every:
    """Fixed interval. First firing is one interval after boot (no boot storms)."""
    minutes: int

    def next_due(self, after: datetime, last_fired: datetime | None) -> datetime:
        base = last_fired or after
        return base + timedelta(minutes=self.minutes)

    def display(self) -> str:
        return f"every {self.minutes}m"


@dataclass(frozen=True)
class DailyAt:
    hhmm: str  # "03:05"

    def next_due(self, after: datetime, last_fired: datetime | None) -> datetime:
        h, m = _parse_hhmm(self.hhmm)
        candidate = after.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= after:
            candidate += timedelta(days=1)
        return candidate

    def display(self) -> str:
        return f"daily {self.hhmm}"


@dataclass(frozen=True)
class WeeklyAt:
    weekdays: tuple[int, ...]  # 0=Mon .. 6=Sun
    hhmm: str

    def next_due(self, after: datetime, last_fired: datetime | None) -> datetime:
        h, m = _parse_hhmm(self.hhmm)
        for offset in range(8):
            day = after + timedelta(days=offset)
            if day.weekday() in self.weekdays:
                candidate = day.replace(hour=h, minute=m, second=0, microsecond=0)
                if candidate > after:
                    return candidate
        raise RuntimeError("unreachable: no weekday match within 8 days")

    def display(self) -> str:
        names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        return f"{','.join(names[d] for d in self.weekdays)} {self.hhmm}"


@dataclass(frozen=True)
class MonthlyAt:
    day: int  # 1..28 (keep it simple; no month-end arithmetic)
    hhmm: str

    def next_due(self, after: datetime, last_fired: datetime | None) -> datetime:
        h, m = _parse_hhmm(self.hhmm)
        candidate = after.replace(day=min(self.day, 28), hour=h, minute=m,
                                  second=0, microsecond=0)
        if candidate <= after:
            year, month = after.year + (after.month // 12), (after.month % 12) + 1
            candidate = candidate.replace(year=year, month=month)
        return candidate

    def display(self) -> str:
        return f"monthly d{self.day} {self.hhmm}"


# ── jobs ──────────────────────────────────────────────────────────────────────

@dataclass
class Job:
    name: str
    cadence: Any
    fn: Callable[[], Awaitable[dict]]
    # async () -> True when this period's output already exists (skip the firing)
    period_guard: Callable[[], Awaitable[bool]] | None = None
    enabled: bool = True
    # mutable state (scheduler-owned)
    last_fired: datetime | None = field(default=None, compare=False)
    last_status: str = field(default="never", compare=False)   # never|completed|failed|skipped_*
    last_error: str | None = field(default=None, compare=False)
    last_run_id: str | None = field(default=None, compare=False)
    consecutive_failures: int = field(default=0, compare=False)
    next_due: datetime | None = field(default=None, compare=False)


class Scheduler:
    """Single asyncio loop over the job table. Construct, then `start()` in
    the app lifespan; `stop()` on shutdown. Infra access is via factories so
    tests inject fakes and api_server injects its shared helpers."""

    def __init__(
        self,
        jobs: list[Job],
        *,
        scope: str,
        pool_factory: Callable[[], Awaitable[Any]],
        publisher_factory: Callable[[], Any],
        redis_factory: Callable[[], Any],
        notifier: Any = None,
    ) -> None:
        self.jobs = jobs
        self.scope = scope
        self._pool_factory = pool_factory
        self._publisher_factory = publisher_factory
        self._redis_factory = redis_factory
        self._notifier = notifier
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self.started_at: datetime | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        self.started_at = datetime.now()
        now = self.started_at
        for j in self.jobs:
            j.next_due = j.cadence.next_due(now, None)
        self._task = asyncio.create_task(self._loop(), name="curlyos-scheduler")
        log.info("scheduler started with %d jobs", len(self.jobs))

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    # ── loop ─────────────────────────────────────────────────────────────────
    async def _loop(self) -> None:
        while not self._stopping.is_set():
            now = datetime.now()
            for job in self.jobs:
                if job.enabled and job.next_due and job.next_due <= now:
                    try:
                        await self.fire(job)
                    except Exception:  # noqa: BLE001 — the loop must survive anything
                        log.exception("scheduler: unexpected error firing %s", job.name)
                    job.next_due = job.cadence.next_due(datetime.now(), job.last_fired)
            soonest = min((j.next_due for j in self.jobs if j.enabled and j.next_due),
                          default=now + timedelta(seconds=60))
            sleep_s = max(1.0, min(60.0, (soonest - datetime.now()).total_seconds()))
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=sleep_s)
            except asyncio.TimeoutError:
                pass

    # ── one firing ───────────────────────────────────────────────────────────
    async def fire(self, job: Job) -> dict | None:
        """Run one job: lock → period guard → agent_runs row → fn → close out."""
        from shared.types.ulid import mint

        job.last_fired = datetime.now()

        redis = self._redis_factory()
        lock_key = f"lock:sched:{job.name}"
        locked = True
        try:
            if redis is not None:
                locked = bool(await redis.set(lock_key, "scheduler", nx=True, ex=_LOCK_TTL_S))
        except Exception as exc:  # noqa: BLE001 — no Redis ≠ no heartbeat
            log.warning("scheduler: lock check failed for %s (%s) — proceeding", job.name, exc)
        if not locked:
            job.last_status = "skipped_locked"
            log.info("scheduler: %s skipped (lock held)", job.name)
            return None

        try:
            if job.period_guard is not None:
                try:
                    if await job.period_guard():
                        job.last_status = "skipped_period_done"
                        log.info("scheduler: %s skipped (output exists for this period)", job.name)
                        return None
                except Exception as exc:  # noqa: BLE001 — guard failure must not block the job
                    log.warning("scheduler: period guard for %s failed (%s) — running anyway",
                                job.name, exc)

            run_id = mint("run")
            job.last_run_id = run_id
            pool = await self._pool_factory()
            pub = self._publisher_factory()
            await self._open_run(pool, pub, run_id, job)

            try:
                result = await job.fn()
            except Exception as exc:  # noqa: BLE001
                log.exception("scheduler: job %s raised", job.name)
                result = {"error": f"{type(exc).__name__}: {exc}"}

            error = result.get("error") if isinstance(result, dict) else None
            await self._close_run(pool, pub, run_id, job, result, error)

            if error:
                job.last_status, job.last_error = "failed", str(error)[:500]
                job.consecutive_failures += 1
                await self._notify_failure(job)
            else:
                job.last_status, job.last_error = "completed", None
                job.consecutive_failures = 0
            return result
        finally:
            if redis is not None:
                try:
                    await redis.delete(lock_key)
                except Exception:  # noqa: BLE001
                    pass
                try:  # factory-made client — close it or connections accumulate
                    await redis.aclose()
                except AttributeError:
                    try:
                        await redis.close()
                    except Exception:  # noqa: BLE001
                        pass
                except Exception:  # noqa: BLE001
                    pass

    # ── run-row + event bookkeeping ──────────────────────────────────────────
    async def _open_run(self, pool: Any, pub: Any, run_id: str, job: Job) -> None:
        from agent.pdp_gate import scope_parts
        from shared.events import build_event

        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO agent_runs (id, agent, scope, task, status, autonomy_level) "
                    "VALUES (%s, %s, %s, %s, 'running', 'confirm_each')",
                    (run_id, f"workflow:{job.name}", self.scope,
                     f"scheduled {job.name} ({job.cadence.display()})"),
                )
            ev = build_event(
                short_type="agent.run.started", subject=run_id,
                scope=scope_parts(self.scope),
                data={"run_id": run_id, "agent": f"workflow:{job.name}"},
                actor="system", source="curlyos-core/scheduler",
            )
            await pub.stage(ev, conn)

    async def _close_run(self, pool: Any, pub: Any, run_id: str, job: Job,
                         result: Any, error: str | None) -> None:
        from agent.pdp_gate import scope_parts
        from psycopg.types.json import Jsonb
        from shared.events import build_event

        status = "failed" if error else "completed"
        # keep stored results bounded — traces live in the result's own tables
        try:
            blob = result if isinstance(result, dict) else {"result": str(result)[:2000]}
            if len(json.dumps(blob, default=str)) > 16_000:
                blob = {"truncated": True, "keys": sorted(blob.keys())}
        except Exception:  # noqa: BLE001
            blob = {"unserializable": True}

        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE agent_runs SET status = %s, result = %s, error = %s, "
                    "finished_at = now() WHERE id = %s",
                    (status, Jsonb(blob), error, run_id),
                )
            ev = build_event(
                short_type=f"agent.run.{status}", subject=run_id,
                scope=scope_parts(self.scope),
                data={"run_id": run_id, "agent": f"workflow:{job.name}",
                      **({"error": str(error)[:500]} if error else {})},
                actor="system", source="curlyos-core/scheduler",
            )
            await pub.stage(ev, conn)

    async def _notify_failure(self, job: Job) -> None:
        if self._notifier is None:
            return
        try:
            await self._notifier.notify(
                f"CurlyOS workflow '{job.name}' failed "
                f"({job.consecutive_failures}× consecutive): {job.last_error}",
                run_id=job.last_run_id,
            )
        except Exception:  # noqa: BLE001
            log.warning("scheduler: failure notification for %s could not be sent", job.name)

    # ── observability ────────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        return {
            "running": self._task is not None and not self._task.done(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "jobs": [
                {
                    "name": j.name,
                    "cadence": j.cadence.display(),
                    "enabled": j.enabled,
                    "next_due": j.next_due.isoformat() if j.next_due else None,
                    "last_fired": j.last_fired.isoformat() if j.last_fired else None,
                    "last_status": j.last_status,
                    "last_error": j.last_error,
                    "last_run_id": j.last_run_id,
                    "consecutive_failures": j.consecutive_failures,
                }
                for j in self.jobs
            ],
        }
