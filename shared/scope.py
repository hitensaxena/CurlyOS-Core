"""Scope-string helpers shared by the memory, goal and scheduler layers.

A scope is stored as a single text column (`"user:usr_hiten"`); events carry it
split into `level` + `user_id` fields. This module is the one place that
conversion lives, so no caller has to re-implement the split.

Moved here from `safety/pdp.py` in the 2026-11-10 lean refactor — the PDP that
used to host it was removed along with the autonomous agent layer.
"""

from __future__ import annotations


def scope_parts(scope_text: str) -> dict[str, str]:
    """'user:usr_hiten' → {'level': 'user', 'user_id': 'usr_hiten'} (event scope)."""
    level, sep, rest = scope_text.partition(":")
    if not sep:
        return {"level": "user", "user_id": scope_text}
    return {"level": level, "user_id": rest}
