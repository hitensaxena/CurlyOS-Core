"""Event envelope + publisher contract.

CloudEvents v1.0 envelope with typed-prefix ULID event ids. Events land in the
`events` Postgres table and nowhere else — there is no message broker in this
system (deviation DR-C1), and every consumer reads the table by `seq`.

Event type grammar: art.curlybrackets.curlyos.<domain>.<verb>
The set of legal types is closed; see shared/events/catalog.py.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from shared.events.catalog import FULL_TYPE_PREFIX, full_type, validate_short_type
from shared.types.ulid import mint

log = logging.getLogger(__name__)


def short_type(full: str) -> str:
    """Strip the prefix. Returns unchanged if not prefixed.

    Lenient counterpart of `catalog.short_of`, which raises on a foreign type.
    Both live here rather than being redefined per caller.
    """
    return full[len(FULL_TYPE_PREFIX):] if full.startswith(FULL_TYPE_PREFIX) else full


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
    """The publisher contract: stage the event row, then (optionally) announce it.

    `stage()` writes the event into `events` inside the caller's transaction, so
    the event and the state change it describes commit together or not at all.
    `emit()` runs post-commit. With no broker in this system the only
    implementation, `PgOnlyPublisher`, makes it a no-op.
    """

    async def stage(self, event: dict, conn: Any) -> tuple[str, str, dict]:
        """Insert event row into `events` inside the caller's transaction.

        Returns (event_id, subject, stamped_event).
        """
        raise NotImplementedError

    async def emit(self, subject: str, event: dict) -> None:
        """Post-commit notification hook (no-op without a broker)."""
        raise NotImplementedError

    def stamp(self, event: dict) -> dict:
        """Add id + time if not already present."""
        if "id" not in event:
            event["id"] = mint("evt")
        if "time" not in event:
            event["time"] = datetime.now(timezone.utc).isoformat()
        return event
