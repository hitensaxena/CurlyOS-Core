"""Audit substrate — actions / observations / hash-chained tool_calls.

Lifted from the build repo's executive.py (the validated parts that are not
control flow). The chain invariant: per run,

    entry_hash = H(prev_hash || canonical({tool, args, result_hash}))

so changing any prior entry, the tool, the args, or the result changes every
subsequent entry_hash — replayed/duplicated side effects are detectable, which
is what lets a checkpoint-resumed run prove it didn't double-execute.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _sha(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def chain_entry(prev_hash: bytes, tool: str, args: dict, result: dict) -> tuple[bytes, bytes]:
    """Pure hash-chain step: return (result_hash, entry_hash).

    `prev_hash` is b"" at the head of a run's chain."""
    result_hash = _sha(_canonical(result))
    canonical_row = _canonical({"tool": tool, "args": args, "result_hash": result_hash.hex()})
    entry_hash = _sha(bytes(prev_hash or b"") + canonical_row)
    return result_hash, entry_hash


async def insert_action(conn: Any, action_id: str, run_id: str, kind: str, payload: dict) -> None:
    from psycopg.types.json import Jsonb  # noqa: PLC0415

    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO actions (id, run_id, kind, payload) VALUES (%s, %s, %s, %s)",
            (action_id, run_id, kind, Jsonb(payload)),
        )


async def insert_observation(conn: Any, obs_id: str, action_id: str, result: dict) -> None:
    from psycopg.types.json import Jsonb  # noqa: PLC0415

    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO observations (id, action_id, result) VALUES (%s, %s, %s)",
            (obs_id, action_id, Jsonb(result)),
        )


async def insert_tool_call(conn: Any, run_id: str, tcl_id: str, action_id: str, tool: str,
                           args: dict, result: dict) -> bytes:
    """Append a hash-chained tool_call (chained per-run). Returns the new entry_hash."""
    from psycopg.types.json import Jsonb  # noqa: PLC0415

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT tc.entry_hash FROM tool_calls tc JOIN actions a ON tc.action_id = a.id "
            "WHERE a.run_id = %s ORDER BY tc.created_at DESC, tc.id DESC LIMIT 1",
            (run_id,),
        )
        row = await cur.fetchone()
    prev_hash = row[0] if row and row[0] is not None else b""
    result_hash, entry_hash = chain_entry(bytes(prev_hash), tool, args, result)
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO tool_calls (id, action_id, tool, args, result_hash, prev_hash, entry_hash) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (tcl_id, action_id, tool, Jsonb(args), result_hash,
             bytes(prev_hash) or None, entry_hash),
        )
    return entry_hash
