"""User-defined autonomous jobs — the scheduler layer over `scheduled_jobs` rows.

The base scheduler (orchestration/scheduler.py) drives a fixed, CODE-defined job
table. This module adds the USER-defined layer the webapp manages: each
`scheduled_jobs` row becomes a `scheduler.Job` whose `fn` routes the row's
natural-language `task` through the Executive agent (`Runner.start_run`) — the
exact same engine as an interactive run — waits for that run to reach a terminal
state, and delivers the synthesized output as an `inbox_items` row.

Design notes:
  * Job names are `user:<sjob_id>` so they namespace cleanly against the
    code-defined jobs and give each its own Redis single-flight lock.
  * The scheduler's `fire()` already wraps every firing in a `workflow:<name>`
    agent_runs envelope; the Executive run started here is a SEPARATE run with
    the real plan/act/synthesize trace. The inbox item links to that inner run.
  * Cadence runs in host local time, consistent with the base scheduler.
  * A 5-minute floor on `Every` keeps a fat-fingered "every 1 minute" from
    hammering the LLM.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Awaitable, Callable

from orchestration.scheduler import DailyAt, Every, Job, MonthlyAt, WeeklyAt

log = logging.getLogger("curlyos-core.user_jobs")

MIN_INTERVAL_MINUTES = 5            # floor on Every(minutes) — never hammer the LLM

PoolFactory = Callable[[], Awaitable[Any]]
RunnerGetter = Callable[[], Any]   # () -> Runner | None (resolved lazily from app.state)


# ── cadence mapping ───────────────────────────────────────────────────────────

def parse_cadence(cadence_type: str, cadence_json: dict | None):
    """Map a stored (cadence_type, cadence_json) onto a scheduler cadence object.

    Raises ValueError on malformed input so the API can reject a bad create.
    """
    cj = cadence_json or {}
    if cadence_type == "every":
        minutes = int(cj.get("minutes", 60))
        if minutes < MIN_INTERVAL_MINUTES:
            log.info("user_jobs: clamping interval %dm → %dm floor", minutes, MIN_INTERVAL_MINUTES)
            minutes = MIN_INTERVAL_MINUTES
        return Every(minutes)
    if cadence_type == "daily_at":
        return DailyAt(_require_hhmm(cj))
    if cadence_type == "weekly_at":
        weekdays = cj.get("weekdays")
        if not weekdays or not all(isinstance(d, int) and 0 <= d <= 6 for d in weekdays):
            raise ValueError("weekly_at needs weekdays: a non-empty list of 0..6 (0=Mon)")
        return WeeklyAt(tuple(sorted(set(weekdays))), _require_hhmm(cj))
    if cadence_type == "monthly_at":
        day = int(cj.get("day", 1))
        if not 1 <= day <= 28:
            raise ValueError("monthly_at needs day in 1..28")
        return MonthlyAt(day, _require_hhmm(cj))
    raise ValueError(f"unknown cadence_type {cadence_type!r}")


def _require_hhmm(cj: dict) -> str:
    hhmm = cj.get("hhmm")
    if not isinstance(hhmm, str) or ":" not in hhmm:
        raise ValueError("cadence needs hhmm as 'HH:MM'")
    h, m = hhmm.split(":", 1)
    if not (h.isdigit() and m.isdigit() and 0 <= int(h) <= 23 and 0 <= int(m) <= 59):
        raise ValueError(f"invalid hhmm {hhmm!r} — expected 'HH:MM' 00:00..23:59")
    return f"{int(h):02d}:{int(m):02d}"


def cadence_display(cadence_type: str, cadence_json: dict | None) -> str:
    """Human-readable cadence for the API/UI without instantiating a cadence."""
    try:
        return parse_cadence(cadence_type, cadence_json).display()
    except Exception:  # noqa: BLE001 — display must never raise
        return f"{cadence_type} {cadence_json or {}}"


# ── job firing ────────────────────────────────────────────────────────────────

def make_job_fn(
    *,
    job_id: str,
    scope: str,
    name: str,
    task: str,
    get_runner: RunnerGetter,
    pool_factory: PoolFactory,
) -> Callable[[], Awaitable[dict]]:
    """Build the async `Job.fn` for one scheduled job."""

    async def fn() -> dict:
        runner = get_runner()
        if runner is None:
            await _mark(pool_factory, job_id, "failed", None, "runner unavailable")
            return {"error": "runner unavailable"}
        try:
            run_id = await runner.start_run(task, source=f"scheduled:{job_id}")
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
            await _mark(pool_factory, job_id, "failed", None, err)
            return {"error": err}

        # Mark running with the run_id NOW so the webapp can find the in-flight
        # run and show live step progress. We do NOT wait for the run here:
        # delivery to the inbox happens when the run reaches a TRUE terminal
        # state, driven by the runner's completion hook (deliver_run_output).
        # That decoupling is what makes a run that PARKS for a human approval
        # (and finishes hours later) still deliver its real output — and means a
        # parked run never lands a misleading "paused" note in the inbox.
        await _mark(pool_factory, job_id, "running", run_id, None)
        return {"run_id": run_id, "status": "running"}

    return fn


async def deliver_run_output(pool_factory: PoolFactory, run_id: str, status: str) -> None:
    """Runner park/completion hook — owns scheduled-job status sync + delivery.

    Called whenever an Executive run changes to a notable state (parked,
    completed, failed). It:
      * keeps the owning scheduled job's last_status in sync (running → parked
        → completed/failed), and
      * on a TRUE terminal state (completed/failed) delivers the run's output to
        the inbox exactly once (dedup on run_id).

    A no-op for interactive runs (not owned by any scheduled job). Idempotent:
    a resume-after-park that completes, or a startup-recovery re-drive, never
    double-delivers. Never raises into the runner — failures are logged.
    """
    from psycopg.types.json import Jsonb
    from shared.types.ulid import mint

    try:
        pool = await pool_factory()
        # Which scheduled job (if any) owns this run?
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, scope, name FROM scheduled_jobs WHERE last_run_id = %s",
                    (run_id,),
                )
                job = await cur.fetchone()
        if job is None:
            return  # interactive run, or the job already moved to a newer run
        job_id, scope, name = job

        # Read the run's authoritative outcome.
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT status, result, error FROM agent_runs WHERE id = %s", (run_id,)
                )
                row = await cur.fetchone()
        run_status = row[0] if row else status
        summary = _extract_summary(row[1]) if row else None
        error = row[2] if row else None

        # Keep the job status in sync no matter the state.
        await _mark(pool_factory, job_id, run_status, run_id, error)

        # Only completed/failed deliver output. Parked is interim (the run
        # resumes after approval); cancelled is an abort — neither delivers.
        if run_status not in ("completed", "failed"):
            return

        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT 1 FROM inbox_items WHERE run_id = %s LIMIT 1", (run_id,)
                )
                if await cur.fetchone() is not None:
                    return  # already delivered
                body = (
                    (summary or "(the agent completed but produced no summary)")
                    if run_status == "completed"
                    else f"The agent run failed: {error or 'unknown error'}"
                )
                title = f"{name} — {datetime.now().strftime('%b %d, %H:%M')}"
                await cur.execute(
                    "INSERT INTO inbox_items (id, scope, job_id, run_id, title, body, meta) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (mint("inb"), scope, job_id, run_id, title, body,
                     Jsonb({"status": run_status})),
                )
        log.info("user_jobs: delivered %s output for job %s (run %s)",
                 run_status, job_id, run_id)
    except Exception:  # noqa: BLE001 — a delivery failure must never break the run
        log.exception("user_jobs: deliver_run_output failed for run %s", run_id)


async def reconcile_deliveries(pool_factory: PoolFactory) -> None:
    """Startup catch-up for missed deliveries.

    The live `on_run_event` hook only fires while the API is up. A run that
    finishes during a restart window — or one that completed before this hook
    existed — would leave its job stuck (e.g. showing 'parked' while the run is
    really 'completed') with no inbox delivery. On boot we replay every job's
    last run through `deliver_run_output`, which syncs status and delivers any
    completed/failed run that hasn't been delivered yet (idempotent via dedup).
    """
    pool = await pool_factory()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT last_run_id FROM scheduled_jobs WHERE last_run_id IS NOT NULL"
            )
            run_ids = [r[0] for r in await cur.fetchall()]
    for run_id in run_ids:
        await deliver_run_output(pool_factory, run_id, "reconcile")
    if run_ids:
        log.info("user_jobs: reconciled %d job(s) on startup", len(run_ids))


def _extract_summary(result: Any) -> str | None:
    """Pull the synthesized text from agent_runs.result (the graph's decision dict)."""
    if result is None:
        return None
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except Exception:  # noqa: BLE001
            return result
    if isinstance(result, dict):
        for key in ("summary", "answer", "output", "narrative", "text"):
            val = result.get(key)
            if isinstance(val, str) and val.strip():
                return val
        return json.dumps(result, ensure_ascii=False, indent=2)
    return str(result)


async def _mark(
    pool_factory: PoolFactory, job_id: str, status: str,
    run_id: str | None, error: str | None,
) -> None:
    pool = await pool_factory()
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE scheduled_jobs SET last_fired = now(), last_status = %s, "
                "last_run_id = %s, last_error = %s, updated_at = now() WHERE id = %s",
                (status, run_id, error, job_id),
            )


# ── building Job objects from rows ────────────────────────────────────────────

def job_scheduler_name(job_id: str) -> str:
    return f"user:{job_id}"


def build_job(row: dict, *, get_runner: RunnerGetter, pool_factory: PoolFactory) -> Job:
    """Turn one scheduled_jobs row (dict) into a scheduler.Job."""
    cadence = parse_cadence(row["cadence_type"], row["cadence_json"])
    fn = make_job_fn(
        job_id=row["id"], scope=row["scope"], name=row["name"], task=row["task"],
        get_runner=get_runner, pool_factory=pool_factory,
    )
    return Job(
        name=job_scheduler_name(row["id"]),
        cadence=cadence,
        fn=fn,
        enabled=bool(row["enabled"]),
    )


async def load_user_jobs(
    pool: Any, *, get_runner: RunnerGetter, pool_factory: PoolFactory,
) -> list[Job]:
    """Read every scheduled_jobs row and build its Job (enabled flag preserved)."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, scope, name, task, cadence_type, cadence_json, enabled "
                "FROM scheduled_jobs ORDER BY created_at"
            )
            cols = [c.name for c in cur.description]
            rows = [dict(zip(cols, r)) for r in await cur.fetchall()]
    jobs: list[Job] = []
    for r in rows:
        try:
            jobs.append(build_job(r, get_runner=get_runner, pool_factory=pool_factory))
        except Exception:  # noqa: BLE001 — one bad row must not sink the whole load
            log.exception("user_jobs: skipping malformed job %s", r.get("id"))
    log.info("user_jobs: loaded %d user-defined job(s)", len(jobs))
    return jobs


# ── live (re)registration against a running scheduler ─────────────────────────
#
# The scheduler reads `scheduler.jobs` fresh each loop tick (≤60s), so mutating
# the list registers/unregisters a job without a restart. All of this runs in the
# single asyncio thread, so list mutation between ticks is safe.

def register_job(scheduler: Any, job: Job) -> None:
    """Add (or replace) a job in the live scheduler and arm its next firing."""
    unregister_job(scheduler, _job_id_from_name(job.name))
    job.next_due = job.cadence.next_due(datetime.now(), None)
    scheduler.jobs.append(job)
    log.info("user_jobs: registered %s (next %s)", job.name, job.next_due)


def unregister_job(scheduler: Any, job_id: str) -> None:
    """Remove a job from the live scheduler by its sjob id (no-op if absent)."""
    name = job_scheduler_name(job_id)
    scheduler.jobs[:] = [j for j in scheduler.jobs if j.name != name]


def find_job(scheduler: Any, job_id: str) -> Job | None:
    name = job_scheduler_name(job_id)
    return next((j for j in scheduler.jobs if j.name == name), None)


def _job_id_from_name(name: str) -> str:
    return name.split("user:", 1)[1] if name.startswith("user:") else name
