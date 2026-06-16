"""Memory consolidation — event-driven projection worker.

The ONLY writer of derived stores (pgvector embeddings + Redis read-model).
Reads unprocessed events by seq watermark per scope, projects them, advances watermark.

Passes:
  DEDUP             — vector sim ≥ 0.92 + cross-encoder → merge duplicates
  MERGE/PROMOTE     — promote working→episodic→semantic
  CONFLICT-RESOLVE  — invalidate superseded facts on conflict
  SUMMARIZE         — LLM-extract distilled memories from episodes
  DECAY             — archive cold rows, invalidate expired speculative content
  RECOMBINE/INCUBATE — nightly creative pass (writes conjecture only)

Ported from ~/curlyos/core/curlyos/memory/consolidation.py.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Sequence

from shared.types.ulid import mint
from shared.events import build_event
from shared.llm import json_records

log = logging.getLogger("curlyos.consolidation")

_FULL_TYPE_PREFIX = "art.curlybrackets.curlyos."
_REDACTED = "[REDACTED]"
PROJECTIONS = ("pgvector", "redis")
CONSOL_LOCK_TTL_MS = 30_000

# Thresholds
_DEDUP_SIMILARITY_THRESHOLD = 0.92
_DEDUP_CONFIRM_THRESHOLD = 0.85
# When NO cross-encoder reranker is available, only auto-merge near-exact
# duplicates. Vector similarity alone in the 0.92–0.98 band routinely flags
# distinct-but-related memories; merging those is destructive data loss.
_DEDUP_AUTO_MERGE_SIM = 0.985
_DECAY_COLD_DAYS = 90
_DECAY_SPECULATIVE_STATUSES = ("seed", "conjecture", "possible_world")


def _vector_literal(vec: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _scope_obj_from_text(scope_text: str) -> dict[str, str]:
    level, _, ident = scope_text.partition(":")
    if level not in ("user", "session", "agent", "workspace"):
        level = "user"
    return {"level": level, "user_id": ident or scope_text}


# ── Redis lock ──────────────────────────────────────────────────────────────

async def _acquire(redis: Any, scope_text: str, ttl_ms: int = CONSOL_LOCK_TTL_MS) -> str | None:
    token = mint("lck")
    ok = await redis.set(f"lock:consol:{scope_text}", token, nx=True, px=ttl_ms)
    return token if ok else None


async def _release(redis: Any, scope_text: str, token: str) -> None:
    key = f"lock:consol:{scope_text}"
    try:
        current = await redis.get(key)
        if current is not None and current in (token, token.encode()):
            await redis.delete(key)
    except Exception:
        pass


async def _evict_retr_cache(redis: Any, scope_text: str) -> None:
    pattern = f"cache:retr:{scope_text}:*"
    try:
        async for key in redis.scan_iter(match=pattern):
            await redis.delete(key)
    except (AttributeError, TypeError):
        keys = await redis.keys(pattern)
        for key in keys or []:
            await redis.delete(key)


# ── Deferred Redis ops ──────────────────────────────────────────────────────

async def _apply_redis_ops(redis: Any, ops: list[tuple]) -> None:
    for op in ops:
        kind = op[0]
        if kind == "flush":
            await redis.delete(f"mem:current:{op[1]}")
            await _evict_retr_cache(redis, op[1])
        elif kind == "hset":
            await redis.hset(op[1], op[2], op[3])
            await _evict_retr_cache(redis, op[4])
        elif kind == "hdel":
            await redis.hdel(op[1], op[2])
            await _evict_retr_cache(redis, op[3])


# ── Watermark ──────────────────────────────────────────────────────────────

async def _read_watermark(conn: Any, scope_text: str) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT projection, last_seq FROM projection_watermarks "
            "WHERE scope = %s AND projection = ANY(%s)",
            (scope_text, list(PROJECTIONS)),
        )
        rows = await cur.fetchall()
    seen = {r[0]: int(r[1]) for r in (rows or [])}
    return min((seen.get(p, 0) for p in PROJECTIONS), default=0)


async def _advance_watermark(conn: Any, scope_text: str, seq: int) -> None:
    async with conn.cursor() as cur:
        for proj in PROJECTIONS:
            await cur.execute(
                "INSERT INTO projection_watermarks (projection, scope, last_seq, updated_at) "
                "VALUES (%s, %s, %s, now()) "
                "ON CONFLICT (projection, scope) "
                "DO UPDATE SET last_seq = EXCLUDED.last_seq, updated_at = now()",
                (proj, scope_text, seq),
            )


# ── Projectors ─────────────────────────────────────────────────────────────

async def project_fact_stored(
    conn: Any, ops: list[tuple], embedder: Any, mem_id: str, scope_text: str
) -> str:
    async with conn.cursor() as cur:
        await cur.execute("SELECT statement, valid_to FROM memories WHERE id = %s", (mem_id,))
        row = await cur.fetchone()
    if row is None:
        return "missing"
    statement, valid_to = row
    if statement == _REDACTED:
        async with conn.cursor() as cur:
            await cur.execute("UPDATE memories SET embedding = NULL WHERE id = %s", (mem_id,))
        ops.append(("hdel", f"mem:current:{scope_text}", mem_id, scope_text))
        return "tombstoned"
    vec = (await embedder.embed([statement]))[0]
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE memories SET embedding = %s::vector WHERE id = %s",
            (_vector_literal(vec), mem_id),
        )
    if valid_to is None:
        ops.append(("hset", f"mem:current:{scope_text}", mem_id, statement, scope_text))
    return "embedded"


async def project_episode_recorded(conn: Any, embedder: Any, epi_id: str) -> str:
    async with conn.cursor() as cur:
        await cur.execute("SELECT content FROM episodes WHERE id = %s", (epi_id,))
        row = await cur.fetchone()
    if row is None:
        return "missing"
    vec = (await embedder.embed([row[0]]))[0]
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE episodes SET embedding = %s::vector WHERE id = %s",
            (_vector_literal(vec), epi_id),
        )
    return "episode"


async def project_fact_invalidated(
    conn: Any, ops: list[tuple], mem_id: str, scope_text: str
) -> str:
    async with conn.cursor() as cur:
        await cur.execute("SELECT statement, valid_to FROM memories WHERE id = %s", (mem_id,))
        row = await cur.fetchone()
    if row is not None and row[0] == _REDACTED:
        async with conn.cursor() as cur:
            await cur.execute("UPDATE memories SET embedding = NULL WHERE id = %s", (mem_id,))
    ops.append(("hdel", f"mem:current:{scope_text}", mem_id, scope_text))
    return "invalidated"


async def project_event(
    conn: Any, ops: list[tuple], embedder: Any,
    ev_type: str, subject: str | None, data: dict | None, scope_text: str
) -> str:
    short = ev_type[len(_FULL_TYPE_PREFIX):] if ev_type.startswith(_FULL_TYPE_PREFIX) else ev_type
    data = data or {}
    if short == "memory.fact.stored":
        return await project_fact_stored(conn, ops, embedder, data.get("mem_id") or subject, scope_text)
    if short == "memory.episode.recorded":
        return await project_episode_recorded(conn, embedder, data.get("epi_id") or subject)
    if short == "memory.fact.invalidated":
        return await project_fact_invalidated(conn, ops, data.get("mem_id") or subject, scope_text)
    return "noop"


# ── Consolidated marker (NATS-only) ────────────────────────────────────────

async def _emit_consolidated(publisher: Any, mem_id: str, scope_text: str) -> None:
    from shared.events import build_event, full_type

    ev = build_event(
        short_type="memory.fact.consolidated",
        subject=mem_id,
        scope=_scope_obj_from_text(scope_text),
        data={"mem_id": mem_id, "scope": scope_text},
        actor="agent:consolidation",
        source="curlyos-core/consolidation",
    )
    stamped = publisher.stamp(ev)
    try:
        _stream, subject = full_type(stamped["type"]).split(".", 2)[:1] + [stamped["type"]]
        await publisher.emit(subject, stamped)
    except Exception:
        log.warning("memory.fact.consolidated emit failed for %s", mem_id)


# ── Replay flush ────────────────────────────────────────────────────────────

async def _flush_scope_pg(conn: Any, scope_text: str) -> None:
    async with conn.cursor() as cur:
        await cur.execute("UPDATE memories SET embedding = NULL WHERE scope = %s", (scope_text,))
        await cur.execute("UPDATE episodes SET embedding = NULL WHERE scope = %s", (scope_text,))
        for proj in PROJECTIONS:
            await cur.execute(
                "INSERT INTO projection_watermarks (projection, scope, last_seq, updated_at) "
                "VALUES (%s, %s, 0, now()) "
                "ON CONFLICT (projection, scope) DO UPDATE SET last_seq = 0, updated_at = now()",
                (proj, scope_text),
            )


# ════════════════════════════════════════════════════════════════════════════
# CONSOLIDATION PASSES
# ════════════════════════════════════════════════════════════════════════════


async def _pass_dedup(
    pool: Any,
    redis: Any,
    embedder: Any,
    reranker: Any,
    publisher: Any,
    scope_text: str,
) -> dict:
    """DEDUP pass: find memories with vector similarity ≥ 0.92.

    Use cross-encoder to confirm. Merge: keep newer, invalidate older.
    """
    result = {"pass": "dedup", "candidates": 0, "merged": 0, "errors": 0}
    try:
        async with pool.connection() as conn:
            # Get all active memories with embeddings for this scope
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, statement, embedding, created_at FROM memories "
                    "WHERE scope = %s AND valid_to IS NULL AND embedding IS NOT NULL "
                    "ORDER BY created_at DESC LIMIT 2000",
                    (scope_text,),
                )
                rows = await cur.fetchall()

            if len(rows) < 2:
                return result

            # Find pairs with high cosine similarity using pgvector. Both sides
            # of the self-join are bounded to the same most-recent-2000 set so
            # the pass stays O(bounded²) as memories grow.
            async with conn.cursor() as cur:
                await cur.execute(
                    "WITH recent AS ("
                    "  SELECT id, statement, embedding FROM memories "
                    "  WHERE scope = %s AND valid_to IS NULL AND embedding IS NOT NULL "
                    "  ORDER BY created_at DESC LIMIT 2000"
                    ") "
                    "SELECT m1.id, m2.id, m1.statement, m2.statement, "
                    "1 - (m1.embedding <=> m2.embedding) AS sim "
                    "FROM recent m1 "
                    "JOIN recent m2 ON m1.id < m2.id "
                    "WHERE 1 - (m1.embedding <=> m2.embedding) >= %s",
                    (scope_text, _DEDUP_SIMILARITY_THRESHOLD),
                )
                pairs = await cur.fetchall()

            result["candidates"] = len(pairs)

            for id1, id2, stmt1, stmt2, sim in pairs:
                try:
                    # Confirm with cross-encoder before destructively merging.
                    # SAFETY: vector similarity alone is NOT sufficient to invalidate
                    # a memory. Without a reranker we cannot confirm semantic identity,
                    # so we only auto-merge near-exact duplicates (sim >= _DEDUP_AUTO_MERGE_SIM).
                    # This prevents catastrophic over-merging of distinct-but-similar
                    # memories when no cross-encoder is wired (e.g. the API path).
                    if reranker is not None:
                        confirmed = True
                        rerank_result = await reranker.rerank(
                            stmt1, [stmt2], top_k=1
                        )
                        if rerank_result and rerank_result[0][1] < _DEDUP_CONFIRM_THRESHOLD:
                            confirmed = False
                    else:
                        confirmed = sim >= _DEDUP_AUTO_MERGE_SIM

                    if confirmed:
                        # Invalidate the older one (keep newer)
                        # Determine which is older
                        async with conn.cursor() as cur:
                            await cur.execute(
                                "SELECT id, created_at FROM memories "
                                "WHERE id IN (%s, %s) ORDER BY created_at",
                                (id1, id2),
                            )
                            ordered = await cur.fetchall()
                            older_id = ordered[0][0]
                            newer_id = ordered[1][0]

                            await cur.execute(
                                "UPDATE memories SET valid_to = now(), superseded_by = %s "
                                "WHERE id = %s AND valid_to IS NULL",
                                (newer_id, older_id),
                            )

                        # Emit invalidation event
                        ev = build_event(
                            short_type="memory.fact.invalidated",
                            subject=older_id,
                            scope=_scope_obj_from_text(scope_text),
                            data={
                                "mem_id": older_id,
                                "scope": scope_text,
                                "superseded_by": newer_id,
                                "reason": "dedup_merge",
                            },
                            actor="agent:consolidation",
                            source="curlyos-core/consolidation",
                        )
                        try:
                            _stored, subj, stamped = await publisher.stage(ev, conn)
                            await publisher.emit(subj, stamped)
                        except Exception:
                            pass

                        result["merged"] += 1
                        log.info("DEDUP: merged %s → %s (sim=%.3f)", older_id, newer_id, sim)
                except Exception as e:
                    result["errors"] += 1
                    log.warning("DEDUP pair (%s, %s) failed: %s", id1, id2, e)

    except Exception as e:
        log.error("DEDUP pass failed for scope %s: %s", scope_text, e)
        result["errors"] += 1

    return result


async def _pass_merge_promote(
    pool: Any,
    redis: Any,
    embedder: Any,
    publisher: Any,
    scope_text: str,
) -> dict:
    """MERGE/PROMOTE pass: find working-tier memories that haven't been promoted.

    Create episodes from working memory, then create semantic facts.
    """
    result = {"pass": "merge_promote", "promoted": 0, "errors": 0}
    try:
        async with pool.connection() as conn:
            # Find working-tier memories without promotion
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, statement, source_episode_id, created_at FROM memories "
                    "WHERE scope = %s AND tier = 'working' AND valid_to IS NULL "
                    "ORDER BY created_at "
                    "LIMIT 100",
                    (scope_text,),
                )
                working_mems = await cur.fetchall()

            for mem_id, statement, src_epi_id, created_at in working_mems:
                try:
                    # Create an episode from the working memory
                    epi_id = mint("epi")
                    async with conn.transaction():
                        async with conn.cursor() as cur:
                            await cur.execute(
                                "INSERT INTO episodes (id, scope, content, source_ref) "
                                "VALUES (%s, %s, %s, %s) "
                                "ON CONFLICT DO NOTHING",
                                (epi_id, scope_text, statement, f"working:{mem_id}"),
                            )

                        # Promote memory tier: working → semantic
                        async with conn.cursor() as cur:
                            await cur.execute(
                                "UPDATE memories SET tier = 'semantic' "
                                "WHERE id = %s AND tier = 'working'",
                                (mem_id,),
                            )

                    result["promoted"] += 1
                except Exception as e:
                    result["errors"] += 1
                    log.warning("MERGE/PROMOTE failed for %s: %s", mem_id, e)

    except Exception as e:
        log.error("MERGE/PROMOTE pass failed for scope %s: %s", scope_text, e)
        result["errors"] += 1

    return result


async def _pass_conflict_resolve(
    pool: Any,
    redis: Any,
    publisher: Any,
    scope_text: str,
) -> dict:
    """CONFLICT-RESOLVE pass: find facts with overlapping (scope, predicate).

    Where both have valid_to IS NULL, invalidate the lower-confidence one.
    Applies to both memories (by statement_key) and identity_facts (by predicate).
    """
    result = {"pass": "conflict_resolve", "conflicts": 0, "resolved": 0, "errors": 0}
    try:
        async with pool.connection() as conn:
            # Find conflicting memories: same statement_key, both active
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT m1.id, m2.id, m1.statement_key, m1.created_at, m2.created_at "
                    "FROM memories m1 "
                    "JOIN memories m2 ON m1.statement_key = m2.statement_key AND m1.id < m2.id "
                    "WHERE m1.scope = %s AND m2.scope = %s "
                    "AND m1.valid_to IS NULL AND m2.valid_to IS NULL",
                    (scope_text, scope_text),
                )
                mem_conflicts = await cur.fetchall()

            result["conflicts"] = len(mem_conflicts)

            for id1, id2, skey, created1, created2 in mem_conflicts:
                try:
                    # Invalidate the older one
                    older_id = id1 if created1 <= created2 else id2
                    newer_id = id2 if created1 <= created2 else id1

                    async with conn.cursor() as cur:
                        await cur.execute(
                            "UPDATE memories SET valid_to = now(), superseded_by = %s "
                            "WHERE id = %s AND valid_to IS NULL",
                            (newer_id, older_id),
                        )

                    ev = build_event(
                        short_type="memory.fact.invalidated",
                        subject=older_id,
                        scope=_scope_obj_from_text(scope_text),
                        data={
                            "mem_id": older_id,
                            "scope": scope_text,
                            "superseded_by": newer_id,
                            "reason": "conflict_resolve",
                        },
                        actor="agent:consolidation",
                        source="curlyos-core/consolidation",
                    )
                    try:
                        _stored, subj, stamped = await publisher.stage(ev, conn)
                        await publisher.emit(subj, stamped)
                    except Exception:
                        pass

                    result["resolved"] += 1
                except Exception as e:
                    result["errors"] += 1
                    log.warning("CONFLICT-RESOLVE pair (%s, %s) failed: %s", id1, id2, e)

            # Find conflicting identity_facts: same (scope, predicate), both active
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT i1.id, i2.id, i1.predicate, i1.confidence, i2.confidence "
                    "FROM identity_facts i1 "
                    "JOIN identity_facts i2 ON i1.predicate = i2.predicate AND i1.id < i2.id "
                    "WHERE i1.scope = %s AND i2.scope = %s "
                    "AND i1.valid_to IS NULL AND i2.valid_to IS NULL",
                    (scope_text, scope_text),
                )
                idf_conflicts = await cur.fetchall()

            result["conflicts"] += len(idf_conflicts)

            for id1, id2, predicate, conf1, conf2 in idf_conflicts:
                try:
                    # Invalidate lower-confidence one
                    if conf1 >= conf2:
                        lower_id, higher_id = id2, id1
                    else:
                        lower_id, higher_id = id1, id2

                    async with conn.cursor() as cur:
                        await cur.execute(
                            "UPDATE identity_facts SET valid_to = now(), superseded_by = %s "
                            "WHERE id = %s AND valid_to IS NULL",
                            (higher_id, lower_id),
                        )

                    result["resolved"] += 1
                except Exception as e:
                    result["errors"] += 1
                    log.warning("CONFLICT-RESOLVE idf (%s, %s) failed: %s", id1, id2, e)

    except Exception as e:
        log.error("CONFLICT-RESOLVE pass failed for scope %s: %s", scope_text, e)
        result["errors"] += 1

    return result


# Episodes-per-pass cap for distillation. Distillation runs against a cloud
# router (OmniRoute → free providers), so it costs no local RAM/CPU and can be
# larger than the local-model era. The NOT-EXISTS guard makes each episode
# distil at most once, so a backlog just drains over passes (≈12/15m).
_DISTILL_EPISODE_LIMIT = 12
_DISTILL_MAX_FACTS = 3

_DISTILL_SYSTEM = (
    "You extract durable long-term memories about the USER and their projects from "
    "a single conversation turn. You are extremely selective. Return JSON only."
)
_DISTILL_PROMPT = (
    "From the turn below (a user message and the assistant's reply), extract only "
    "facts that will still matter in months: the user's stable preferences, "
    "decisions they made, concrete project/system facts, identity, or relationships.\n"
    "STRICT RULES:\n"
    "- Most turns contain nothing durable — then return {\"facts\":[]}.\n"
    "- NEVER write meta-statements about the session itself (e.g. \"no new decisions "
    "were made\", \"project state unchanged\", \"configuration not altered\").\n"
    "- NEVER describe the assistant or its next steps. Only durable facts about the "
    "USER and their world.\n"
    "- NEVER invent or pad. If unsure, omit.\n"
    "- Each fact: a concise, self-contained third-person statement (resolve pronouns; "
    "name the subject).\n"
    f"- Return at most {_DISTILL_MAX_FACTS}.\n"
    'Return JSON: {"facts":[{"statement":"...","kind":"fact|preference|procedure"}]}.'
    "\n\nTurn:\n"
)


async def _distill_episode_facts(
    llm_client: Any, llm_model: str, content: str
) -> list[tuple[str, str]] | None:
    """LLM-distil an episode into 0..N durable (statement, kind) facts.

    Returns:
      - list of facts (possibly empty) on a successful LLM call;
      - None when the LLM call itself FAILED (e.g. rate limit) — the caller must
        then DEFER the episode (leave it unmarked) so it retries next pass.

    We never fall back to dumping raw sentences (that was the old noise source)."""
    try:
        resp = await llm_client.chat.completions.create(
            model=llm_model,
            messages=[
                {"role": "system", "content": _DISTILL_SYSTEM},
                {"role": "user", "content": _DISTILL_PROMPT + content[:6000]},
            ],
            temperature=0.0,
            max_tokens=1024,  # headroom for reasoning models (content after reasoning_content)
        )
        recs = json_records(resp.choices[0].message.content)
    except Exception as e:
        log.warning("distill LLM call failed (deferring episode): %s", e)
        return None

    # Flatten wrapper records: small models sometimes over-nest as
    # {"facts":[...]} or [{"facts":[...]}] instead of a bare list of facts.
    flat: list[dict] = []
    for r in recs:
        if isinstance(r, dict) and isinstance(r.get("facts"), list):
            flat.extend(x for x in r["facts"] if isinstance(x, dict))
        elif isinstance(r, dict):
            flat.append(r)

    facts: list[tuple[str, str]] = []
    for r in flat:
        stmt = (r.get("statement") or "").strip()
        if len(stmt) < 8:
            continue
        kind = (r.get("kind") or "fact").strip().lower()
        if kind not in ("fact", "preference", "procedure"):
            kind = "fact"
        facts.append((stmt, kind))
    return facts[:_DISTILL_MAX_FACTS]


_DISTILL_BATCH = 6  # episodes distilled per LLM call (amortizes the instruction overhead)

_TRIVIAL_RE = re.compile(
    r"^[\s\W]*(?:(?:ok(?:ay)?|thanks?|thank you|yes|yep|yeah|yup|no|nope|nah|sure|"
    r"got it|cool|nice|great|perfect|awesome|continue|go ahead|proceed|please do|"
    r"do it|done|working|works|back|hi|hello|hey|sounds good)[\s\W]*)+$",
    re.I,
)


def _is_trivial_turn(content: str) -> bool:
    """True only for turns that cannot hold a durable fact — pure acknowledgments /
    greetings, or near-empty. CONSERVATIVE by design: requires the WHOLE turn to be
    content-word-free (the regex anchors ^…$), so a real short fact like "use
    Postgres for JobPilot" is never skipped. Lets the summarize pass mark these
    processed WITHOUT spending an LLM call."""
    c = (content or "").strip()
    if len(c) < 12:
        return True
    if len(c) <= 120 and _TRIVIAL_RE.match(c):
        return True
    return False


_DISTILL_BATCH_SYSTEM = (
    "You extract durable long-term memories about the USER and their projects from "
    "conversation turns. You are extremely selective. Return JSON only."
)


def _distill_batch_prompt(items: list[tuple[str, str]]) -> str:
    head = (
        "Below are several conversation turns, each marked [#N]. For EACH turn, extract "
        "only facts that will still matter in months: the user's stable preferences, "
        "decisions they made, concrete project/system facts, identity, or relationships.\n"
        "STRICT RULES:\n"
        "- Most turns contain nothing durable — then give that turn an empty facts list.\n"
        "- NEVER write meta-statements about the session itself.\n"
        "- NEVER describe the assistant or its next steps. Only durable facts about the USER and their world.\n"
        "- NEVER invent or pad. If unsure, omit.\n"
        "- Each fact: a concise, self-contained third-person statement (resolve pronouns; name the subject).\n"
        f"- At most {_DISTILL_MAX_FACTS} facts per turn.\n"
        "- Return EXACTLY one result object per turn number N below, even when its facts list is empty.\n"
        'Return JSON: {"results":[{"n":N,"facts":[{"statement":"...","kind":"fact|preference|procedure"}]}]}'
        "\n\n"
    )
    body = "\n\n".join(f"[#{i + 1}]\n{(c or '')[:4000]}" for i, (_eid, c) in enumerate(items))
    return head + body


async def _distill_episodes_batch(
    llm_client: Any, llm_model: str, items: list[tuple[str, str]]
) -> dict[str, list[tuple[str, str]]] | None:
    """Distil up to _DISTILL_BATCH episodes in ONE LLM call.

    items: [(epi_id, content)]. Returns {epi_id: [(statement, kind), …]} ONLY for the
    turns the model actually answered (results keyed by turn number → epi_id). Episodes
    the model omitted are simply absent from the map → the caller leaves them unmarked
    so they retry next pass (never lost, never mis-attributed). Returns None if the LLM
    call itself failed → caller defers the whole batch."""
    if not items:
        return {}
    try:
        resp = await llm_client.chat.completions.create(
            model=llm_model,
            messages=[
                {"role": "system", "content": _DISTILL_BATCH_SYSTEM},
                {"role": "user", "content": _distill_batch_prompt(items)},
            ],
            temperature=0.0,
            max_tokens=512 + 256 * len(items),  # headroom: per-turn facts + reasoning models
        )
        recs = json_records(resp.choices[0].message.content)
    except Exception as e:
        log.warning("batch distill LLM call failed (deferring %d episodes): %s", len(items), e)
        return None

    # Unwrap {"results":[…]} (small models sometimes nest or return a bare list).
    rows: list[dict] = []
    for r in recs:
        if isinstance(r, dict) and isinstance(r.get("results"), list):
            rows.extend(x for x in r["results"] if isinstance(x, dict))
        elif isinstance(r, dict):
            rows.append(r)

    out: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        try:
            idx = int(row.get("n")) - 1
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(items)):
            continue
        epi_id = items[idx][0]
        facts: list[tuple[str, str]] = []
        raw = row.get("facts")
        if isinstance(raw, list):
            for f in raw:
                if not isinstance(f, dict):
                    continue
                stmt = (f.get("statement") or "").strip()
                if len(stmt) < 8:
                    continue
                kind = (f.get("kind") or "fact").strip().lower()
                if kind not in ("fact", "preference", "procedure"):
                    kind = "fact"
                facts.append((stmt, kind))
        out[epi_id] = facts[:_DISTILL_MAX_FACTS]
    return out


async def _pass_summarize(
    pool: Any,
    redis: Any,
    embedder: Any,
    publisher: Any,
    scope_text: str,
    llm_client: Any = None,
    llm_model: str = "",
) -> dict:
    """SUMMARIZE pass: distil episodes without derived memories into clean,
    LLM-curated facts. This is the ONLY producer of recallable memories from
    raw turns, so it is the cleaning choke point.

    Skips when no LLM is available (we never regex-dump raw sentences). Episodes
    that already have a derived memory are skipped — that includes jrnl entries,
    which are written straight to memories at ingest and must pass through
    untouched (also matched explicitly on source_ref as a guard).
    """
    result = {"pass": "summarize", "episodes_processed": 0, "facts_extracted": 0,
              "skipped_no_llm": 0, "deferred": 0, "prefiltered": 0, "errors": 0}

    if llm_client is None:
        result["skipped_no_llm"] = 1
        log.info("SUMMARIZE skipped for scope %s: no LLM client", scope_text)
        return result

    try:
        async with pool.connection() as cur_conn:
            # Episodes with no derived memory yet, excluding jrnl (direct-to-memory).
            async with cur_conn.cursor() as cur:
                await cur.execute(
                    "SELECT e.id, e.content FROM episodes e "
                    "WHERE e.scope = %s "
                    "AND (e.source_ref IS NULL OR e.source_ref NOT LIKE 'jrnl:%%') "
                    "AND NOT EXISTS ("
                    "  SELECT 1 FROM memories m "
                    "  WHERE m.source_episode_id = e.id AND m.scope = %s"
                    ") "
                    "ORDER BY e.created_at "
                    "LIMIT %s",
                    (scope_text, scope_text, _DISTILL_EPISODE_LIMIT),
                )
                episodes = await cur.fetchall()

            # Mark a chatter-only turn processed WITHOUT a recallable memory. A
            # zero-fact "marker" at tier='working' + valid_to is invisible to recall
            # (which reads active canonical) and satisfies the NOT-EXISTS guard so the
            # episode isn't re-distilled every pass.
            async def _mark_no_facts(epi_id: str) -> None:
                marker_id = mint("mem")
                async with cur_conn.cursor() as cur:
                    await cur.execute(
                        "INSERT INTO memories "
                        "(id, scope, statement, statement_key, kind, tier, "
                        " epistemic_status, valid_from, valid_to, ingested_at, "
                        " source_episode_id) "
                        "VALUES (%s, %s, '(no durable facts)', %s, 'fact', "
                        "'working', 'hypothesis', now(), now(), now(), %s) "
                        "ON CONFLICT DO NOTHING",
                        (marker_id, scope_text, f"_distilled:{epi_id}", epi_id),
                    )

            async def _store_facts(epi_id: str, facts: list[tuple[str, str]]) -> None:
                for fact_text, kind in facts:
                    mem_id = mint("mem")
                    skey = re.sub(r"\s+", " ", fact_text.strip().lower()).rstrip(" .!?,;:")
                    async with cur_conn.cursor() as cur:
                        await cur.execute(
                            "INSERT INTO memories "
                            "(id, scope, statement, statement_key, kind, tier, "
                            " epistemic_status, valid_from, ingested_at, source_episode_id) "
                            "VALUES (%s, %s, %s, %s, %s, 'semantic', "
                            "'canonical', now(), now(), %s) "
                            "ON CONFLICT DO NOTHING",
                            (mem_id, scope_text, fact_text, skey, kind, epi_id),
                        )
                    ev = build_event(
                        short_type="memory.fact.stored",
                        subject=mem_id,
                        scope=_scope_obj_from_text(scope_text),
                        data={"mem_id": mem_id, "scope": scope_text, "source_episode_id": epi_id},
                        actor="agent:consolidation",
                        source="curlyos-core/consolidation",
                    )
                    try:
                        _stored, subj, stamped = await publisher.stage(ev, cur_conn)
                        await publisher.emit(subj, stamped)
                    except Exception:
                        pass
                    result["facts_extracted"] += 1

            # ---- pre-filter: pure-acknowledgment turns can't hold a durable fact;
            # mark them processed WITHOUT spending an LLM call ----
            to_distill: list[tuple[str, str]] = []
            for epi_id, content in episodes:
                if _is_trivial_turn(content or ""):
                    try:
                        await _mark_no_facts(epi_id)
                        result["episodes_processed"] += 1
                        result["prefiltered"] += 1
                    except Exception as e:
                        result["errors"] += 1
                        log.warning("SUMMARIZE prefilter-mark failed for %s: %s", epi_id, e)
                else:
                    to_distill.append((epi_id, content))

            # ---- batch the rest: up to _DISTILL_BATCH episodes per LLM call ----
            for start in range(0, len(to_distill), _DISTILL_BATCH):
                batch = to_distill[start:start + _DISTILL_BATCH]
                results_map = await _distill_episodes_batch(llm_client, llm_model, batch)
                # Whole call failed (rate limit, timeout): DEFER this batch and all
                # that follow — leave them unmarked so the next pass retries.
                if results_map is None:
                    result["deferred"] += len(to_distill) - start
                    log.info("SUMMARIZE deferring %d episodes for scope %s (LLM unavailable)",
                             len(to_distill) - start, scope_text)
                    break
                for epi_id, _content in batch:
                    if epi_id not in results_map:
                        # model omitted this turn — leave unmarked → retries next pass
                        result["deferred"] += 1
                        continue
                    try:
                        facts = results_map[epi_id]
                        if facts:
                            await _store_facts(epi_id, facts)
                        else:
                            await _mark_no_facts(epi_id)
                        result["episodes_processed"] += 1
                    except Exception as e:
                        result["errors"] += 1
                        log.warning("SUMMARIZE failed for episode %s: %s", epi_id, e)

    except Exception as e:
        log.error("SUMMARIZE pass failed for scope %s: %s", scope_text, e)
        result["errors"] += 1

    return result


async def _pass_decay(
    pool: Any,
    redis: Any,
    publisher: Any,
    scope_text: str,
) -> dict:
    """DECAY pass: archive cold rows (no access in 90 days).

    Invalidate expired speculative content.
    """
    result = {"pass": "decay", "archived": 0, "invalidated_speculative": 0, "errors": 0}
    cutoff = datetime.now(timezone.utc) - timedelta(days=_DECAY_COLD_DAYS)

    try:
        async with pool.connection() as conn:
            # Archive cold memories (old, no recent access — use created_at as proxy)
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE memories SET valid_to = now() "
                    "WHERE scope = %s AND valid_to IS NULL "
                    "AND created_at < %s "
                    "AND tier = 'working' "
                    "AND epistemic_status = 'canonical'",
                    (scope_text, cutoff),
                )
                result["archived"] = cur.rowcount or 0

            # Invalidate expired speculative content
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE memories SET valid_to = now() "
                    "WHERE scope = %s AND valid_to IS NULL "
                    "AND epistemic_status = ANY(%s) "
                    "AND created_at < %s",
                    (scope_text, list(_DECAY_SPECULATIVE_STATUSES), cutoff),
                )
                result["invalidated_speculative"] = cur.rowcount or 0

    except Exception as e:
        log.error("DECAY pass failed for scope %s: %s", scope_text, e)
        result["errors"] += 1

    return result


async def _pass_recombine_incubate(
    pool: Any,
    redis: Any,
    embedder: Any,
    publisher: Any,
    scope_text: str,
) -> dict:
    """RECOMBINE/INCUBATE pass: nightly creative pass.

    Find clusters of related memories. Generate conjecture-level hypotheses.
    Write at epistemic_status="conjecture".
    """
    result = {"pass": "recombine_incubate", "clusters_found": 0, "conjectures": 0, "errors": 0}

    try:
        async with pool.connection() as conn:
            # Find pairs of related memories (share words, different content)
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT m1.id, m1.statement, m2.id, m2.statement "
                    "FROM memories m1 "
                    "JOIN memories m2 ON m1.id < m2.id "
                    "WHERE m1.scope = %s AND m2.scope = %s "
                    "AND m1.valid_to IS NULL AND m2.valid_to IS NULL "
                    "AND m1.epistemic_status = 'canonical' "
                    "AND m2.epistemic_status = 'canonical' "
                    "ORDER BY m1.created_at DESC "
                    "LIMIT 100",
                    (scope_text, scope_text),
                )
                pairs = await cur.fetchall()

            seen_pairs: set[tuple[str, str]] = set()

            for id1, stmt1, id2, stmt2 in pairs:
                try:
                    # Check if statements share significant words (simple clustering)
                    words1 = set(stmt1.lower().split()) - {"the", "a", "an", "is", "are", "was", "were", "in", "on", "at", "to", "of", "and", "or", "for", "with", "that", "this", "it", "as", "by", "from"}
                    words2 = set(stmt2.lower().split()) - {"the", "a", "an", "is", "are", "was", "were", "in", "on", "at", "to", "of", "and", "or", "for", "with", "that", "this", "it", "as", "by", "from"}

                    if len(words1) < 3 or len(words2) < 3:
                        continue

                    overlap = words1 & words2
                    similarity = len(overlap) / max(min(len(words1), len(words2)), 1)

                    # Related but not too similar (not duplicates)
                    if 0.2 <= similarity <= 0.7:
                        pair_key = (min(id1, id2), max(id1, id2))
                        if pair_key in seen_pairs:
                            continue
                        seen_pairs.add(pair_key)
                        result["clusters_found"] += 1

                        # Generate a conjecture
                        conjecture_text = (
                            f"Possible connection: '{stmt1[:80]}' "
                            f"may relate to '{stmt2[:80]}' "
                            f"(shared concepts: {', '.join(list(overlap)[:5])})"
                        )

                        mem_id = mint("mem")
                        skey = re.sub(r"\s+", " ", conjecture_text.strip().lower()).rstrip(" .!?,;:")

                        async with conn.cursor() as cur:
                            await cur.execute(
                                "INSERT INTO memories "
                                "(id, scope, statement, statement_key, kind, tier, "
                                " epistemic_status, valid_from, ingested_at, source_episode_id) "
                                "VALUES (%s, %s, %s, %s, 'fact', 'semantic', "
                                "'conjecture', now(), now(), %s) "
                                "ON CONFLICT DO NOTHING",
                                (mem_id, scope_text, conjecture_text, skey, id1),
                            )

                        ev = build_event(
                            short_type="memory.fact.stored",
                            subject=mem_id,
                            scope=_scope_obj_from_text(scope_text),
                            data={
                                "mem_id": mem_id,
                                "scope": scope_text,
                                "epistemic_status": "conjecture",
                                "source_episode_id": id1,
                            },
                            actor="agent:consolidation",
                            source="curlyos-core/consolidation",
                        )
                        try:
                            _stored, subj, stamped = await publisher.stage(ev, conn)
                            await publisher.emit(subj, stamped)
                        except Exception:
                            pass

                        result["conjectures"] += 1

                        # Cap conjectures per run
                        if result["conjectures"] >= 20:
                            break
                except Exception as e:
                    result["errors"] += 1
                    log.warning("RECOMBINE pair (%s, %s) failed: %s", id1, id2, e)

    except Exception as e:
        log.error("RECOMBINE/INCUBATE pass failed for scope %s: %s", scope_text, e)
        result["errors"] += 1

    return result


# ── Per-scope loop ─────────────────────────────────────────────────────────

async def project_scope(
    pool: Any, redis: Any, embedder: Any, publisher: Any,
    scope_text: str, *, live: bool, replay: bool = False
) -> dict[str, Any]:
    token = await _acquire(redis, scope_text)
    if token is None:
        return {"scope": scope_text, "skipped": "locked"}

    counts = {"embedded": 0, "episode": 0, "invalidated": 0, "tombstoned": 0, "missing": 0, "noop": 0}
    ops: list[tuple] = []
    to_emit: list[str] = []
    processed = 0
    try:
        async with pool.connection() as conn:
            if replay:
                await _flush_scope_pg(conn, scope_text)
                ops.append(("flush", scope_text))
            last = await _read_watermark(conn, scope_text)
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, type, subject, data, seq FROM events "
                    "WHERE scope = %s AND seq > %s ORDER BY seq",
                    (scope_text, last),
                )
                rows = await cur.fetchall()
            for (_ev_id, ev_type, subject, data, seq) in rows or []:
                action = await project_event(conn, ops, embedder, ev_type, subject, data, scope_text)
                counts[action] = counts.get(action, 0) + 1
                if action == "embedded" and live and publisher is not None:
                    to_emit.append((data or {}).get("mem_id") or subject)
                await _advance_watermark(conn, scope_text, int(seq))
                processed += 1
        await _apply_redis_ops(redis, ops)
    finally:
        await _release(redis, scope_text, token)

    for mem_id in to_emit:
        await _emit_consolidated(publisher, mem_id, scope_text)

    return {"scope": scope_text, "processed": processed, "replay": replay, **counts}


async def _scopes_with_events(pool: Any) -> list[str]:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT DISTINCT scope FROM events ORDER BY scope")
            rows = await cur.fetchall()
    return [r[0] for r in (rows or [])]


async def run_once(
    pool: Any, redis: Any, embedder: Any, publisher: Any,
    *, scope: str | None = None, replay: bool = False, live: bool = True
) -> dict[str, Any]:
    scopes = [scope] if scope else await _scopes_with_events(pool)
    results = []
    for sc in scopes:
        results.append(await project_scope(pool, redis, embedder, publisher, sc, live=live, replay=replay))
    return {"replay": replay, "live": live, "scopes": results}


# ── Full consolidation orchestrator ────────────────────────────────────────

ALL_PASSES = (
    "dedup",
    "merge_promote",
    "conflict_resolve",
    "summarize",
    "decay",
    "recombine_incubate",
)

# Fast (15m) path now distils first so new episodes become clean memories
# promptly, then dedup/conflict-resolve tidy the result in the same pass.
FAST_PASSES = (
    "summarize",
    "dedup",
    "conflict_resolve",
)


async def run_consolidation(
    pool: Any,
    redis: Any,
    embedder: Any,
    publisher: Any,
    reranker: Any = None,
    *,
    scope: str | None = None,
    deep: bool = False,
    llm_client: Any = None,
    llm_model: str = "",
) -> dict[str, Any]:
    """Run full consolidation pipeline for one or more scopes.

    Reads unprocessed events by seq watermark, projects them,
    then runs consolidation passes.

    Args:
        pool: Postgres connection pool.
        redis: Redis client.
        embedder: Embedder instance.
        publisher: Event publisher.
        reranker: Optional cross-encoder reranker.
        scope: Specific scope, or None for all scopes with events.
        deep: If True, run all passes including decay/recombine.
              If False, run fast passes (summarize + dedup + conflict_resolve).
        llm_client: Optional chat client for the SUMMARIZE distiller. When None,
              SUMMARIZE no-ops (no raw-sentence fallback).
        llm_model: Model id for the distiller.
    """
    passes = ALL_PASSES if deep else FAST_PASSES
    scopes = [scope] if scope else await _scopes_with_events(pool)

    if not scopes:
        # Also check for scopes that have memories but no events
        try:
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT DISTINCT scope FROM memories ORDER BY scope")
                    mem_scopes = [r[0] for r in (await cur.fetchall()) or []]
                    # Add scopes not already in list
                    for s in mem_scopes:
                        if s not in scopes:
                            scopes.append(s)
        except Exception:
            pass

    log.info("Starting consolidation: scopes=%s, deep=%d, passes=%s", scopes, deep, passes)

    all_results: list[dict] = []

    for sc in scopes:
        scope_result: dict[str, Any] = {
            "scope": sc,
            "deep": deep,
            "passes": {},
        }

        # Step 1: Project unprocessed events
        try:
            proj = await project_scope(pool, redis, embedder, publisher, sc, live=True)
            scope_result["projection"] = proj
        except Exception as e:
            log.error("Projection failed for scope %s: %s", sc, e)
            scope_result["projection_error"] = str(e)
            all_results.append(scope_result)
            continue

        # Step 2: Run consolidation passes
        pass_map = {
            "dedup": lambda: _pass_dedup(pool, redis, embedder, reranker, publisher, sc),
            "merge_promote": lambda: _pass_merge_promote(pool, redis, embedder, publisher, sc),
            "conflict_resolve": lambda: _pass_conflict_resolve(pool, redis, publisher, sc),
            "summarize": lambda: _pass_summarize(pool, redis, embedder, publisher, sc, llm_client, llm_model),
            "decay": lambda: _pass_decay(pool, redis, publisher, sc),
            "recombine_incubate": lambda: _pass_recombine_incubate(pool, redis, embedder, publisher, sc),
        }

        for pass_name in passes:
            try:
                pass_fn = pass_map[pass_name]
                pass_result = await pass_fn()
                scope_result["passes"][pass_name] = pass_result
                log.info("Pass %s for scope %s: %s", pass_name, sc, pass_result)
            except Exception as e:
                log.error("Pass %s failed for scope %s: %s", pass_name, sc, e)
                scope_result["passes"][pass_name] = {"error": str(e)}

        all_results.append(scope_result)

    return {
        "deep": deep,
        "passes_run": list(passes),
        "scopes": all_results,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Public pass aliases (task API) ─────────────────────────────────────────

async def _dedup_pass(
    pool: Any,
    scope: str,
    embedder: Any,
    reranker: Any,
) -> int:
    """DEDUP pass — public alias.

    Finds memory pairs with cosine similarity >= 0.92, confirms with
    cross-encoder reranker, merges by invalidating the older duplicate.

    Returns the number of dedup merge operations performed.
    """
    result = await _pass_dedup(pool, None, embedder, reranker, None, scope)
    return int(result.get("merged", 0))


async def _conflict_resolve_pass(
    pool: Any,
    scope: str,
) -> int:
    """CONFLICT-RESOLVE pass — public alias.

    Finds facts sharing the same (scope, statement_key) with both
    valid_to IS NULL, invalidates the older one.

    Returns the number of conflict resolutions performed.
    """
    result = await _pass_conflict_resolve(pool, None, None, scope)
    return int(result.get("resolved", 0))


async def _summarize_pass(
    pool: Any,
    scope: str,
    publisher: Any,
    embedder: Any,
) -> int:
    """SUMMARIZE pass — public alias.

    For episodes without derived memories, extracts key facts
    (LLM when API key available, otherwise sentence splitting).

    Returns the number of new memories created.
    """
    result = await _pass_summarize(pool, None, embedder, publisher, scope)
    return int(result.get("facts_extracted", 0))


async def _decay_pass(
    pool: Any,
    scope: str,
) -> int:
    """DECAY pass — public alias.

    Archives cold rows (no access in 90 days) and invalidates
    expired speculative / non-canonical content.

    Returns the total number of decayed items.
    """
    result = await _pass_decay(pool, None, None, scope)
    archived = int(result.get("archived", 0))
    invalidated = int(result.get("invalidated_speculative", 0))
    return archived + invalidated


async def _recombine_pass(
    pool: Any,
    scope: str,
    publisher: Any,
    embedder: Any,
) -> int:
    """RECOMBINE pass — public alias.

    Finds clusters of related memories and generates
    conjecture-level hypotheses.

    Returns the number of conjectures created.
    """
    result = await _pass_recombine_incubate(pool, None, embedder, publisher, scope)
    return int(result.get("conjectures", 0))
