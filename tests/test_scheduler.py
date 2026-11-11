"""Scheduler — cadence math + the fire() wrapper (lock, period guard,
agent_runs bookkeeping, failure handling). In-process fakes, $0."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from shared.scheduler import DailyAt, Every, Job, MonthlyAt, Scheduler, WeeklyAt

SCOPE = "user:usr_test"
T = datetime(2026, 6, 10, 14, 30)  # a Wednesday


# ── cadences ──────────────────────────────────────────────────────────────────
def test_every_first_fire_is_one_interval_after_boot():
    assert Every(15).next_due(T, None) == T + timedelta(minutes=15)
    assert Every(15).next_due(T, T - timedelta(minutes=5)) == T + timedelta(minutes=10)


def test_daily_at_before_and_after_the_time():
    assert DailyAt("03:05").next_due(T, None) == datetime(2026, 6, 11, 3, 5)
    early = T.replace(hour=2, minute=0)
    assert DailyAt("03:05").next_due(early, None) == datetime(2026, 6, 10, 3, 5)


def test_weekly_at_picks_next_matching_weekday():
    # T is Wed 14:30; Mon 06:20 → next Monday
    assert WeeklyAt((0,), "06:20").next_due(T, None) == datetime(2026, 6, 15, 6, 20)
    # Mon+Thu 08:10 → Thursday tomorrow
    assert WeeklyAt((0, 3), "08:10").next_due(T, None) == datetime(2026, 6, 11, 8, 10)
    # same-day future time counts
    assert WeeklyAt((2,), "18:00").next_due(T, None) == datetime(2026, 6, 10, 18, 0)
    # same-day past time rolls a week
    assert WeeklyAt((2,), "06:00").next_due(T, None) == datetime(2026, 6, 17, 6, 0)


def test_monthly_at_rolls_to_next_month_and_year():
    assert MonthlyAt(1, "06:40").next_due(T, None) == datetime(2026, 7, 1, 6, 40)
    before = datetime(2026, 6, 1, 5, 0)
    assert MonthlyAt(1, "06:40").next_due(before, None) == datetime(2026, 6, 1, 6, 40)
    december = datetime(2026, 12, 15, 12, 0)
    assert MonthlyAt(1, "06:40").next_due(december, None) == datetime(2027, 1, 1, 6, 40)


# ── fakes ─────────────────────────────────────────────────────────────────────
class FakeCursor:
    def __init__(self, log, rows=()):
        self.log = log
        self.rows = list(rows)

    async def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))

    async def fetchone(self):
        return None

    async def fetchall(self):
        return self.rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeConn:
    def __init__(self, log, rows=()):
        self._log = log
        self._rows = rows

    def cursor(self):
        return FakeCursor(self._log, self._rows)


class FakePool:
    def __init__(self, rows=()):
        self.sql_log = []
        self.rows = list(rows)

    def connection(self):
        pool = self

        class _CM:
            async def __aenter__(self):
                return FakeConn(pool.sql_log, pool.rows)

            async def __aexit__(self, *a):
                return False

        return _CM()


class FakePublisher:
    def __init__(self):
        self.staged = []

    async def stage(self, ev, conn):
        self.staged.append(ev)
        return ev["id"], "curlyos.test", ev

    async def emit(self, subject, ev):
        pass


class FakeRedis:
    def __init__(self, locked=False):
        self.locked = locked
        self.deleted = []

    async def set(self, key, value, nx=False, ex=None):
        return not self.locked

    async def delete(self, key):
        self.deleted.append(key)

    async def aclose(self):
        pass


class FakeNotifier:
    def __init__(self):
        self.sent = []

    async def notify(self, text, **kw):
        self.sent.append(text)
        return True


def _scheduler(job, *, pool=None, redis=None, notifier=None):
    pool = pool or FakePool()
    sched = Scheduler(
        [job], scope=SCOPE,
        pool_factory=_async_return(pool),
        publisher_factory=FakePublisher,
        redis_factory=lambda: redis,
        notifier=notifier,
    )
    return sched, pool


def _async_return(value):
    async def f():
        return value
    return f


# ── fire() behavior ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_fire_success_records_run_row_and_events():
    async def ok_job():
        return {"did": "things"}

    job = Job("test_job", Every(15), ok_job)
    sched, pool = _scheduler(job, redis=FakeRedis())
    result = await sched.fire(job)

    assert result == {"did": "things"}
    assert job.last_status == "completed" and job.consecutive_failures == 0
    assert job.last_run_id and job.last_run_id.startswith("run_")
    assert any(s.startswith("INSERT INTO agent_runs") for s, _ in pool.sql_log)
    assert "workflow:" in str(pool.sql_log)
    update = next((s, p) for s, p in pool.sql_log if s.startswith("UPDATE agent_runs"))
    assert update[1][0] == "completed"


@pytest.mark.asyncio
async def test_fire_failure_marks_failed_and_notifies():
    async def bad_job():
        raise RuntimeError("boom")

    notifier = FakeNotifier()
    job = Job("bad_job", Every(15), bad_job)
    sched, pool = _scheduler(job, redis=FakeRedis(), notifier=notifier)
    result = await sched.fire(job)

    assert "boom" in result["error"]
    assert job.last_status == "failed" and job.consecutive_failures == 1
    update = next((s, p) for s, p in pool.sql_log if s.startswith("UPDATE agent_runs"))
    assert update[1][0] == "failed"
    assert notifier.sent and "bad_job" in notifier.sent[0]


@pytest.mark.asyncio
async def test_fire_error_dict_result_counts_as_failure():
    async def soft_fail():
        return {"error": "llm unavailable"}

    job = Job("soft", Every(15), soft_fail)
    sched, _ = _scheduler(job, redis=FakeRedis())
    await sched.fire(job)
    assert job.last_status == "failed" and job.last_error == "llm unavailable"


@pytest.mark.asyncio
async def test_fire_skips_when_lock_held_and_runs_nothing():
    fired = []

    async def job_fn():
        fired.append(1)
        return {}

    job = Job("locked", Every(15), job_fn)
    sched, pool = _scheduler(job, redis=FakeRedis(locked=True))
    out = await sched.fire(job)
    assert out is None and not fired and not pool.sql_log
    assert job.last_status == "skipped_locked"


@pytest.mark.asyncio
async def test_fire_skips_when_period_guard_says_done():
    fired = []

    async def job_fn():
        fired.append(1)
        return {}

    async def guard():
        return True

    job = Job("guarded", WeeklyAt((0,), "06:20"), job_fn, period_guard=guard)
    sched, pool = _scheduler(job, redis=FakeRedis())
    out = await sched.fire(job)
    assert out is None and not fired and not pool.sql_log
    assert job.last_status == "skipped_period_done"


@pytest.mark.asyncio
async def test_fire_runs_when_guard_itself_fails():
    async def job_fn():
        return {"ran": True}

    async def broken_guard():
        raise ConnectionError("db hiccup")

    job = Job("guard_broken", Every(15), job_fn, period_guard=broken_guard)
    sched, _ = _scheduler(job, redis=FakeRedis())
    out = await sched.fire(job)
    assert out == {"ran": True} and job.last_status == "completed"


@pytest.mark.asyncio
async def test_fire_without_redis_still_runs():
    async def job_fn():
        return {"ok": 1}

    job = Job("no_redis", Every(15), job_fn)
    sched, _ = _scheduler(job, redis=None)
    out = await sched.fire(job)
    assert out == {"ok": 1} and job.last_status == "completed"


def test_snapshot_shape():
    job = Job("snap", DailyAt("03:05"), _async_return({}))
    sched, _ = _scheduler(job, redis=None)
    snap = sched.snapshot()
    assert snap["jobs"][0]["name"] == "snap"
    assert snap["jobs"][0]["cadence"] == "daily 03:05"
    assert snap["jobs"][0]["last_status"] == "never"


# ── trigger() — the manual POST /api/scheduler/run/{name} path ────────────────
@pytest.mark.asyncio
async def test_trigger_runs_the_job_and_reports_the_outcome():
    async def job_fn():
        return {"found": 3}

    job = Job("attention_scan", Every(15), job_fn)
    sched, pool = _scheduler(job, redis=FakeRedis())
    out = await sched.trigger("attention_scan")

    assert out["job"] == "attention_scan"
    assert out["status"] == "completed" and out["error"] is None
    assert out["result"] == {"found": 3}
    assert out["run_id"].startswith("run_")
    assert isinstance(out["duration_ms"], int) and out["duration_ms"] >= 0
    # Same bookkeeping as a scheduled firing: a run row was opened and closed.
    assert any(s.startswith("INSERT INTO agent_runs") for s, _ in pool.sql_log)
    assert any(s.startswith("UPDATE agent_runs") for s, _ in pool.sql_log)


@pytest.mark.asyncio
async def test_trigger_advances_next_due_so_the_loop_does_not_refire():
    job = Job("once", Every(15), _async_return({}))
    sched, _ = _scheduler(job, redis=None)
    await sched.trigger("once")
    assert job.next_due > datetime.now() + timedelta(minutes=14)


@pytest.mark.asyncio
async def test_trigger_unknown_job_raises_keyerror():
    job = Job("known", Every(15), _async_return({}))
    sched, _ = _scheduler(job, redis=None)
    with pytest.raises(KeyError):
        await sched.trigger("nope")


@pytest.mark.asyncio
async def test_trigger_disabled_job_does_nothing():
    fired = []

    async def job_fn():
        fired.append(1)
        return {}

    job = Job("off", Every(15), job_fn, enabled=False)
    sched, pool = _scheduler(job, redis=None)
    out = await sched.trigger("off")
    assert out["status"] == "disabled" and out["run_id"] is None
    assert not fired and not pool.sql_log


@pytest.mark.asyncio
async def test_trigger_reports_a_skip_as_a_status_not_an_error():
    async def guard():
        return True

    job = Job("guarded", WeeklyAt((0,), "06:20"), _async_return({}), period_guard=guard)
    sched, _ = _scheduler(job, redis=FakeRedis())
    out = await sched.trigger("guarded")
    assert out["status"] == "skipped_period_done" and out["error"] is None
    assert out["result"] is None


# ── orphan reconciliation at startup ──────────────────────────────────────────
@pytest.mark.asyncio
async def test_reconcile_closes_rows_left_running_by_a_crash():
    job = Job("any", Every(15), _async_return({}))
    sched, pool = _scheduler(job, redis=None)
    await sched._reconcile_orphaned_runs()

    sql, _ = next((s, p) for s, p in pool.sql_log if s.startswith("UPDATE agent_runs"))
    assert "status = 'failed'" in sql
    assert "WHERE status = 'running'" in sql
    assert "interrupted" in sql  # the reason is recorded, not just the status


@pytest.mark.asyncio
async def test_reconcile_never_blocks_the_loop_when_the_db_is_down():
    job = Job("any", Every(15), _async_return({}))

    async def broken_pool():
        raise ConnectionError("postgres down")

    sched = Scheduler([job], scope=SCOPE, pool_factory=broken_pool,
                      publisher_factory=FakePublisher, redis_factory=lambda: None)
    await sched._reconcile_orphaned_runs()  # must not raise


# ── boot-time catch-up ────────────────────────────────────────────────────────
# `start()` can only compute the next slot from "now"; without the run history
# a restart after a slot moved weekly/monthly jobs a whole period out, which is
# how narrative_generate last fired on 2026-10-11 and stayed silent after.
@pytest.mark.asyncio
async def test_seed_catches_up_a_weekly_job_whose_slot_has_passed():
    job = Job("narrative_generate", WeeklyAt((6,), "05:20"), _async_return({}))
    last = datetime.now() - timedelta(days=31)
    sched, _ = _scheduler(job, pool=FakePool(rows=[("workflow:narrative_generate", last)]))

    await sched._seed_next_due()

    assert job.next_due <= datetime.now() + timedelta(seconds=30)
    assert job.last_fired == last


@pytest.mark.asyncio
async def test_seed_keeps_a_weekly_job_that_already_ran_this_period_on_schedule():
    job = Job("narrative_generate", WeeklyAt((6,), "05:20"), _async_return({}))
    last = datetime.now() - timedelta(days=1)
    sched, _ = _scheduler(job, pool=FakePool(rows=[("workflow:narrative_generate", last)]))

    await sched._seed_next_due()

    assert job.next_due > datetime.now() + timedelta(days=1)


@pytest.mark.asyncio
async def test_seed_staggers_a_backlog_instead_of_firing_it_all_at_once():
    jobs = [Job(f"job{i}", WeeklyAt((6,), "05:20"), _async_return({})) for i in range(3)]
    last = datetime.now() - timedelta(days=40)
    rows = [(f"workflow:job{i}", last) for i in range(3)]
    sched = Scheduler(jobs, scope=SCOPE, pool_factory=_async_return(FakePool(rows=rows)),
                      publisher_factory=FakePublisher, redis_factory=lambda: None)

    await sched._seed_next_due()

    dues = [j.next_due for j in jobs]
    assert dues == sorted(dues) and len(set(dues)) == len(dues)
    assert max(dues) <= datetime.now() + timedelta(seconds=60)


@pytest.mark.asyncio
async def test_seed_without_history_schedules_normally():
    job = Job("fresh", Every(20), _async_return({}))
    sched, _ = _scheduler(job, pool=FakePool())

    before = datetime.now()
    await sched._seed_next_due()

    assert before + timedelta(minutes=19) < job.next_due < datetime.now() + timedelta(minutes=21)


@pytest.mark.asyncio
async def test_seed_never_blocks_the_loop_when_the_db_is_down():
    job = Job("fresh", Every(20), _async_return({}))

    async def broken_pool():
        raise ConnectionError("postgres down")

    sched = Scheduler([job], scope=SCOPE, pool_factory=broken_pool,
                      publisher_factory=FakePublisher, redis_factory=lambda: None)
    await sched._seed_next_due()
    assert job.next_due is not None
