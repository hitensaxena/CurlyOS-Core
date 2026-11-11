"""The event publisher — Postgres staging only.

There is no message broker in this system (deviation DR-C1). `stage()` inserts
the event row into `events` inside the caller's transaction, so the event and
the state change it describes commit together or not at all. `emit()` exists
only to satisfy the publisher interface and does nothing: every consumer reads
`events` by `seq`.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from shared.events import EventPublisher

log = logging.getLogger(__name__)


def _stored_scope(scope_obj: Any, data: Any) -> str:
    """Resolve the `events.scope` text for one event.

    Accepts every shape the call sites have ever produced, because a mismatch
    here is silent: the row is written and simply never matches a scoped query.
      {"level": "user", "user_id": "usr_x"}   → "user:usr_x"   (canonical)
      {"scope": "user:usr_x"}                 → "user:usr_x"   (legacy envelope)
      "user:usr_x"                            → "user:usr_x"   (bare string)
    A bare level (no id) is only used when nothing carries a user id — never in
    preference to one, which is how 34k events ended up scoped to "user".
    """
    if isinstance(scope_obj, str) and ":" in scope_obj:
        return scope_obj
    scope_obj = scope_obj if isinstance(scope_obj, dict) else {}
    level = scope_obj.get("level") or "user"
    uid = scope_obj.get("user_id") or ""
    if uid:
        return f"{level}:{uid}"
    nested = scope_obj.get("scope")
    if isinstance(nested, str) and ":" in nested:
        return nested
    data_scope = data.get("scope") if isinstance(data, dict) else None
    if isinstance(data_scope, str) and ":" in data_scope:
        return data_scope
    if isinstance(data_scope, dict) and data_scope.get("user_id"):
        return f"{data_scope.get('level') or 'user'}:{data_scope['user_id']}"
    return level


class PgOnlyPublisher(EventPublisher):
    """Postgres-only publisher. The `events` table is both the SoR and the transport."""

    async def stage(self, event: dict, conn: Any) -> tuple[str, str, dict]:
        """Insert the event row inside the caller's transaction."""
        evt_id = event.get("id", "")
        evt_type = event.get("type", "")
        subject = event.get("subject", "")
        data = event.get("data", {})
        scope = _stored_scope(event.get("scope"), data)

        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO events (id, type, subject, scope, data) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING seq",
                (evt_id, evt_type, subject, scope, json.dumps(data)),
            )
            await cur.fetchone()

        return evt_id, subject, event

    async def emit(self, subject: str, event: dict) -> None:
        """No-op — there is no broker. Kept so callers need not branch."""

    def stamp(self, event: dict) -> dict:
        from datetime import datetime, timezone

        from shared.types.ulid import mint

        if "id" not in event:
            event["id"] = mint("evt")
        if "time" not in event:
            event["time"] = datetime.now(timezone.utc).isoformat()
        return event
