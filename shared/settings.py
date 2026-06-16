"""Tiny async key/value runtime settings over the app_settings table.

Used for global toggles like the agent bypass mode. Values are jsonb, so any
JSON-serializable value works. Reads are cheap (single PK lookup); hot-path
callers use `get_setting_cached` (short in-process TTL).

SETTINGS_REGISTRY makes the knobs self-describing: type, default, description and
category. The /api/settings endpoints validate writes against it and expose
defaults, so a setting that has never been written still reports its effective
value. Keys NOT in the registry are still readable/writable (free-form), but the
registry is the documented, validated surface.
"""
from __future__ import annotations

import time
from typing import Any

AGENT_BYPASS = "agent_bypass"  # bool: run agent side effects without approval

# key -> (type, default, category, description). type ∈ {bool, int, float, str}.
SETTINGS_REGISTRY: dict[str, dict[str, Any]] = {
    # autonomy
    "auto_promote": {"type": "bool", "default": True, "category": "autonomy",
                     "description": "Promote high-scoring opportunities into goals automatically."},
    "auto_plan": {"type": "bool", "default": True, "category": "autonomy",
                  "description": "Decompose active goals into plans automatically (autoplan sweep)."},
    # safety
    "agent_bypass": {"type": "bool", "default": False, "category": "safety",
                     "description": "Run agent side effects without human approval. Use with care."},
    # recall
    "recall_cache_enabled": {"type": "bool", "default": True, "category": "recall",
                             "description": "Cache /api/recall results in Redis (per-scope, generation-invalidated)."},
    "recall_cache_ttl_seconds": {"type": "int", "default": 120, "category": "recall",
                                 "description": "TTL for cached recall results, in seconds (1–3600)."},
    "recall_fast_followups": {"type": "bool", "default": False, "category": "recall",
                              "description": "Allow the agentic coverage-gap follow-up round in fast mode (slower, more thorough)."},
    # ingest
    "epistemic_classify_enabled": {"type": "bool", "default": True, "category": "ingest",
                                   "description": "Run the per-ingest LLM epistemic classification (canonical/belief/hypothesis)."},
    "kg_extraction_enabled": {"type": "bool", "default": True, "category": "ingest",
                              "description": "Run knowledge-graph extraction (entities/edges) on ingest."},
}

_ALLOWED_TYPES = {"bool", "int", "float", "str"}


def coerce_value(key: str, raw: Any) -> Any:
    """Validate/coerce a value against the registry. Raises ValueError on bad input.

    Unregistered keys pass through unchanged (free-form). Registered keys are
    coerced to their declared type, and registered int/float keys are range-checked
    where a sensible bound applies.
    """
    spec = SETTINGS_REGISTRY.get(key)
    if spec is None:
        return raw
    t = spec["type"]
    if t == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            if raw.lower() in ("true", "1", "yes", "on"):
                return True
            if raw.lower() in ("false", "0", "no", "off"):
                return False
        if isinstance(raw, (int, float)):
            return bool(raw)
        raise ValueError(f"{key} expects a boolean")
    if t == "int":
        try:
            v = int(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{key} expects an integer")
        if key == "recall_cache_ttl_seconds" and not (1 <= v <= 3600):
            raise ValueError("recall_cache_ttl_seconds must be 1–3600")
        return v
    if t == "float":
        try:
            return float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{key} expects a number")
    return str(raw)


async def get_setting(pool: Any, key: str, default: Any = None) -> Any:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT value FROM app_settings WHERE key = %s", (key,))
            row = await cur.fetchone()
    if row is not None:
        return row[0]
    if default is None and key in SETTINGS_REGISTRY:
        return SETTINGS_REGISTRY[key]["default"]
    return default


# In-process cache for hot-path reads (recall/ingest). Short TTL so a settings
# change takes effect within a few seconds without a DB hit per request.
_CACHE: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 10.0


async def get_setting_cached(pool: Any, key: str, default: Any = None) -> Any:
    now = time.time()
    hit = _CACHE.get(key)
    if hit is not None and (now - hit[0]) < _CACHE_TTL:
        return hit[1]
    val = await get_setting(pool, key, default)
    _CACHE[key] = (now, val)
    return val


def invalidate_cache(key: str | None = None) -> None:
    if key is None:
        _CACHE.clear()
    else:
        _CACHE.pop(key, None)


async def set_setting(pool: Any, key: str, value: Any) -> None:
    from psycopg.types.json import Jsonb
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (%s, %s, now()) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                (key, Jsonb(value)),
            )
    invalidate_cache(key)


async def all_settings(pool: Any) -> dict[str, Any]:
    """Every registered setting with effective value + metadata, plus any
    free-form keys present in the table."""
    stored: dict[str, Any] = {}
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT key, value, updated_at FROM app_settings")
            for k, v, updated in await cur.fetchall():
                stored[k] = (v, updated)
    out: dict[str, Any] = {}
    for key, spec in SETTINGS_REGISTRY.items():
        sv = stored.get(key)
        out[key] = {
            "value": sv[0] if sv else spec["default"],
            "default": spec["default"],
            "type": spec["type"],
            "category": spec["category"],
            "description": spec["description"],
            "is_default": sv is None,
            "updated_at": sv[1].isoformat() if sv and sv[1] else None,
        }
    # surface free-form keys not in the registry
    for key, (v, updated) in stored.items():
        if key not in out:
            out[key] = {"value": v, "default": None, "type": None, "category": "other",
                        "description": "(unregistered)", "is_default": False,
                        "updated_at": updated.isoformat() if updated else None}
    return out
