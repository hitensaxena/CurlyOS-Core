"""The CLOSED event catalog — single source of truth for event types.

Adapted from the build repo's validated catalog (00b-shared-canon §8 grammar:
`art.curlybrackets.curlyos.<short>`), with two recorded changes for curlyos-core:

1. The catalog value is a DOMAIN GROUP (MEMORY / AGENTS / SAFETY / EVENTS /
   EVOLUTION), not a NATS stream — there is no NATS here (deviation DR-C1).
   Groups drive SSE filtering and any future bus mapping.
2. The set is the UNION of every type curlyos-core already emits (verified by
   grep at lift time — renaming live types would break event-log continuity)
   plus the agent/safety types the Phase-A runtime emits.

CLOSED means closed: `build_event()` rejects a short type not listed here.
Adding an event type is a deliberate one-line registration in this file,
never string improv at the call site.
"""
from __future__ import annotations

FULL_TYPE_PREFIX = "art.curlybrackets.curlyos."
SUBJECT_PREFIX = "curlyos."

# short type → domain group.
EVENT_CATALOG: dict[str, str] = {
    # ── memory / identity / knowledge (live) ────────────────────────────────
    "memory.episode.recorded": "MEMORY",
    "memory.fact.stored": "MEMORY",
    "memory.fact.consolidated": "MEMORY",
    "memory.fact.invalidated": "MEMORY",
    "identity.fact.updated": "MEMORY",
    "knowledge.entity.created": "MEMORY",
    "knowledge.entity.invalidated": "MEMORY",
    "knowledge.edge.created": "MEMORY",
    "knowledge.edge.invalidated": "MEMORY",
    # ── cognition (live) ─────────────────────────────────────────────────────
    "metacog.assumption.created": "MEMORY",
    "metacog.model.created": "MEMORY",
    "cognition.reflection.completed": "MEMORY",
    "cognition.audit.completed": "MEMORY",
    "cognition.meta.models_generated": "MEMORY",
    "memory.consolidation.fast": "MEMORY",
    "memory.consolidation.deep": "MEMORY",
    # ── creative / exploration (live) ────────────────────────────────────────
    "studio.created": "EVENTS",
    "studio.sketch.created": "EVENTS",
    "studio.sketch.updated": "EVENTS",
    "studio.sketch.invalidated": "EVENTS",
    "studio.sketch.graduated": "EVENTS",
    "studio.sketches.linked": "EVENTS",
    "simulation.run.created": "EVENTS",
    "simulation.run.completed": "EVENTS",
    "simulation.run.forked": "EVENTS",
    # ── goal OS (Phase G) ────────────────────────────────────────────────────
    "goal.created": "EVENTS",
    "goal.updated": "EVENTS",
    "goal.invalidated": "EVENTS",
    "goal.derived": "EVENTS",
    # ── autonomous loop lifecycle (opportunity → goal → plan → task → verify) ──
    "goal.plan.proposed": "EVENTS",
    "goal.plan.approved": "EVENTS",
    "goal.task.dispatched": "EVENTS",
    "goal.task.verified": "EVENTS",
    "goal.task.retry": "EVENTS",
    "goal.progress": "EVENTS",
    "goal.achieved": "EVENTS",
    "goal.needs_work": "EVENTS",
    "decision.recorded": "EVENTS",
    "decision.reviewed": "EVENTS",
    "opportunity.detected": "EVENTS",
    "opportunity.resolved": "EVENTS",
    # ── agent runtime (Phase A emits; lifted from build repo) ───────────────
    "agent.run.started": "AGENTS",
    "agent.run.completed": "AGENTS",
    "agent.run.failed": "AGENTS",
    "runtime.action.executed": "AGENTS",
    "runtime.observation.recorded": "AGENTS",
    "tool.call.invoked": "AGENTS",
    # ── evolution (Phase E) ──────────────────────────────────────────────────
    "evolution.candidate.proposed": "EVOLUTION",
    "evolution.eval.completed": "EVOLUTION",
    "evolution.candidate.held": "EVOLUTION",
    "evolution.prompt.activated": "EVOLUTION",
    # ── safety (lifted from build repo) ──────────────────────────────────────
    "safety.approval.requested": "SAFETY",
    "safety.approval.granted": "SAFETY",
    "safety.approval.denied": "SAFETY",
    "safety.approval.expired": "SAFETY",
    "safety.kill.triggered": "SAFETY",
    "safety.budget.exceeded": "SAFETY",
    "safety.pdp.unavailable": "SAFETY",
}


class UnknownEventType(KeyError):
    """A short type not in the closed catalog (register it in catalog.py)."""


def short_of(full_type: str) -> str:
    """'art.curlybrackets.curlyos.memory.fact.stored' -> 'memory.fact.stored'."""
    if not full_type.startswith(FULL_TYPE_PREFIX):
        raise UnknownEventType(f"not a curlyos event type: {full_type!r}")
    return full_type[len(FULL_TYPE_PREFIX):]


def subject_for(short: str) -> str:
    """'memory.fact.stored' -> 'curlyos.memory.fact.stored'."""
    return SUBJECT_PREFIX + short


def group_for(type_or_short: str) -> str:
    """Resolve the domain group for a full or short type (CLOSED — raises if unknown)."""
    short = short_of(type_or_short) if type_or_short.startswith(FULL_TYPE_PREFIX) else type_or_short
    try:
        return EVENT_CATALOG[short]
    except KeyError:
        raise UnknownEventType(
            f"event type not in the closed catalog: {short!r} — register it in shared/events/catalog.py"
        ) from None


def validate_short_type(short: str) -> str:
    """Assert membership in the closed catalog; returns the short type unchanged."""
    if short not in EVENT_CATALOG:
        raise UnknownEventType(
            f"event type not in the closed catalog: {short!r} — register it in shared/events/catalog.py"
        )
    return short
