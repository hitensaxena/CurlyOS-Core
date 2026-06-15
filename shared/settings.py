"""Tiny async key/value runtime settings over the app_settings table.

Used for global toggles like the agent bypass mode. Values are jsonb, so any
JSON-serializable value works. Reads are cheap (single PK lookup); callers that
read on a hot path can cache if needed.
"""
from __future__ import annotations

from typing import Any

AGENT_BYPASS = "agent_bypass"  # bool: run agent side effects without approval


async def get_setting(pool: Any, key: str, default: Any = None) -> Any:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
            row = await cur.fetchone()
    return row[0] if row else default


async def set_setting(pool: Any, key: str, value: Any) -> None:
    from psycopg.types.json import Jsonb
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, now()) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                (key, Jsonb(value)),
            )
