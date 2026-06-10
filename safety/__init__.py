"""Safety spine — lifted from the validated Phase-1 build (Spike-04 GO 11/11).

pdp.py        — the PURE policy decision point: decide(PDPRequest) -> PDPDecision.
killswitch.py — fail-closed Redis kill flags (safety:kill:global / :agent:{name}).
budget.py     — the BudgetSnapshot seam the PDP reads (default snapshot until
                live Redis metering lands in Phase A).

The async I/O resolver that feeds the pure PDP lives in agent/pdp_gate.py.
Invariant (P6, non-negotiable): no side effect without a PDP verdict.
"""
from safety.pdp import (  # noqa: F401
    ActionClass,
    AutonomyLevel,
    BudgetSnapshot,
    CapabilityGrantClaims,
    PDPDecision,
    PDPRequest,
    PDPVerdict,
    SecurityRisk,
    class_floor,
    decide,
    resolve_level,
)
