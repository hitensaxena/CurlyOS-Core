"""In-process, since-boot metrics counters. Thread-safe, zero-dependency.

Cumulative counter semantics (like Prometheus counters): values only grow and
reset when the process restarts — always read them alongside `uptime_seconds`.
This deliberately lives in process memory (no Redis/DB): it's a single-process
box, the overhead must be ~nothing, and metrics are never on a correctness path.

Key convention (dotted): `llm.<tier>.calls`, `recall.cache_hits`, etc.
"""
from __future__ import annotations

import threading
import time
from typing import Any

_LOCK = threading.Lock()
_COUNTERS: dict[str, float] = {}
_TIMINGS: dict[str, list[float]] = {}   # key -> [sum_ms, count]
_NOTES: dict[str, Any] = {}             # key -> last value (e.g. last model/error)
_START = time.time()


def incr(key: str, n: float = 1.0) -> None:
    with _LOCK:
        _COUNTERS[key] = _COUNTERS.get(key, 0.0) + n


def timing(key: str, ms: float) -> None:
    """Record one latency observation (kept as running sum + count → avg)."""
    with _LOCK:
        s = _TIMINGS.get(key)
        if s is None:
            _TIMINGS[key] = [ms, 1.0]
        else:
            s[0] += ms
            s[1] += 1.0


def note(key: str, value: Any) -> None:
    """Store a last-seen scalar (model name, error string, …)."""
    with _LOCK:
        _NOTES[key] = value


def counter(key: str) -> float:
    with _LOCK:
        return _COUNTERS.get(key, 0.0)


def uptime_seconds() -> float:
    return time.time() - _START


def snapshot() -> dict:
    with _LOCK:
        counters = dict(_COUNTERS)
        timings = {
            k: {"avg_ms": round(v[0] / v[1], 1) if v[1] else 0.0, "count": int(v[1])}
            for k, v in _TIMINGS.items()
        }
        notes = dict(_NOTES)
    return {
        "counters": counters,
        "timings": timings,
        "notes": notes,
        "uptime_seconds": round(uptime_seconds(), 1),
    }


def reset() -> None:
    """Zero all counters (e.g. after reading a window). Does not reset uptime."""
    with _LOCK:
        _COUNTERS.clear()
        _TIMINGS.clear()
        _NOTES.clear()
