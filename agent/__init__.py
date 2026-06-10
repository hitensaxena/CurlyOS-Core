"""Agent runtime substrate — lifted from the validated Phase-1 build.

hashchain.py        — tamper-evident tool_calls chain + action/observation rows.
pdp_gate.py         — the async I/O resolver around the pure PDP (safety/pdp.py).
approval_service.py — grant / deny / list the human-in-the-loop approvals.

The run loop itself (plan→act→observe, parking, resume) is NOT here — it is
implemented with LangGraph in Phase A (orchestration/), per curlyos-final/06.
These modules are the loop's audited substrate, usable by both the Phase-A
Executive and human-originated flows (e.g. webapp forget approvals) today.
"""
