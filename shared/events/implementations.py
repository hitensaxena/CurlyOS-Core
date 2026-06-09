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
        # Use full scope string from data if available, otherwise reconstruct from scope object
        scope = data.get("scope") if isinstance(data, dict) else None
        if not scope:
            scope_obj = event.get("scope") or {}
            level = scope_obj.get("level", "user")
            uid = scope_obj.get("user_id", "")
            scope = f"{level}:{uid}" if uid else level

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
