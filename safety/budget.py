"""Budget snapshot — the Phase-1 metering seam the PDP reads.

`specs/09-permission-system/architecture.md §4`: the PDP enforces per-run / per-agent-day /
per-user-day budgets across four dims (tokens, tool_actions, usd_spend, wall_clock_seconds),
backed by Redis counters (`budget:{scope}:{dim}:{window}`) with a nightly Postgres reconciliation
into `budget_ledger`. Crossing a HARD limit → the PDP DENYs (`budget_hard_limit_exceeded`) and
triggers a per-agent kill.

Phase 1 does not yet meter live token/cost counters into Redis — the Executive only emits the three
low-cost memory action classes. So this module supplies a generous, under-limit DEFAULT snapshot
(consumed=0) that keeps the budget gate present and exercised (the PDP still computes headroom and
the hard-limit check runs) without standing up the counter pipeline. The real Redis read lands in a
later milestone; the shape (`BudgetSnapshot`) and the PDP's enforcement are already final.
"""
from __future__ import annotations

from safety.pdp import BudgetSnapshot

# Generous P1 ceilings — comfortably above a single confirm_each memory turn.
_DEFAULT_LIMITS = {
    "tokens_hard_limit": 1_000_000,
    "tool_actions_hard_limit": 1_000,
    "usd_spend_hard_limit": 50.0,
    "wall_clock_seconds_hard_limit": 3_600,
}


def default_budget_snapshot() -> BudgetSnapshot:
    """A zero-consumed, under-limit snapshot — the P1 default until live metering lands."""
    return BudgetSnapshot(
        tokens=0,
        tool_actions=0,
        usd_spend=0.0,
        wall_clock_seconds=0,
        **_DEFAULT_LIMITS,
    )
