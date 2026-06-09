"""Shared introspection contract — emit_finding() and epistemic-humility rule.

EVERY finding about the user is written at epistemic_status = hypothesis.
Graduation to canonical REQUIRES explicit user confirmation.
This is the standing guard against identity-overfitting.

See: ~/hitenos-architecture/33-introspection-overview.md
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from shared.types.ulid import mint
from shared.events import build_event


async def emit_finding(
    pool: Any,
    publisher: Any,
    scope_text: str,
    kind: str,          # "assumption" | "mental_model" | "decision_audit" | "principle" | "alignment_signal" | "theme" | "chapter"
    statement: str,
    provenance: dict,   # {source_episode_ids: [...], analysis_type: str, ...}
    tags: list[str] | None = None,
) -> dict:
    """Emit an introspective finding.

    HARD CONTRACT: always writes at epistemic_status = hypothesis.
    Never writes at canonical — that requires user confirmation via
    POST /findings/{id}/confirm.
    """
    prefix_map = {
        "assumption": "asu", "mental_model": "mdl", "decision_audit": "dau",
        "principle": "prn", "alignment_signal": "aln", "theme": "thm",
        "chapter": "cha", "trend": "trd",
    }
    prefix = prefix_map.get(kind, "asu")
    finding_id = mint(prefix)

    # Store as a memory row with epistemic_status = hypothesis
    from memory.governance import add, record_episode

    # Record the analysis as an episode first (provenance anchor)
    epi = await record_episode(
        pool, publisher, scope_text,
        content=f"[{kind}] {statement}",
        source_ref=provenance.get("analysis_type", "introspection"),
    )

    # Add as a fact at hypothesis status
    ref = await add(
        pool, publisher, scope_text,
        statement=statement,
        source_episode_id=epi["epi_id"],
        epistemic_status="hypothesis",
    )

    return {
        "finding_id": finding_id,
        "kind": kind,
        "epistemic_status": "hypothesis",
        "mem_id": ref["mem_id"],
        "epi_id": epi["epi_id"],
    }


async def confirm_finding(
    pool: Any,
    publisher: Any,
    mem_id: str,
) -> dict:
    """Graduate a finding from hypothesis → canonical (user confirmation).

    This is the ONLY sanctioned path from hypothesis to canonical
    for introspective findings.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE memories SET epistemic_status = 'canonical' "
                "WHERE id = %s AND epistemic_status = 'hypothesis' "
                "RETURNING statement",
                (mem_id,),
            )
            row = await cur.fetchone()
    if row is None:
        return {"error": "no hypothesis finding with that id"}
    return {"mem_id": mem_id, "epistemic_status": "canonical", "statement": row[0]}


async def reject_finding(
    pool: Any,
    publisher: Any,
    scope_text: str,
    mem_id: str,
) -> dict:
    """Invalidate a hypothesis finding (user rejects it)."""
    from memory.governance import invalidate
    result = await invalidate(
        pool, publisher, scope_text, mem_id,
        reason="User rejected introspective finding",
    )
    return result
