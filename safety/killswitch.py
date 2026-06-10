"""Kill switch — the fail-closed Redis reader + setter the PDP gate consults on every decide.

`specs/09-permission-system/architecture.md §6`: two Redis keys —

    safety:kill:global          kills ALL agent actions system-wide
    safety:kill:agent:{name}    kills a specific agent

Both are checked on every `decide()` (sub-millisecond GET). FAIL-CLOSED: if the key state is
UNREADABLE (Redis down / timeout / absent), every side-effecting action degrades to `suggest_only`
(the PDP returns DENY `kill_switch_unreadable`). Reads still flow. There is no silent fail-open.

`read_kill` is the hot-path read (returns booleans the gate threads into the pure PDPRequest).
`set_kill` / `clear_kill` / `kill_status` back the `POST/DELETE/GET /api/v1/safety/kill` panic surface;
`set_kill` emits `safety.kill.triggered` on CURLYOS_SAFETY (architecture §6 fan-out).

Redis is async (`redis.asyncio`, opened in app lifespan). All reads are bounded so a slow/dead Redis
DENYs within the budget rather than hanging the request.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger("curlyos-core.safety.kill")

KEY_GLOBAL = "safety:kill:global"


def _agent_key(agent: str) -> str:
    return f"safety:kill:agent:{agent}"


# TD-3 fail-closed read budget: a slow/dead Redis must resolve to `unreadable` within this, not hang.
_READ_BUDGET_S = 0.05


async def read_kill(redis: Any, agent: str | None = None) -> tuple[bool, bool, bool]:
    """Resolve the kill state. Returns `(kill_global, kill_agent, kill_unreadable)`.

    FAIL-CLOSED: a missing client (`redis is None`) or ANY read error/timeout → `unreadable=True`
    (the gate then DENYs the action, degraded to suggest_only). A key being *present* (any value) is
    a kill; absent is alive. `kill_agent` is only meaningful when `agent` is supplied.
    """
    if redis is None:
        return False, False, True
    try:
        async def _read() -> tuple[Any, Any]:
            g = await redis.get(KEY_GLOBAL)
            a = await redis.get(_agent_key(agent)) if agent else None
            return g, a

        g, a = await asyncio.wait_for(_read(), timeout=_READ_BUDGET_S)
        return g is not None, a is not None, False
    except Exception as exc:  # noqa: BLE001 — any failure is fail-closed unreadable
        log.warning("kill-switch read failed (fail-closed → unreadable): %s", exc)
        return False, False, True


async def set_kill(
    redis: Any,
    pool: Any,
    publisher: Any,
    *,
    scope_text: str,
    agent: str | None = None,
    set_by: str,
) -> dict[str, Any]:
    """Set the kill key (global, or per-agent when `agent` given) and emit `safety.kill.triggered`.

    Raises `RuntimeError` if Redis is unavailable — a panic button that can't engage must surface
    loudly, never silently no-op. The Redis SET comes FIRST: the kill engages
    even if the event record fails (event is best-effort, kill is not).
    """
    if redis is None:
        raise RuntimeError("redis unavailable — cannot engage kill switch")
    key = _agent_key(agent) if agent else KEY_GLOBAL
    target = f"agent:{agent}" if agent else "global"
    await redis.set(key, set_by)

    if publisher is not None and pool is not None:
        from agent.pdp_gate import scope_parts  # noqa: PLC0415
        from shared.events import build_event  # noqa: PLC0415

        parts = scope_parts(scope_text)
        ev = build_event(
            short_type="safety.kill.triggered",
            subject=key,
            scope=parts,
            data={"scope": target, "set_by": set_by},
            actor=f"user:{parts['user_id']}",
            source="curlyos-core/safety",
        )
        try:
            async with pool.connection() as conn:
                _id, subject, stamped = await publisher.stage(ev, conn)
            await publisher.emit(subject, stamped)
        except Exception as exc:  # noqa: BLE001 — the kill is engaged; the event is best-effort
            log.warning("kill engaged but safety.kill.triggered emit failed: %s", exc)
    return {"killed": True, "scope": target, "set_by": set_by}


async def clear_kill(redis: Any, *, agent: str | None = None) -> dict[str, Any]:
    """Clear the kill key (manual recovery). Returns whether a key was actually removed."""
    if redis is None:
        raise RuntimeError("redis unavailable — cannot clear kill switch")
    key = _agent_key(agent) if agent else KEY_GLOBAL
    removed = await redis.delete(key)
    return {"killed": False, "scope": f"agent:{agent}" if agent else "global", "cleared": bool(removed)}


async def kill_status(redis: Any, agent: str | None = None) -> dict[str, Any]:
    """Report the current kill state for the panic surface (`GET /safety/kill`)."""
    g, a, unreadable = await read_kill(redis, agent)
    return {"global": g, "agent": a if agent else None, "unreadable": unreadable}
