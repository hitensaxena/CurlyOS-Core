"""Memory governance — the four write/lifecycle verbs.

record_episode() — append provenance ground-truth
add()           — fast append-only fact insert (hot path)
invalidate()    — soft-invalidate: set valid_to, never DELETE
forget()        — hard-forget: redact body, keep tombstone (gated by approval)

Ported from ~/curlyos/core/curlyos/memory/governance.py.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from shared.types.ulid import mint, is_valid
from shared.events import build_event

log = logging.getLogger("curlyos.memory.governance")

_FK_VIOLATION = "23503"
_REDACTED = "[REDACTED]"
_FORGET_ACTION_CLASS = "memory_forget_hard"
_FORGET_LOCK_NS = 0x4D34F06D  # "M4 forget"


# ── Errors ──────────────────────────────────────────────────────────────────

class SourceEpisodeNotFound(ValueError):
    def __init__(self, epi_id: str) -> None:
        super().__init__(f"source_episode_id does not reference an existing episode: {epi_id!r}")
        self.epi_id = epi_id


class StatementReserved(ValueError):
    def __init__(self) -> None:
        super().__init__("statement must not be the reserved redaction sentinel '[REDACTED]'")


class MemoryNotFound(ValueError):
    def __init__(self, mem_id: str) -> None:
        super().__init__(f"no memory {mem_id!r} in scope")
        self.mem_id = mem_id


class AlreadyInvalidated(ValueError):
    def __init__(self, mem_id: str) -> None:
        super().__init__(f"memory {mem_id!r} is already invalidated")
        self.mem_id = mem_id


class SupersededByNotFound(ValueError):
    def __init__(self, mem_id: str) -> None:
        super().__init__(f"superseded_by does not reference an existing memory: {mem_id!r}")
        self.mem_id = mem_id


class ForgetRequiresApproval(PermissionError):
    def __init__(self, approval_id: str) -> None:
        super().__init__(f"no granted memory_forget_hard approval {approval_id!r}")
        self.approval_id = approval_id


class ApprovalAlreadyUsed(PermissionError):
    def __init__(self, approval_id: str) -> None:
        super().__init__(f"approval {approval_id!r} already consumed by a prior forget")
        self.approval_id = approval_id


class AlreadyForgotten(ValueError):
    def __init__(self, mem_id: str) -> None:
        super().__init__(f"memory {mem_id!r} is already forgotten (tombstoned)")
        self.mem_id = mem_id


# ── Helpers ─────────────────────────────────────────────────────────────────

def statement_key(statement: str) -> str:
    """Normalised key for overlap detection."""
    return re.sub(r"\s+", " ", statement.strip().lower()).rstrip(" .!?,;:")


def _scope_obj(scope_text: str) -> dict[str, Any]:
    level, _, ident = scope_text.partition(":")
    return {"level": level or "user", "user_id": ident or scope_text}


async def _emit(publisher: Any, subject: str, ev: dict, type_str: str) -> None:
    try:
        await publisher.emit(subject, ev)
    except Exception:
        log.warning("NATS emit failed post-commit for %s (durable in events table)", type_str)


# ── record_episode ───────────────────────────────────────────────────────────

async def record_episode(
    pool: Any,
    publisher: Any,
    scope_text: str,
    content: str,
    source_ref: str | None = None,
    modality: str = "text",
) -> dict:
    """Insert `episodes` row (sync) → publish `memory.episode.recorded`.

    Returns dict with epi_id + ingested_at.
    """
    epi_id = mint("epi")

    data: dict[str, Any] = {"epi_id": epi_id, "scope": scope_text}
    if source_ref is not None:
        data["source_ref"] = source_ref

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO episodes (id, scope, content, source_ref) "
                "VALUES (%s, %s, %s, %s) RETURNING ingested_at",
                (epi_id, scope_text, content, source_ref),
            )
            (ingested_at,) = await cur.fetchone()

        ev = build_event(
            short_type="memory.episode.recorded",
            subject=epi_id,
            scope=_scope_obj(scope_text),
            data=data,
            actor=f"system",
            source="curlyos-core/memory",
        )
        _stored, subject, stamped = await publisher.stage(ev, conn)

    await _emit(publisher, subject, stamped, ev["type"])
    return {"epi_id": epi_id, "ingested_at": ingested_at}


# ── add ─────────────────────────────────────────────────────────────────────

async def add(
    pool: Any,
    publisher: Any,
    scope_text: str,
    statement: str,
    source_episode_id: str,
    kind: str = "fact",
    tier: str = "semantic",
    epistemic_status: str = "canonical",
    valid_from: datetime | None = None,
) -> dict:
    """FAST PATH — append-only fact insert. No dedup, no graph write.

    Returns FactRef dict.
    """
    if not is_valid("epi", source_episode_id):
        raise SourceEpisodeNotFound(source_episode_id)
    if statement == _REDACTED:
        raise StatementReserved()

    mem_id = mint("mem")
    skey = statement_key(statement)

    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO memories "
                    "(id, scope, statement, statement_key, kind, tier, embedding, "
                    " epistemic_status, valid_from, valid_to, ingested_at, source_episode_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, NULL, "
                    "        %s, COALESCE(%s, now()), NULL, now(), %s) "
                    "RETURNING valid_from, ingested_at",
                    (mem_id, scope_text, statement, skey, kind, tier,
                     epistemic_status, valid_from, source_episode_id),
                )
                vf, ia = await cur.fetchone()

            ev = build_event(
                short_type="memory.fact.stored",
                subject=mem_id,
                scope=_scope_obj(scope_text),
                data={"mem_id": mem_id, "scope": scope_text,
                      "valid_from": vf.isoformat(), "source_episode_id": source_episode_id},
                actor="system",
                source="curlyos-core/memory",
            )
            _stored, subject, stamped = await publisher.stage(ev, conn)
    except Exception as exc:
        if getattr(exc, "sqlstate", None) == _FK_VIOLATION:
            raise SourceEpisodeNotFound(source_episode_id) from exc
        raise

    await _emit(publisher, subject, stamped, ev["type"])
    return {"mem_id": mem_id, "valid_from": vf, "ingested_at": ia,
            "source_episode_id": source_episode_id}


# ── invalidate ──────────────────────────────────────────────────────────────

async def invalidate(
    pool: Any,
    publisher: Any,
    scope_text: str,
    mem_id: str,
    superseded_by: str | None = None,
    reason: str | None = None,
) -> dict:
    """Soft-invalidate: close the open interval. NEVER deletes the row."""
    if superseded_by is not None and not is_valid("mem", superseded_by):
        raise SupersededByNotFound(superseded_by)

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT valid_to FROM memories WHERE id = %s AND scope = %s",
                (mem_id, scope_text),
            )
            existing = await cur.fetchone()
        if existing is None:
            raise MemoryNotFound(mem_id)
        if existing[0] is not None:
            raise AlreadyInvalidated(mem_id)

        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE memories SET valid_to = now(), superseded_by = %s "
                    "WHERE id = %s AND scope = %s AND valid_to IS NULL "
                    "RETURNING valid_to, superseded_by",
                    (superseded_by, mem_id, scope_text),
                )
                updated = await cur.fetchone()
            if updated is None:
                raise AlreadyInvalidated(mem_id)
            valid_to, sup_by = updated

            ev = build_event(
                short_type="memory.fact.invalidated",
                subject=mem_id,
                scope=_scope_obj(scope_text),
                data={"mem_id": mem_id, "scope": scope_text,
                      "valid_to": valid_to.isoformat(), "superseded_by": sup_by, "hard": False},
                actor="system",
                source="curlyos-core/memory",
            )
            _stored, subject, stamped = await publisher.stage(ev, conn)
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == _FK_VIOLATION:
                raise SupersededByNotFound(superseded_by) from exc
            raise

    await _emit(publisher, subject, stamped, ev["type"])
    return {"mem_id": mem_id, "valid_to": valid_to, "superseded_by": sup_by, "deleted": False}


# ── forget ──────────────────────────────────────────────────────────────────

async def forget(
    pool: Any,
    publisher: Any,
    scope_text: str,
    mem_id: str,
    approval_id: str,
    reason: str,
) -> dict:
    """Hard-forget: redact body, keep tombstone. Requires approval."""
    from shared.events import full_type

    invalidated_type = full_type("memory.fact.invalidated")

    async with pool.connection() as conn:
        # Serialise concurrent forgets sharing this approval
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                (_FORGET_LOCK_NS, approval_id),
            )

        # Gate 1: granted, unexpired approval for this action class in same scope
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM approvals a JOIN agent_runs r ON a.run_id = r.id "
                "WHERE a.id = %s AND a.state = 'granted' AND a.action_class = %s "
                "AND a.expires_at > now() AND r.scope = %s LIMIT 1",
                (approval_id, _FORGET_ACTION_CLASS, scope_text),
            )
            if await cur.fetchone() is None:
                raise ForgetRequiresApproval(approval_id)

        # Gate 2: single-use (check if approval already authorised a forget)
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM events WHERE type = %s AND data->>'approval_id' = %s LIMIT 1",
                (invalidated_type, approval_id),
            )
            if await cur.fetchone() is not None:
                raise ApprovalAlreadyUsed(approval_id)

        # Resource checks
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT statement, valid_to FROM memories WHERE id = %s AND scope = %s",
                (mem_id, scope_text),
            )
            row = await cur.fetchone()
        if row is None:
            raise MemoryNotFound(mem_id)
        if row[0] == _REDACTED:
            raise AlreadyForgotten(mem_id)

        # Redact + close interval
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE memories "
                "SET statement = %s, statement_key = %s, valid_to = COALESCE(valid_to, now()) "
                "WHERE id = %s AND scope = %s AND statement <> %s "
                "RETURNING valid_to",
                (_REDACTED, _REDACTED, mem_id, scope_text, _REDACTED),
            )
            updated = await cur.fetchone()
        if updated is None:
            raise AlreadyForgotten(mem_id)
        (valid_to,) = updated

        ev = build_event(
            short_type="memory.fact.invalidated",
            subject=mem_id,
            scope=_scope_obj(scope_text),
            data={"mem_id": mem_id, "scope": scope_text,
                  "valid_to": valid_to.isoformat(), "hard": True,
                  "approval_id": approval_id, "reason": reason},
            actor="system",
            source="curlyos-core/memory",
        )
        _stored, subject, stamped = await publisher.stage(ev, conn)

    await _emit(publisher, subject, stamped, ev["type"])
    return {"mem_id": mem_id, "tombstoned": True, "approval_id": approval_id}


# ── SoR read helpers (NOT ranked retrieval) ─────────────────────────────────

async def list_episodes(pool: Any, scope_text: str, limit: int = 100) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, content, source_ref, ingested_at FROM episodes "
                "WHERE scope = %s ORDER BY created_at DESC LIMIT %s",
                (scope_text, limit),
            )
            rows = await cur.fetchall()
    return [{"id": r[0], "content": r[1], "source_ref": r[2],
             "ingested_at": r[3].isoformat() if r[3] else None} for r in rows]


async def list_memories(pool: Any, scope_text: str, limit: int = 100) -> list[dict]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, statement, kind, valid_from, source_episode_id FROM memories "
                "WHERE scope = %s AND valid_to IS NULL ORDER BY created_at DESC LIMIT %s",
                (scope_text, limit),
            )
            rows = await cur.fetchall()
    return [{"id": r[0], "statement": r[1], "kind": r[2],
             "valid_from": r[3].isoformat() if r[3] else None,
             "source_episode_id": r[4]} for r in rows]
