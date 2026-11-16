"""Scope-string helpers shared by the memory, goal and scheduler layers.

A scope is stored as a single text column (`"user:usr_hiten"`); events carry it
split into `level` + `user_id` fields. This module is the one place that
conversion lives, so no caller has to re-implement the split.

Moved here from `safety/pdp.py` in the 2026-11-10 lean refactor — the PDP that
used to host it was removed along with the autonomous agent layer.
"""

from __future__ import annotations

import os


class ScopeNotAllowed(ValueError):
    """The caller asked for a scope outside CURLYOS_ALLOWED_SCOPES."""


def allowed_scopes() -> set[str] | None:
    """Scopes writes may target, or None when enforcement is off.

    Default (unset or ``*``) is permissive so a single-user local install and
    the import scripts keep working. Set ``CURLYOS_ALLOWED_SCOPES`` to a
    comma-separated list to enforce one or more write scopes.
    """
    raw = os.environ.get("CURLYOS_ALLOWED_SCOPES", "").strip()
    if not raw or raw == "*":
        return None
    return {part.strip() for part in raw.split(",") if part.strip()}


def scope_allowed(scope: str, allowed: set[str] | None = None) -> bool:
    if allowed is None:
        allowed = allowed_scopes()
    return allowed is None or scope in allowed


def require_scope(scope: str, allowed: set[str] | None = None) -> str:
    if not scope_allowed(scope, allowed):
        raise ScopeNotAllowed(scope)
    return scope


def scope_parts(scope_text: str) -> dict[str, str]:
    """'user:usr_hiten' → {'level': 'user', 'user_id': 'usr_hiten'} (event scope)."""
    level, sep, rest = scope_text.partition(":")
    if not sep:
        return {"level": "user", "user_id": scope_text}
    return {"level": level, "user_id": rest}
