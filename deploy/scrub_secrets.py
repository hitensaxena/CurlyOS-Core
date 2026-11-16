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

# The hardest class: an opaque value with no label at all, sitting near a
# credential word. "Delta Exchange API keys (r1ay… / -jGSo…)" and "API key and
# secret … account access (2N8r…)" both defeated the labelled pattern and were
# only found because recall returned them after the first scrub pass.
_PROXIMITY_VALUE = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")
_PROXIMITY_WORD = re.compile(
    r"(?i)(?:api|access|auth|secret|private)?[ _-]?(?:key|keys|secret|token|credential|credentials)")

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
    for match in _PROXIMITY_VALUE.finditer(statement):
        value = match.group(0)
        # Require both a letter and a digit: that skips words, paths, hashes of
        # pure digits, and ULIDs (which are still reported by other patterns
        # only when they carry a credential word, which they never do here).
        if not (any(c.isdigit() for c in value) and any(c.isalpha() for c in value)):
            continue
        # A filename or a version is not a secret: "intrl_…csv", "v1.2.3".
        after = statement[match.end():match.end() + 1]
        if ("." in value or after == "."
                or statement[max(0, match.start() - 1):match.start()] in ("/", "\\")):
            continue
        # Presigned URLs carry an expiring signature, handled separately.
        if "X-Amz-Credential=" in statement[max(0, match.start() - 40):match.start()]:
            continue
        window = statement[max(0, match.start() - 80):match.start()]
        if not _PROXIMITY_WORD.search(window):
            continue
        # Transcript metadata and tutorial placeholders: an id near the word
        # "token" is not a token, and "Replace with your …" is an instruction.
        nearby = statement[max(0, match.start() - 120):match.end() + 120]
        if "conversation_id" in nearby or "Replace with your" in nearby:
            continue
        if "pen." in statement[max(0, match.start() - 12):match.start()]:
            continue
        found.append({"kind": "proximity", "masked": _mask(value),
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


def pool2(dsn: str):
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(dsn, min_size=1, max_size=4, open=False)
    return _PoolContext(pool)


class _PoolContext:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        await self.pool.open()
        return self.pool

    async def __aexit__(self, *exc):
        await self.pool.close()


async def main() -> int:
    import psycopg.rows
    from psycopg_pool import AsyncConnectionPool

    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", default=None)
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--apply", action="store_true",
                        help="invalidate the credential-bearing memories and write "
                             "redacted replacements. Reversible: the originals stay "
                             "in the table, only closed.")
    parser.add_argument("--only-ids", type=Path, default=None,
                        help="restrict --apply to ids listed in this file (one per line)")
    parser.add_argument("--apply-episodes", action="store_true",
                        help="ALSO rewrite episode content in place. Irreversible — "
                             "episodes have no valid_to, so the original text is "
                             "gone. Take your own copy first if you want one.")
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

    if not args.apply:
        print("\nnothing was modified (pass --apply to redact)")
        return 0

    from memory.governance import add, invalidate
    from shared.events.implementations import PgOnlyPublisher

    if args.only_ids:
        allow = {line.strip() for line in args.only_ids.read_text().splitlines() if line.strip()}
        hits = [h for h in hits if h["id"] in allow]
        print(f"\nrestricted to {len(hits)} ids from {args.only_ids}")

    publisher = PgOnlyPublisher()
    redacted_memories = replaced_memories = redacted_episodes = 0

    async with pool2(dsn) as pool:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                for hit in hits:
                    await cur.execute(
                        "SELECT statement, kind, tier, epistemic_status, source_episode_id, "
                        "       valid_to FROM memories WHERE id = %s", (hit["id"],))
                    row = await cur.fetchone()
                    if row is None or row[5] is not None:
                        continue
                    statement, kind, tier, status, epi, _ = row
                    found = scan_statement(statement)
                    if not found:
                        continue
                    safe = redact(statement, [f["span"] for f in found])
                    if safe == statement:
                        continue
                    created = await add(
                        pool, publisher, scope, statement=safe,
                        source_episode_id=epi, kind=kind, tier=tier,
                        epistemic_status=status,
                    )
                    await invalidate(
                        pool, publisher, scope, hit["id"],
                        superseded_by=created.get("mem_id"),
                        reason="credentials redacted",
                    )
                    redacted_memories += 1
                    replaced_memories += 1

        if not args.apply_episodes:
            print("\nepisodes untouched (pass --apply-episodes to rewrite them in place; "
                  "irreversible)")
            print(f"\nredacted memories: {redacted_memories} (invalidated {replaced_memories} originals)")
            return 0

        # Episodes have no valid_to: they are raw capture, not derived facts.
        # Redacting the span in place is the only option short of deleting the
        # transcript, and the embedding is dropped so the sweep re-embeds from
        # the redacted text.
        # Episodes are stricter than memories because the rewrite cannot be
        # undone: only formats that are unambiguous on their own, plus a
        # proximity hit that names a real service. Bare proximity, JWTs and
        # vendored package docs are skipped — in a 10k-transcript corpus they
        # fire on gallery ids, plist logs and example configs.
        precise = {"openai-key", "github-token", "figma-token", "google-key",
                   "aws-key", "slack-token", "labelled"}
        services = re.compile(r"(?i)delta|serpapi|podcast ?index|omniroute|noesis|figma")
        skip_sources = ("mind:systems/",)

        async with pool.connection() as conn:
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT id, content, source_ref FROM episodes WHERE scope = %s",
                        (scope,))
                    for epi_id, content, source_ref in await cur.fetchall():
                        if (source_ref or "").startswith(skip_sources):
                            continue
                        found = scan_statement(content or "")
                        if not found:
                            continue
                        found = [
                            f for f in found
                            if f["kind"] in precise
                            or (f["kind"] == "proximity" and services.search(f["context"]))
                        ]
                        if not found:
                            continue
                        if "X-Amz-Credential=" in (content or ""):
                            found = [f for f in found if f["kind"] != "aws-key"]
                        if not found:
                            continue
                        safe = redact(content or "", [f["span"] for f in found])
                        if safe == content:
                            continue
                        await cur.execute(
                            "UPDATE episodes SET content = %s, embedding = NULL "
                            "WHERE id = %s", (safe, epi_id))
                        redacted_episodes += 1

    print(f"\nredacted memories: {redacted_memories} (invalidated {replaced_memories} originals)")
    print(f"redacted episodes: {redacted_episodes} (embedding cleared for re-embed)")
    return 0


if __name__ == "__main__":
    import asyncio

    raise SystemExit(asyncio.run(main()))
