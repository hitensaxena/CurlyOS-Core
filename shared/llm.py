"""Robust JSON extraction from LLM responses.

Models — especially cheaper / free ones like nvidia/nemotron-*:free — wrap JSON
in ``` fences, prepend prose, or truncate long outputs mid-string. Routing every
LLM-JSON parse through these helpers means one ugly response degrades gracefully
(salvage what parsed) instead of dropping an entire batch with a raise.
"""
from __future__ import annotations

import json
import re
from typing import Any

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)
_FLAT_OBJ = re.compile(r"\{[^{}]*\}")


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = _FENCE.sub("", s).strip()
    return s


def _first_balanced(s: str) -> str | None:
    """First balanced {...} or [...] substring, string/escape aware."""
    start = open_ch = close_ch = None
    depth = 0
    in_str = esc = False
    for i, ch in enumerate(s):
        if start is None:
            if ch in "{[":
                start, open_ch, close_ch, depth = i, ch, ("}" if ch == "{" else "]"), 1
            continue
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return None


def first_json(content: str | None, default: Any = None) -> Any:
    """Parse the first complete JSON value from an LLM response (fence/prose
    tolerant). Returns `default` if nothing parses."""
    if not content:
        return default
    cleaned = _strip_fences(content)
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    frag = _first_balanced(cleaned)
    if frag is not None:
        try:
            return json.loads(frag)
        except Exception:
            pass
    return default


def json_records(content: str | None) -> list[dict]:
    """Best-effort list of flat JSON record-objects from an LLM response.

    Tolerates fences, prose, AND truncation: a fully-parsed wrapper
    ({"results"/"triples"/...: [...]}) or bare [...] is used directly; otherwise
    complete {...} records are regex-salvaged and a truncated trailing record is
    dropped.
    """
    parsed = first_json(content, default=None)
    if isinstance(parsed, list):
        return [r for r in parsed if isinstance(r, dict)]
    if isinstance(parsed, dict):
        for v in parsed.values():
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
        return [parsed] if parsed else []
    out: list[dict] = []
    for m in _FLAT_OBJ.finditer(_strip_fences(content or "")):
        try:
            out.append(json.loads(m.group(0)))
        except Exception:
            continue
    return out
