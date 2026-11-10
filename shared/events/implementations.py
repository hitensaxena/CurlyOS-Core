"""Concrete EventPublisher — Postgres SoR staging + optional NATS live publish.

PgNatsPublisher:
  stage()  — INSERT event row into `events` table in the caller's PG transaction
  emit()   — Publish to NATS JetStream post-commit (best-effort, non-fatal)

If NATS is unavailable, stage() still works (events are durable in PG),
and the consolidation worker reads from `events` table by seq, not NATS.
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


class PgNatsPublisher(EventPublisher):
    """Postgres-first event publisher with optional NATS spine.

    - stage(): INSERT into `events` table inside the caller's PG tx.
    - emit():  Publish to NATS JetStream post-commit (best-effort).
    """

    def __init__(self, nats_client: Any = None, stream: str = "CURLYOS_MEMORY"):
        self._nats = nats_client
        self._stream = stream

    async def stage(self, event: dict, conn: Any) -> tuple[str, str, dict]:
        """Insert event row into `events` table inside the caller's transaction."""
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
            (seq,) = await cur.fetchone()

        # Return (event_id, nats_subject, stamped_event)
        nats_subject = f"curlyos.{evt_type.split('.')[-1]}" if evt_type else "curlyos.unknown"
        return evt_id, nats_subject, event

    async def emit(self, subject: str, event: dict) -> None:
        """Publish to NATS JetStream (post-commit, best-effort)."""
        if self._nats is None:
            return  # No NATS — events are still durable in PG
        try:
            await self._nats.publish(
                subject,
                json.dumps(event, default=str).encode(),
            )
        except Exception as e:
            log.warning("NATS publish failed for %s: %s (event is durable in PG)", subject, e)

    def stamp(self, event: dict) -> dict:
        from shared.types.ulid import mint
        from datetime import datetime, timezone
        if "id" not in event:
            event["id"] = mint("evt")
        if "time" not in event:
            event["time"] = datetime.now(timezone.utc).isoformat()
        return event


class PgOnlyPublisher(PgNatsPublisher):
    """Postgres-only publisher — for development / testing without NATS."""

    def __init__(self):
        super().__init__(nats_client=None)
