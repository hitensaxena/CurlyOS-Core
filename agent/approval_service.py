"""Approval grant / deny — the human-in-the-loop half of the confirm_each gate.

A parked confirm_each action waits on an `apv_` approval. The user GRANTS it
(→ the caller resumes the parked run so the side effect executes) or DENIES it
(→ the caller cancels/degrades the run). Both transitions are scope-checked,
single-shot (a conditional `pending →` UPDATE — a second grant/deny is
rejected), and expiry-aware. A granted `memory_forget_hard` approval feeds
straight into governance.forget(), whose own single-use check (the immutable
events log) prevents the same approval scrubbing twice.

Adaptations from the build repo (recorded):
  * Scope checks read approvals.scope directly (no agent_runs join) — works
    identically for agent-originated rows and human-originated rows
    (origin='human', run_id NULL, e.g. a webapp forget request).
  * grant()/deny() do NOT call into a run loop. They flip state, emit the
    event, and return {run_id, action_class}; the Phase-A runner (LangGraph,
    orchestration/runner.py) owns resume/cancel — one resume primitive,
    per curlyos-final/06 §2. run_id is None for human-originated approvals.
"""
from __future__ import annotations

import logging
from typing import Any

from agent.pdp_gate import scope_parts

log = logging.getLogger("curlyos-core.agent.approval")


class ApprovalNotFound(Exception):
    """No such approval in the caller's scope (→ 404)."""

    def __init__(self, apv_id: str) -> None:
        super().__init__(f"approval {apv_id!r} not found")
        self.apv_id = apv_id


class ApprovalNotActionable(Exception):
    """The approval exists but is not pending (already granted/denied/expired) (→ 409)."""

    def __init__(self, apv_id: str, state: str) -> None:
        super().__init__(f"approval {apv_id!r} is {state}, not pending")
        self.apv_id = apv_id
        self.state = state


async def _locate(conn: Any, apv_id: str, scope_text: str) -> tuple[str, str | None, str, bool]:
    """Return (state, run_id, action_class, expired) for an approval in scope, or raise NotFound."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT state, run_id, action_class, (expires_at <= now()) AS expired "
            "FROM approvals WHERE id = %s AND scope = %s",
            (apv_id, scope_text),
        )
        row = await cur.fetchone()
    if row is None:
        raise ApprovalNotFound(apv_id)
    return row[0], row[1], row[2], bool(row[3])


async def _transition(pool: Any, publisher: Any, scope_text: str, apv_id: str, *, to_state: str,
                      short_type: str, event_data: dict[str, Any]) -> tuple[str | None, str]:
    """Atomically flip a pending approval to `to_state` and emit the safety event.

    Returns (run_id, action_class). `event_data` is the per-event canonical
    payload: granted carries `granted_by`, denied carries `reason` —
    `{apv_id, run_id}` are added here.
    """
    from shared.events import build_event

    parts = scope_parts(scope_text)
    async with pool.connection() as conn:
        state, run_id, action_class, expired = await _locate(conn, apv_id, scope_text)
        if state != "pending":
            raise ApprovalNotActionable(apv_id, state)
        if expired:
            raise ApprovalNotActionable(apv_id, "expired")
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE approvals SET state = %s, decided_at = now() "
                "WHERE id = %s AND state = 'pending' AND expires_at > now()",
                (to_state, apv_id),
            )
            if cur.rowcount != 1:  # lost a race to a concurrent grant/deny/expiry — report the TRUE state
                await cur.execute("SELECT state, (expires_at <= now()) FROM approvals WHERE id = %s", (apv_id,))
                row = await cur.fetchone()
                cur_state = "expired" if (row and row[1]) else (row[0] if row else "missing")
                raise ApprovalNotActionable(apv_id, cur_state)
        data = {"apv_id": apv_id, "run_id": run_id, **event_data}
        ev = build_event(
            short_type=short_type, subject=apv_id,
            scope=parts,
            data=data,
            actor=f"user:{parts['user_id']}", source="curlyos-core/safety",
        )
        _stored, subject, stamped = await publisher.stage(ev, conn)  # state flip + event, one tx
    try:
        await publisher.emit(subject, stamped)
    except Exception as exc:  # noqa: BLE001 — durable in events table
        log.warning("approval %s event emit failed for %s: %s", to_state, apv_id, exc)
    return run_id, action_class


async def grant(pool: Any, publisher: Any, scope_text: str, apv_id: str) -> dict:
    """Grant a pending approval. The caller resumes any parked run (run_id non-None)."""
    parts = scope_parts(scope_text)
    run_id, action_class = await _transition(
        pool, publisher, scope_text, apv_id, to_state="granted",
        short_type="safety.approval.granted",
        event_data={"granted_by": f"user:{parts['user_id']}"},
    )
    return {"apv_id": apv_id, "state": "granted", "run_id": run_id, "action_class": action_class}


async def deny(pool: Any, publisher: Any, scope_text: str, apv_id: str,
               reason: str = "user_denied") -> dict:
    """Deny a pending approval. The caller cancels/degrades any parked run."""
    run_id, action_class = await _transition(
        pool, publisher, scope_text, apv_id, to_state="denied",
        short_type="safety.approval.denied", event_data={"reason": reason},
    )
    return {"apv_id": apv_id, "state": "denied", "run_id": run_id, "action_class": action_class}


async def list_pending(pool: Any, scope_text: str) -> list[dict]:
    """List the caller's pending, unexpired approvals (the Approval-card queue)."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, run_id, origin, action_class, payload, expires_at, created_at "
                "FROM approvals "
                "WHERE scope = %s AND state = 'pending' AND expires_at > now() "
                "ORDER BY created_at DESC LIMIT 100",
                (scope_text,),
            )
            rows = await cur.fetchall()
    return [
        {"apv_id": r[0], "run_id": r[1], "origin": r[2], "action_class": r[3],
         "payload": r[4],
         "expires_at": r[5].isoformat() if r[5] else None,
         "created_at": r[6].isoformat() if r[6] else None}
        for r in rows
    ]
