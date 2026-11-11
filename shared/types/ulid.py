"""Typed-prefix ULID registry + minting + validation.

All ids are `<prefix>_<26-char ULID>` stored as `text`.
Crockford Base32 alphabet: 0123456789ABCDEFGHJKMNPQRSTVWXYZ
(Excludes I, L, O, U to avoid ambiguity.)

Ported from ~/curlyos/core/curlyos/models/ids.py + events/ulid.py.
"""
from __future__ import annotations

import re
import time
import os

# Crockford Base32
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE_MAP = {c: i for i, c in enumerate(_ALPHABET)}
# Also accept lowercase and ambiguous chars (I→1, L→1, O→0, U→V)
_DECODE_MAP.update({"i": 1, "I": 1, "l": 1, "L": 1, "o": 0, "O": 0, "u": 21, "U": 21})

# Entity → prefix. Every entry here is minted somewhere in this repository,
# except `usr`, whose ids are written as literals in scope strings
# (`user:usr_hiten`) rather than minted. Anything else was pruned in the
# 2026-11-11 cleanup along with the packages that produced those ids; rows
# carrying the old prefixes still exist in the database and remain readable.
PREFIXES: dict[str, str] = {
    # Memory spine
    "event": "evt",
    "episode": "epi",
    "memory": "mem",
    "identity_fact": "idf",
    "entity": "ent",
    # Runs and approvals
    "agent_run": "run",
    "approval": "apv",
    # Scope owners (literal, never minted)
    "user": "usr",
    # Goals and decisions
    "goal": "goal",
    "decision": "dec",
    "outcome": "out",
    "lesson": "les",
    "opportunity": "opp",
    # Introspection
    "assumption": "asu",
    "mental_model": "mdl",
    "decision_audit": "dau",
    "principle": "prn",
    "chapter": "cha",
    "theme": "thm",
    "alignment_signal": "aln",
    "insight_report": "rpt",
    "mood_log": "moo",
    # Infrastructure
    "lock": "lck",
    "correlation": "cor",
}

ALL_PREFIXES: frozenset[str] = frozenset(PREFIXES.values())

_ULID_BODY = r"[0-9A-HJKMNP-TV-Z]{26}"


def id_pattern(prefix: str) -> str:
    """Anchored regex string a `<prefix>_<ULID>` id must match."""
    return rf"^{re.escape(prefix)}_{_ULID_BODY}$"


def is_valid(prefix: str, value: str) -> bool:
    """True if `value` is a well-formed id for `prefix`."""
    return bool(re.match(id_pattern(prefix), value))


def prefix_of(value: str) -> str | None:
    """Return the prefix segment of an id, or None if no `_` separator."""
    return value.split("_", 1)[0] if "_" in value else None


# ── ULID minting ────────────────────────────────────────────────────────────

_last_timestamp_ms: int = 0
_randomness: int = 0


def _encode_time(ts_ms: int) -> str:
    """Encode 48-bit timestamp to 10 Crockford Base32 chars (most significant first)."""
    chars = []
    for _ in range(10):
        chars.append(_ALPHABET[ts_ms & 0x1F])
        ts_ms >>= 5
    return "".join(reversed(chars))


def _encode_random(rand: int) -> str:
    """Encode 80-bit randomness to 16 Crockford Base32 chars."""
    chars = []
    for _ in range(16):
        chars.append(_ALPHABET[rand & 0x1F])
        rand >>= 5
    return "".join(reversed(chars))


def mint_ulid() -> str:
    """Generate a monotonically-increasing 26-char ULID (Crockford Base32)."""
    global _last_timestamp_ms, _randomness

    ts_ms = int(time.time() * 1000)

    if ts_ms == _last_timestamp_ms:
        _randomness += 1
        if _randomness >= (1 << 80):
            # Overflow (extremely unlikely) — wait 1ms
            time.sleep(0.001)
            ts_ms = int(time.time() * 1000)
            _randomness = int.from_bytes(os.urandom(10), "big")
    else:
        _randomness = int.from_bytes(os.urandom(10), "big")

    _last_timestamp_ms = ts_ms
    return _encode_time(ts_ms) + _encode_random(_randomness)


def mint(prefix: str) -> str:
    """Mint a typed-prefix ULID: e.g. mint('epi') → 'epi_01JX8ZTQABC...'."""
    if prefix not in ALL_PREFIXES:
        raise ValueError(f"Unknown prefix {prefix!r}. Register it in PREFIXES first.")
    return f"{prefix}_{mint_ulid()}"
