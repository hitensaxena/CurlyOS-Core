"""PDP gate — the async I/O resolver around the pure PDP (safety/pdp.py).

`safety.pdp.decide()` is a pure function (no clock, no RNG, no I/O). The spec
says the PDP reads the kill-switch from Redis on every decide and the
capability grant / approval state from Postgres, fail-closed. To keep
`decide()` pure, that I/O lives HERE:

    resolve ambient state (kill via Redis, capability grant, approval state,
    budget snapshot) → build the canonical PDPRequest → decide()
        → if REQUIRE_APPROVAL: mint the `apv_`, INSERT the pending approvals
          row + emit `safety.approval.requested` (one transaction), stamp
          `apv_id` onto the decision.

The gate is the ONLY place a side effect attaches to a verdict. Every
Executive/workflow action and every resume passes through here, so
"every side effect passes the PDP" (P6) holds.

Adaptations from the build repo (recorded, curlyos-final/04 §1):
  * scope is curlyos-core's plain scope string ("user:usr_hiten"), not an
    object — `scope_parts()` derives the event-scope dict and user_id.
  * approvals INSERT carries scope + origin='agent' (the 0002 schema), so
    scope checks never need the agent_runs join.
  * approval TTL from env CURLYOS_APPROVAL_TTL_SECONDS (default 7 days, the
    spec's hard expiry) instead of a settings object.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from safety.budget import default_budget_snapshot
from safety.killswitch import read_kill
from safety.pdp import (
    AutonomyLevel,
    CapabilityGrantClaims,
    CapGrantFsPolicy,
    CapGrantNetPolicy,
    PDPDecision,
    PDPRequest,
    PDPVerdict,
    decide,
)

log = logging.getLogger("curlyos-core.agent.pdp_gate")

# Phase-A grant: the memory verbs + file_edit. Every other class is
# deny-by-default (absent from `tools` → DENY capability_grant_missing).
# file_edit's floor is bounded_auto, so at the Phase-A confirm_each ceiling it
# clamps to REQUIRE_APPROVAL — writes park for approval like memory writes do.
PHASE_A_GRANTED_TOOLS = ["read", "memory_write", "memory_forget_hard", "file_edit"]

# Chat/runs are user-scoped (not bound to a workspace); identity only.
_USER_SCOPE_WORKSPACE_ID = "ws_user_scope"

_DEFAULT_APPROVAL_TTL = 7 * 24 * 3600  # the spec's 7-day hard expiry


def approval_ttl_seconds() -> int:
    try:
        return int(os.environ.get("CURLYOS_APPROVAL_TTL_SECONDS", _DEFAULT_APPROVAL_TTL))
    except ValueError:
        return _DEFAULT_APPROVAL_TTL


def scope_parts(scope_text: str) -> dict[str, str]:
    """'user:usr_hiten' → {'level': 'user', 'user_id': 'usr_hiten'} (event scope)."""
    level, sep, rest = scope_text.partition(":")
    if not sep:
        return {"level": "user", "user_id": scope_text}
    return {"level": level, "user_id": rest}


def phase_a_capability_grant(
    run_id: str, scope_text: str, agent: str = "Executive",
    grant_id: str = "cap_phase_a_executive",
) -> CapabilityGrantClaims:
    """The deny-by-default capability grant a run carries."""
    return CapabilityGrantClaims(
        grant_id=grant_id,
        agent=f"agent:{agent}",
        run_id=run_id,
        scope=scope_text,
        tools=list(PHASE_A_GRANTED_TOOLS),
        fs=CapGrantFsPolicy(),
        net=CapGrantNetPolicy(),
        memory_scope=[scope_text],
        max_autonomy=AutonomyLevel.CONFIRM_EACH,  # Phase-A ceiling
    )


async def _approval_state(pool: Any, approval_id: str | None, action_class: str,
                          run_id: str) -> str | None:
    """Resolve the state of a specific approval (the resume path passes the granted apv_).

    Bound to (run_id, action_class) so an approval can only upgrade the SAME
    run's matching action — a granted apv_ from a different run can never
    satisfy this run's gate (defense in depth)."""
    if approval_id is None:
        return None
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT state FROM approvals WHERE id = %s AND run_id = %s AND action_class = %s "
                "AND expires_at > now() LIMIT 1",
                (approval_id, run_id, action_class),
            )
            row = await cur.fetchone()
    return row[0] if row else None


async def evaluate(
    *,
    pool: Any,
    redis: Any,
    publisher: Any,
    scope_text: str,
    run_id: str,
    action_id: str,
    action_class: str,
    autonomy_level: str,
    agent: str = "Executive",
    tool: str | None = None,
    args: dict[str, Any] | None = None,
    approval_id: str | None = None,
    create_approval: bool = True,
    force_dry_run: bool = False,
) -> PDPDecision:
    """Resolve ambient state, run the pure PDP, and (on REQUIRE_APPROVAL) record the approval.

    `autonomy_level` is the run's resolved level (the Phase-A ceiling is
    confirm_each); it feeds BOTH the agent_default and workspace_override
    inputs — the min()-clamp then takes the class floor into account.
    On the resume path, pass the granted `approval_id` and `create_approval=False`.
    """
    parts = scope_parts(scope_text)
    kill_global, kill_agent, kill_unreadable = await read_kill(redis, agent=agent)
    appr_state = await _approval_state(pool, approval_id, action_class, run_id) if approval_id else None

    req = PDPRequest(
        action_id=action_id,
        run_id=run_id,
        agent=agent,
        workspace_id=_USER_SCOPE_WORKSPACE_ID,
        user_id=parts["user_id"],
        action_class=action_class,  # type: ignore[arg-type] — coerced by pydantic
        tool=tool,
        args=args or {},
        agent_default_level=autonomy_level,        # type: ignore[arg-type]
        workspace_override_level=autonomy_level,    # type: ignore[arg-type]
        capability_grant=phase_a_capability_grant(run_id, scope_text, agent=agent),
        budget=default_budget_snapshot(),
        kill_global=kill_global,
        kill_agent=kill_agent,
        kill_unreadable=kill_unreadable,
        force_dry_run=force_dry_run,
        approval_state=appr_state,
    )
    decision = decide(req)

    # The ONLY side effect attached to a verdict: persist the approval + emit the request event.
    if decision.verdict == PDPVerdict.REQUIRE_APPROVAL and create_approval:
        decision.apv_id = await _create_approval(
            pool, publisher, scope_text,
            run_id=run_id, action_class=action_class, decision=decision,
            ttl=approval_ttl_seconds(),
        )
    return decision


async def _create_approval(
    pool: Any,
    publisher: Any,
    scope_text: str,
    *,
    run_id: str,
    action_class: str,
    decision: PDPDecision,
    ttl: int,
) -> str:
    """Mint an `apv_`, INSERT the pending approvals row + emit `safety.approval.requested` atomically."""
    from shared.events import build_event
    from shared.types.ulid import mint

    apv_id = mint("apv")
    parts = scope_parts(scope_text)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO approvals (id, run_id, origin, scope, action_class, state, expires_at) "
                "VALUES (%s, %s, 'agent', %s, %s, 'pending', now() + make_interval(secs => %s))",
                (apv_id, run_id, scope_text, action_class, ttl),
            )
        ev = build_event(
            short_type="safety.approval.requested",
            subject=apv_id,
            scope=parts,
            data={  # refs only (envelope <16KB rule)
                "apv_id": apv_id,
                "run_id": run_id,
                "action_class": action_class,
                "security_risk": decision.security_risk.value if decision.security_risk else None,
                "policy_version": decision.policy_version,
            },
            actor=f"user:{parts['user_id']}",
            source="curlyos-core/safety",
        )
        _stored, subject, stamped = await publisher.stage(ev, conn)  # approvals row + event, one tx
    try:
        await publisher.emit(subject, stamped)  # post-commit, best-effort
    except Exception as exc:  # noqa: BLE001 — durable in the events table
        log.warning("approval requested but emit failed for %s: %s", apv_id, exc)
    return apv_id
