"""Event envelope + publisher contract.

CloudEvents v1.0 envelope with typed-prefix ULID event ids.
All events land in the `events` Postgres table AND NATS JetStream.

Stream taxonomy:
  HITENOS_MEMORY  — memory.fact.stored|consolidated|invalidated, memory.episode.recorded
  HITENOS_AGENTS  — agent.run.* , agents.delegate.*
  HITENOS_SAFETY  — safety.approval.*
  HITENOS_EVOLUTION — evolution.eval.completed, evolution.selfmod.*, reflection.insight.produced
  HITENOS_EVENTS  — identity.fact.updated, attention.*, narrative.*, sense.*, discovery.*

Event type grammar: art.curlybrackets.curlyos.<domain>.<verb>
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from shared.types.ulid import mint

log = logging.getLogger(__name__)

_FULL_TYPE_PREFIX = "art.curlybrackets.curlyos."


def full_type(short_type: str) -> str:
    """Expand 'memory.fact.stored' → 'art.curlybrackets.curlyos.memory.fact.stored'."""
    return f"{_FULL_TYPE_PREFIX}{short_type}"


def short_type(full: str) -> str:
    """Strip the prefix. Returns unchanged if not prefixed."""
    return full[len(_FULL_TYPE_PREFIX):] if full.startswith(_FULL_TYPE_PREFIX) else full


def build_event(
    short_type: str,
    subject: str,
    scope: dict[str, Any],
    data: dict[str, Any],
    actor: str = "system",
    source: str = "curlyos-core",
) -> dict[str, Any]:
    """Build a CloudEvents v1.0 envelope.

    The short type must be registered in the CLOSED catalog
    (shared/events/catalog.py) — unknown types raise UnknownEventType."""
    from shared.events.catalog import validate_short_type

    validate_short_type(short_type)
    return {
        "specversion": "1.0",
        "type": full_type(short_type),
        "source": source,
        "id": mint("evt"),
        "time": datetime.now(timezone.utc).isoformat(),
        "subject": subject,
        "data": data,
        "actor": actor,
        "scope": scope,
    }


class EventPublisher:
    """Abstract publisher — stamps events to Postgres (SoR) and NATS JetStream (live).

    Subclasses implement `_stage_pg()` and `_emit_nats()`.
    The pattern from HitenOS: stage event row in the SAME DB transaction as the
    SoR write, then publish to NATS post-commit (non-fatal on failure).
    """

    async def stage(self, event: dict, conn: Any) -> tuple[str, str, dict]:
        """Insert event row into `events` table inside the caller's transaction.

        Returns (event_id, nats_subject, stamped_event).
        """
        raise NotImplementedError

    async def emit(self, subject: str, event: dict) -> None:
        """Publish to NATS JetStream (post-commit, best-effort)."""
        raise NotImplementedError

    def stamp(self, event: dict) -> dict:
        """Add id + time if not already present."""
        if "id" not in event:
            event["id"] = mint("evt")
        if "time" not in event:
            event["time"] = datetime.now(timezone.utc).isoformat()
        return event
