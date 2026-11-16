"""Ingest application service.

Owns the write orchestration that used to live in the `/api/ingest` route:
strip harness scaffolding, record the episode, chunk the text, and append one
recallable memory per chunk. The route keeps only HTTP concerns.

Background work (embedding, epistemic classification, graph extraction) stays
with the caller for now: it depends on the API process's embedder and LLM
factories. The service returns the chunk IDs so the caller can schedule it.
"""

from __future__ import annotations

import re
from typing import Any

from memory.chunking import split_text

# Harness/tooling scaffolding that leaks into captured "user" turns: task
# notifications delivered as user messages, injected system reminders, and
# slash-command wrappers. None of it is personal content.
_SCAFFOLD_TAGS = (
    "system-reminder", "task-notification", "command-name", "command-message",
    "command-args", "command-output", "local-command-stdout",
    "local-command-stderr", "user-prompt-submit-hook",
)
_SCAFFOLD_BLOCK_RE = re.compile(
    r"<(" + "|".join(_SCAFFOLD_TAGS) + r")\b[^>]*>.*?</\1\s*>",
    re.DOTALL | re.IGNORECASE,
)
_SCAFFOLD_TAG_RE = re.compile(
    r"</?(" + "|".join(_SCAFFOLD_TAGS) + r")\b[^>]*>", re.IGNORECASE
)


def strip_scaffolding(text: str) -> str:
    """Remove harness/tooling blocks, returning only the human content."""
    if not text:
        return ""
    cleaned = _SCAFFOLD_BLOCK_RE.sub("", text)
    cleaned = _SCAFFOLD_TAG_RE.sub("", cleaned)  # drop any unpaired leftovers
    return cleaned.strip()


async def _mark_duplicate_episode(pool: Any, scope: str, epi_id: str) -> None:
    """Record a closed, non-recallable marker for a fully deduplicated episode."""
    from shared.types.ulid import mint

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO memories "
                "(id, scope, statement, statement_key, kind, tier, epistemic_status, "
                " content_hash, valid_from, valid_to, ingested_at, source_episode_id) "
                "VALUES (%s, %s, '(duplicate capture)', %s, 'fact', 'working', "
                "        'hypothesis', NULL, now(), now(), now(), %s) "
                "ON CONFLICT DO NOTHING",
                (mint("mem"), scope, f"_duplicate:{epi_id}", epi_id),
            )


async def ingest_text(
    pool: Any,
    publisher: Any,
    scope: str,
    text: str,
    *,
    source_ref: str,
    add_memory: bool = True,
    kind: str = "fact",
    epistemic_status: str = "canonical",
) -> dict:
    """Record an episode and (optionally) its chunked memories.

    Returns a JSON-safe dict plus ``mem_chunks``: a list of
    ``(mem_id, chunk_text)`` for the caller's background processing. Pop
    ``mem_chunks`` before returning the result over HTTP.
    """
    from memory.governance import add, record_episode

    epi = await record_episode(pool, publisher, scope, content=text, source_ref=source_ref)
    epi_id = epi["epi_id"]

    mem_chunks: list[tuple[str, str]] = []
    new_memories = 0
    if add_memory:
        for chunk in split_text(text):
            mem = await add(
                pool,
                publisher,
                scope,
                statement=chunk,
                source_episode_id=epi_id,
                kind=kind,
                epistemic_status=epistemic_status,
            )
            mem_chunks.append((mem["mem_id"], chunk))
            if not mem.get("deduplicated"):
                new_memories += 1

        if new_memories == 0:
            # Every chunk collided with an active memory. Close this duplicate
            # episode with an invisible marker, otherwise SUMMARIZE selects it
            # as undistilled on every run and spends an LLM call forever.
            await _mark_duplicate_episode(pool, scope, epi_id)

    return {
        "epi_id": epi_id,
        "mem_id": mem_chunks[0][0] if mem_chunks else None,
        "mem_ids": [mid for mid, _ in mem_chunks],
        "chunks": len(mem_chunks),
        "mem_chunks": mem_chunks,
    }
