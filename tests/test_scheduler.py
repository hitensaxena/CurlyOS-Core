"""Scheduler — cadence math + the fire() wrapper (lock, period guard,
agent_runs bookkeeping, failure handling). In-process fakes, $0."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from orchestration.scheduler import DailyAt, Every, Job, MonthlyAt, Scheduler, WeeklyAt

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
    def __init__(self, log):
        self.log = log

    async def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))

    async def fetchone(self):
        return None

    async def fetchall(self):
        return []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeConn:
    def __init__(self, log):
        self._log = log

    def cursor(self):
        return FakeCursor(self._log)


class FakePool:
    def __init__(self):
        self.sql_log = []

    def connection(self):
        pool = self

        class _CM:
            async def __aenter__(self):
                return FakeConn(pool.sql_log)

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
    insert = next(s for s, _ in pool.sql_log if s.startswith("INSERT INTO agent_runs"))
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
