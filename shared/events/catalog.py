"""The CLOSED event catalog — single source of truth for event types.

Adapted from the build repo's validated catalog (00b-shared-canon §8 grammar:
`art.curlybrackets.curlyos.<short>`), with two recorded changes for curlyos-core:

1. The catalog value is a DOMAIN GROUP (MEMORY / AGENTS / EVENTS),
   not a NATS stream — there is no NATS here (deviation DR-C1).
2. The set is exactly the types this repo emits today. The 2026-11-10 lean
   refactor deleted the studio / simulation / evolution / plan-and-task event
   families along with the packages that emitted them; the removal of `safety/`
   took `safety.kill.triggered` with it. Historical rows with those types remain
   in `events` and are still readable, they simply can no longer be written.

CLOSED means closed: `build_event()` rejects a short type not listed here.
Adding an event type is a deliberate one-line registration in this file,
never string improv at the call site.
"""
from __future__ import annotations

FULL_TYPE_PREFIX = "art.curlybrackets.curlyos."
SUBJECT_PREFIX = "curlyos."

# short type → domain group.
# Every entry below has a live emitter in this repository; anything without one
# was removed in the 2026-11-10 lean refactor.
EVENT_CATALOG: dict[str, str] = {
    # ── memory ──────────────────────────────────────────────────────────────
    "memory.episode.recorded": "MEMORY",
    "memory.fact.stored": "MEMORY",
    "memory.fact.consolidated": "MEMORY",
    "memory.fact.invalidated": "MEMORY",
    "memory.consolidation.fast": "MEMORY",
    "memory.consolidation.deep": "MEMORY",
    # ── identity ────────────────────────────────────────────────────────────
    "identity.fact.updated": "MEMORY",
    # ── knowledge graph ─────────────────────────────────────────────────────
    "knowledge.entity.created": "MEMORY",
    "knowledge.entity.invalidated": "MEMORY",
    "knowledge.edge.created": "MEMORY",
    "knowledge.edge.invalidated": "MEMORY",
    # ── cognition ───────────────────────────────────────────────────────────
    "metacog.assumption.created": "MEMORY",
    "metacog.model.created": "MEMORY",
    "cognition.reflection.completed": "MEMORY",
    "cognition.audit.completed": "MEMORY",
    "cognition.meta.models_generated": "MEMORY",
    # ── goals / decisions / opportunities ───────────────────────────────────
    "goal.created": "EVENTS",
    "goal.updated": "EVENTS",
    "goal.invalidated": "EVENTS",
    "decision.recorded": "EVENTS",
    "decision.reviewed": "EVENTS",
    "opportunity.detected": "EVENTS",
    "opportunity.resolved": "EVENTS",
    # ── scheduler bookkeeping (shared/scheduler.py) ─────────────────────────
    "agent.run.started": "AGENTS",
    "agent.run.completed": "AGENTS",
    "agent.run.failed": "AGENTS",
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
