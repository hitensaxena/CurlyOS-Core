#!/usr/bin/env python3
"""Find credentials that were stored as memories. Reports only.

Memories are recalled into prompts: the UserPromptSubmit hook injects up to
14 KB of recall on every turn. A password or API key stored as a memory
therefore reaches model context and the transcript archive, which is a wider
audience than the person who wrote it.

Two different things look alike here and must not be treated the same:

  custody     "The user stores OmniRoute credentials in ~/.config/..." — the
              location, no secret. Useful and harmless.
  secret      "Delta Exchange API credentials (key/token: 4guXKm…) — a value.

This reports both, with values masked, so a decision can be made about the
second without printing it again into a transcript.

Usage:
    .venv/bin/python deploy/scrub_secrets.py                # report
    .venv/bin/python deploy/scrub_secrets.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

# Values, not words. Each pattern must match the secret itself, so a sentence
# that merely says "credentials" stays out of the report.
_VALUE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("figma-token", re.compile(r"\bfigd_[A-Za-z0-9_-]{20,}")),
    ("google-key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}")),
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
]

# A labelled value: "password: hunter2", "api_key = ab12…", "key/token: 4guX…".
_LABELLED = re.compile(
    r"(?i)\b(?:password|passwd|passphrase|secret|api[_ -]?key|access[_ -]?key|"
    r"api[_ -]?token|access[_ -]?token|auth[_ -]?token|key/token|token)"
    r"\s*(?:is|are|was)?\s*[:=]\s*[`'\"]?([A-Za-z0-9._/+=:-]{6,})"
)

# Custody statements name a place, not a value.
_CUSTODY = re.compile(
    r"(?i)(?:stored|stored in|kept|lives? in|located|in)\s+[`'\"]?(?:~|/|\.config|\.env)"
)


def _mask(value: str) -> str:
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:3]}…{len(value)} chars"


def redact(statement: str, spans: list[list[int]]) -> str:
    """Replace every matched span with [REDACTED], longest-first.

    Masking the extracted value is not enough: the statement around it still
    contains the secret, and this report is read in a transcript that CurlyOS
    itself archives.
    """
    out = statement
    for start, end in sorted((tuple(s) for s in spans), reverse=True):
        out = out[:start] + "[REDACTED]" + out[end:]
    return out


def _context(statement: str, match: re.Match, width: int = 45) -> str:
    """The text around a match, with the matched value removed.

    Showing the statement's first 200 characters is useless for review — the
    label ("api_key =") can be anywhere — and showing the raw text around the
    value re-publishes the secret.
    """
    start, end = match.span(0) if match.lastindex is None else match.span(1)
    lo, hi = max(0, start - width), min(len(statement), end + width)
    return statement[lo:start] + "[REDACTED]" + statement[end:hi]


def scan_statement(statement: str) -> list[dict]:
    """Return every credential-looking value found, with the value masked."""
    found: list[dict] = []
    for kind, pattern in _VALUE_PATTERNS:
        for match in pattern.finditer(statement):
            found.append({"kind": kind, "masked": _mask(match.group(0)),
                          "span": list(match.span()),
                          "context": _context(statement, match)})
    for match in _LABELLED.finditer(statement):
        value = match.group(1)
        # "password:" followed by a path or a word is custody, not a secret.
        if _CUSTODY.search(statement):
            continue
        if value.lower() in {"true", "false", "null", "none", "required", "unset"}:
            continue
        if value.startswith(("~/", "/", "./", "http")):
            continue
        # Templates read like secrets and are not: your_instagram_access_token,
        # <YOUR_KEY>, xxx, example. Flagging them buries the real ones.
        low = value.lower()
        if any(marker in low for marker in
               ("your_", "your-", "xxx", "example", "placeholder", "redacted")):
            continue
        found.append({"kind": "labelled", "masked": _mask(value),
                      "span": list(match.span(1)),
                      "context": _context(statement, match)})
    return found


async def main() -> int:
    import psycopg.rows
    from psycopg_pool import AsyncConnectionPool

    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", default=None)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()

    scope = args.scope or os.environ.get("CURLYOS_SCOPE") or "user:usr_hiten"
    dsn = os.environ.get("CURLYOS_DATABASE_URL")
    if not dsn:
        raise SystemExit("CURLYOS_DATABASE_URL is not set")

    pool = AsyncConnectionPool(dsn, min_size=1, max_size=2, open=False,
                               kwargs={"row_factory": psycopg.rows.tuple_row})
    await pool.open()
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, statement, source_episode_id, created_at::date "
                    "FROM memories WHERE scope = %s AND valid_to IS NULL",
                    (scope,),
                )
                rows = await cur.fetchall()
    finally:
        await pool.close()

    # A presigned URL carries an access-key id and an expiring signature; the
    # URL from months ago grants nothing now. Reported separately so it does not
    # inflate the count of live secrets.
    presigned = re.compile(r"X-Amz-Credential=", re.IGNORECASE)

    hits = []
    for mid, statement, epi, created in rows:
        found = scan_statement(statement or "")
        if found and presigned.search(statement or ""):
            found = [f for f in found if f["kind"] != "aws-key"]
            if not found:
                continue
        if found:
            safe = redact(statement or "", [f["span"] for f in found])
            hits.append({
                "id": mid, "kinds": sorted({f["kind"] for f in found}),
                "count": len(found), "created": str(created),
                "redacted": safe[:200],
                "contexts": [f["context"].replace("\n", " ")[:120] for f in found[:2]],
            })

    kinds = Counter(k for h in hits for k in h["kinds"])
    print(f"scope={scope} memories={len(rows)} rows_with_credential_values={len(hits)}")
    for kind, n in kinds.most_common():
        print(f"  {kind:<14} {n}")

    print("\nsamples (values replaced with [REDACTED]):")
    for hit in hits[:14]:
        print(f"  {hit['id']}  {'/'.join(hit['kinds'])}")
        for ctx in hit["contexts"]:
            print(f"      …{ctx}…")

    if args.json:
        args.json.write_text(json.dumps(hits, indent=1))
        print(f"\nfull report: {args.json}")
    print("\nnothing was modified")
    return 0


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(main()))
