"""Approval service + PDP gate — in-process, fake pool/redis/publisher ($0).

Covers the lifted approval state machine on the curlyos-core schema
(approvals.scope direct, nullable run_id) and the gate's fail-closed and
approval-persistence paths.
"""
from __future__ import annotations

import pytest

from agent import approval_service, pdp_gate
from safety.pdp import PDPVerdict

SCOPE = "user:usr_test"


# ── minimal fakes (scripted cursor) ───────────────────────────────────────────
class FakeCursor:
    def __init__(self, script):
        self.script = script          # list of fetchone/fetchall results, popped in order
        self.executed = []            # (sql, params) log shared via script owner
        self.rowcount = 1

    async def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))

    async def fetchone(self):
        return self.script.pop(0) if self.script else None

    async def fetchall(self):
        return self.script.pop(0) if self.script else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    async def execute(self, sql, params=None):
        await self._cursor.execute(sql, params)


class FakePool:
    def __init__(self, cursor):
        self.conn = FakeConn(cursor)

    def connection(self):
        pool = self

        class _CM:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *a):
                return False

        return _CM()


class FakePublisher:
    def __init__(self):
        self.staged = []
        self.emitted = []

    async def stage(self, ev, conn):
        self.staged.append(ev)
        return ev.get("id", "evt_x"), "curlyos.test", ev

    async def emit(self, subject, ev):
        self.emitted.append((subject, ev))


class DeadRedis:
    async def get(self, key):  # any read explodes → fail-closed unreadable
        raise ConnectionError("redis down")


class AliveRedis:
    async def get(self, key):
        return None  # no kill keys set


# ── approval_service ──────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_grant_happy_path_flips_state_and_emits():
    # _locate → (pending, run_id, action_class, expired=False); UPDATE rowcount=1
    cur = FakeCursor(script=[("pending", "run_1", "memory_write", False)])
    pool, pub = FakePool(cur), FakePublisher()
    out = await approval_service.grant(pool, pub, SCOPE, "apv_1")
    assert out == {"apv_id": "apv_1", "state": "granted",
                   "run_id": "run_1", "action_class": "memory_write"}
    update_sql = next(s for s, _ in cur.executed if s.startswith("UPDATE approvals"))
    assert "state = 'pending'" in update_sql and "decided_at = now()" in update_sql
    locate_sql = next(s for s, _ in cur.executed if s.startswith("SELECT state"))
    assert "JOIN" not in locate_sql and "scope = %s" in locate_sql  # schema-direct scope
    assert pub.staged[0]["type"].endswith("safety.approval.granted")
    assert pub.emitted


@pytest.mark.asyncio
async def test_grant_human_origin_approval_run_id_none():
    cur = FakeCursor(script=[("pending", None, "memory_forget_hard", False)])
    out = await approval_service.grant(FakePool(cur), FakePublisher(), SCOPE, "apv_h")
    assert out["run_id"] is None and out["state"] == "granted"


@pytest.mark.asyncio
async def test_grant_not_found_raises():
    cur = FakeCursor(script=[None])
    with pytest.raises(approval_service.ApprovalNotFound):
        await approval_service.grant(FakePool(cur), FakePublisher(), SCOPE, "apv_missing")


@pytest.mark.asyncio
async def test_grant_non_pending_raises_conflict():
    cur = FakeCursor(script=[("granted", "run_1", "memory_write", False)])
    with pytest.raises(approval_service.ApprovalNotActionable) as ei:
        await approval_service.grant(FakePool(cur), FakePublisher(), SCOPE, "apv_1")
    assert ei.value.state == "granted"


@pytest.mark.asyncio
async def test_grant_expired_raises():
    cur = FakeCursor(script=[("pending", "run_1", "memory_write", True)])
    with pytest.raises(approval_service.ApprovalNotActionable) as ei:
        await approval_service.grant(FakePool(cur), FakePublisher(), SCOPE, "apv_1")
    assert ei.value.state == "expired"


@pytest.mark.asyncio
async def test_grant_lost_race_reports_true_state():
    cur = FakeCursor(script=[("pending", "run_1", "memory_write", False),
                             ("denied", False)])  # post-race re-read
    cur.rowcount = 0  # the conditional UPDATE hit nothing
    with pytest.raises(approval_service.ApprovalNotActionable) as ei:
        await approval_service.grant(FakePool(cur), FakePublisher(), SCOPE, "apv_1")
    assert ei.value.state == "denied"


@pytest.mark.asyncio
async def test_deny_emits_reason():
    cur = FakeCursor(script=[("pending", "run_1", "memory_write", False)])
    pub = FakePublisher()
    out = await approval_service.deny(FakePool(cur), pub, SCOPE, "apv_1", reason="nope")
    assert out["state"] == "denied"
    assert pub.staged[0]["data"]["reason"] == "nope"


@pytest.mark.asyncio
async def test_list_pending_shape():
    cur = FakeCursor(script=[[("apv_1", None, "human", "memory_forget_hard", {"k": 1}, None, None)]])
    items = await approval_service.list_pending(FakePool(cur), SCOPE)
    assert items == [{"apv_id": "apv_1", "run_id": None, "origin": "human",
                      "action_class": "memory_forget_hard", "payload": {"k": 1},
                      "expires_at": None, "created_at": None}]


# ── pdp_gate ──────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_gate_redis_none_fails_closed_no_approval_row():
    cur = FakeCursor(script=[])
    pub = FakePublisher()
    d = await pdp_gate.evaluate(
        pool=FakePool(cur), redis=None, publisher=pub, scope_text=SCOPE,
        run_id="run_1", action_id="act_1", action_class="memory_write",
        autonomy_level="confirm_each",
    )
    assert d.verdict is PDPVerdict.DENY and d.reason == "kill_switch_unreadable"
    assert not cur.executed and not pub.staged  # DENY attaches no side effect


@pytest.mark.asyncio
async def test_gate_dead_redis_fails_closed():
    d = await pdp_gate.evaluate(
        pool=FakePool(FakeCursor([])), redis=DeadRedis(), publisher=FakePublisher(),
        scope_text=SCOPE, run_id="run_1", action_id="act_1",
        action_class="memory_write", autonomy_level="confirm_each",
    )
    assert d.verdict is PDPVerdict.DENY and d.reason == "kill_switch_unreadable"


@pytest.mark.asyncio
async def test_gate_confirm_each_persists_approval_with_scope_and_origin():
    cur = FakeCursor(script=[])
    pub = FakePublisher()
    d = await pdp_gate.evaluate(
        pool=FakePool(cur), redis=AliveRedis(), publisher=pub, scope_text=SCOPE,
        run_id="run_1", action_id="act_1", action_class="memory_write",
        autonomy_level="confirm_each",
    )
    assert d.verdict is PDPVerdict.REQUIRE_APPROVAL
    assert d.apv_id and d.apv_id.startswith("apv_")
    sql, params = next((s, p) for s, p in cur.executed if s.startswith("INSERT INTO approvals"))
    assert "'agent'" in sql and "scope" in sql
    assert params[1] == "run_1" and params[2] == SCOPE
    assert pub.staged[0]["type"].endswith("safety.approval.requested")


@pytest.mark.asyncio
async def test_gate_read_allows_without_io():
    d = await pdp_gate.evaluate(
        pool=FakePool(FakeCursor([])), redis=AliveRedis(), publisher=FakePublisher(),
        scope_text=SCOPE, run_id="run_1", action_id="act_1",
        action_class="read", autonomy_level="confirm_each",
    )
    assert d.verdict is PDPVerdict.ALLOW


def test_scope_parts():
    assert pdp_gate.scope_parts("user:usr_hiten") == {"level": "user", "user_id": "usr_hiten"}
    assert pdp_gate.scope_parts("usr_bare") == {"level": "user", "user_id": "usr_bare"}
