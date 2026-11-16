"""Integration tests that exercise real SQL against Postgres.

Skipped unless CURLYOS_TEST_DATABASE_URL is set. CI supplies a pgvector
service container; local runs can point at a scratch database.
"""

from __future__ import annotations

import json
import os
import uuid

import psycopg.rows
import pytest
from psycopg_pool import AsyncConnectionPool

import migrate
from cognition.contradiction import list_candidates, resolve_candidate, scan
from cognition.working_set import (
    get_working_set, pin_memory, refresh_working_set, working_set_boost,
)
from shared.types.ulid import mint
from memory.chunking import split_text
from memory.governance import add, invalidate, record_episode
from memory.ingest_service import ingest_text

DSN = os.environ.get("CURLYOS_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="CURLYOS_TEST_DATABASE_URL is not set")


class _Publisher:
    """Minimal publisher: events are staged but never emitted."""

    async def stage(self, ev, conn):
        return None, "subject", ev

    async def emit(self, subject, ev):
        return None


@pytest.fixture
async def pool():
    migrate.run_migrations(DSN)
    conn = AsyncConnectionPool(
        DSN,
        min_size=1,
        max_size=2,
        open=False,
        kwargs={"row_factory": psycopg.rows.tuple_row},
    )
    await conn.open()
    try:
        yield conn
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_add_deduplicates_active_fact(pool):
    pub = _Publisher()
    scope = "test:integration"
    text = f"Dedup integration {uuid.uuid4()}: identical active facts collapse."
    epi = await record_episode(pool, pub, scope, content=text)

    first = await add(pool, pub, scope, text, epi["epi_id"])
    second = await add(pool, pub, scope, text, epi["epi_id"])

    assert first["deduplicated"] is False
    assert second["deduplicated"] is True
    assert second["mem_id"] == first["mem_id"]

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM memories "
                "WHERE scope = %s AND statement = %s AND valid_to IS NULL",
                (scope, text),
            )
            assert (await cur.fetchone())[0] == 1

    await invalidate(pool, pub, scope, first["mem_id"], reason="integration cleanup")

    # A closed interval no longer blocks the same statement: history keeps both.
    third = await add(pool, pub, scope, text, epi["epi_id"])
    assert third["deduplicated"] is False
    assert third["mem_id"] != first["mem_id"]
    await invalidate(pool, pub, scope, third["mem_id"], reason="integration cleanup")


@pytest.mark.asyncio
async def test_working_set_refresh_preserves_pins(pool):
    pub = _Publisher()
    scope = "test:integration"
    epi = await record_episode(pool, pub, scope, content="working set integration")
    mem_ids = []
    for i in range(3):
        mem = await add(
            pool, pub, scope,
            f"Working set fact {uuid.uuid4()} number {i}",
            epi["epi_id"],
        )
        mem_ids.append(mem["mem_id"])

    await pin_memory(pool, scope, mem_ids[0], "integration pin")
    result = await refresh_working_set(pool, pub, scope, size=10, ttl_hours=1)
    assert result["selected"] >= 1
    assert result["pinned"] == 1

    items = await get_working_set(pool, scope, 10)
    pinned = [item for item in items if item["memory_id"] == mem_ids[0]]
    assert pinned and pinned[0]["pinned"] is True
    assert pinned[0]["salience"] == 1.0

    boost = await working_set_boost(pool, scope)
    assert boost.get(mem_ids[0]) == 1.0

    for mem_id in mem_ids:
        await invalidate(pool, pub, scope, mem_id, reason="integration cleanup")
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM working_set WHERE scope = %s AND memory_id = ANY(%s)",
                (scope, mem_ids),
            )


@pytest.mark.asyncio
async def test_duplicate_ingest_leaves_a_closed_marker(pool):
    pub = _Publisher()
    scope = "test:integration"
    text = f"Duplicate capture {uuid.uuid4()}: the second write must not re-summarize forever."
    source = "test:integration"

    first = await ingest_text(pool, pub, scope, text, source_ref=source)
    second = await ingest_text(pool, pub, scope, text, source_ref=source)

    assert first["mem_chunks"][0][0] == second["mem_chunks"][0][0]

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM memories WHERE source_episode_id = %s AND valid_to IS NOT NULL",
                (second["epi_id"],),
            )
            assert (await cur.fetchone())[0] == 1

    await invalidate(pool, pub, scope, first["mem_chunks"][0][0], reason="integration cleanup")


@pytest.mark.asyncio
async def test_chunked_statements_cover_the_tail(pool):
    pub = _Publisher()
    scope = "test:integration"
    marker = f"TAIL{uuid.uuid4().hex}"
    paragraphs = [f"Paragraph {i}. " + ("content " * 120) for i in range(12)]
    text = "\n\n".join(paragraphs + [f"The final line contains {marker}."])
    chunks = split_text(text)
    assert len(chunks) > 1

    epi = await record_episode(pool, pub, scope, content=text)
    mem_ids = []
    for chunk in chunks:
        mem = await add(pool, pub, scope, chunk, epi["epi_id"])
        mem_ids.append(mem["mem_id"])

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM memories "
                "WHERE id = ANY(%s::text[]) AND valid_to IS NULL",
                (mem_ids,),
            )
            assert (await cur.fetchone())[0] == len(mem_ids)

            await cur.execute(
                "SELECT count(*) FROM memories "
                "WHERE scope = %s AND statement LIKE %s AND valid_to IS NULL",
                (scope, f"%{marker}%"),
            )
            assert (await cur.fetchone())[0] == 1

    for mem_id in mem_ids:
        await invalidate(pool, pub, scope, mem_id, reason="integration cleanup")


class _FakeLLM:
    def __init__(self):
        payload = json.dumps([{
            "index": 1,
            "relation": "contradiction",
            "reason": "the two statements cannot both hold",
        }])
        message = type("Message", (), {"content": payload})()
        choice = type("Choice", (), {"message": message})()
        response = type("Response", (), {"choices": [choice]})()

        class Completions:
            async def create(self, **kwargs):
                return response

        self.chat = type("Chat", (), {"completions": Completions()})()


@pytest.mark.asyncio
async def test_identity_conflict_scan_and_resolve(pool):
    pub = _Publisher()
    scope = "test:integration"
    epi = await record_episode(pool, pub, scope, content="identity conflict")

    ids = []
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            for obj in ("value one", "value two"):
                fact_id = mint("idf")
                ids.append(fact_id)
                await cur.execute(
                    "INSERT INTO identity_facts "
                    "(id, scope, predicate, object, confidence, epistemic_status, "
                    " valid_from, ingested_at, created_at, source_episode_id) "
                    "VALUES (%s, %s, %s, %s, 0.9, 'canonical', now(), now(), now(), %s)",
                    (fact_id, scope, "test_predicate", obj, epi["epi_id"]),
                )

    result = await scan(pool, pub, scope)
    assert result["identity_conflicts"] == 1
    assert result["inserted"] == 1

    candidates = await list_candidates(pool, scope)
    candidate = next(c for c in candidates if c["kind"] == "identity_identity")
    resolved = await resolve_candidate(pool, pub, scope, candidate["id"], "supersede_left")
    assert resolved["status"] == "resolved"

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM identity_facts "
                "WHERE id = ANY(%s) AND valid_to IS NULL",
                (ids,),
            )
            assert (await cur.fetchone())[0] == 1
            await cur.execute(
                "UPDATE identity_facts SET valid_to = now() "
                "WHERE scope = %s AND predicate = 'test_predicate' AND valid_to IS NULL",
                (scope,),
            )
            await cur.execute(
                "DELETE FROM contradiction_candidates WHERE scope = %s", (scope,),
            )


@pytest.mark.asyncio
async def test_memory_contradiction_scan_and_resolve(pool):
    pub = _Publisher()
    scope = "test:integration"
    epi = await record_episode(pool, pub, scope, content="memory conflict")
    left = await add(
        pool, pub, scope,
        f"The test setting is enabled {uuid.uuid4()}", epi["epi_id"],
    )
    right = await add(
        pool, pub, scope,
        f"The test setting is disabled {uuid.uuid4()}", epi["epi_id"],
    )

    base = [1.0] * 1024
    alternate = [1.0] * 1024
    for i in range(100):
        alternate[i] = -1.0

    def vector(values):
        return "[" + ",".join(repr(float(v)) for v in values) + "]"

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE memories SET embedding = %s::vector WHERE id = %s",
                (vector(base), left["mem_id"]),
            )
            await cur.execute(
                "UPDATE memories SET embedding = %s::vector WHERE id = %s",
                (vector(alternate), right["mem_id"]),
            )

    result = await scan(
        pool, pub, scope,
        llm_client=_FakeLLM(), llm_model="fake",
        similarity_floor=0.80, similarity_ceiling=0.97, max_pairs=10,
    )
    assert result["memory_conflicts"] >= 1
    assert result["inserted"] >= 1

    candidates = await list_candidates(pool, scope)
    candidate = next(c for c in candidates if c["kind"] == "memory_memory")
    resolved = await resolve_candidate(pool, pub, scope, candidate["id"], "supersede_left")
    assert resolved["status"] == "resolved"

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE memories SET valid_to = now() "
                "WHERE id = ANY(%s) AND valid_to IS NULL",
                ([left["mem_id"], right["mem_id"]],),
            )
            await cur.execute(
                "DELETE FROM contradiction_candidates WHERE scope = %s", (scope,),
            )


@pytest.mark.asyncio
async def test_intention_surface_complete_and_snooze(pool):
    from datetime import datetime, timedelta, timezone

    from cognition.prospective import (
        complete_intention, create_intention, due_intentions,
        snooze_intention, surface_due_intentions,
    )

    pub = _Publisher()
    scope = "test:intentions"
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM intentions WHERE scope = %s", (scope,))

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    first = await create_intention(pool, pub, scope, "Test intention A", due_at=past)
    second = await create_intention(pool, pub, scope, "Test intention B", due_at=past)

    assert await surface_due_intentions(pool, pub, scope) == 2
    due = await due_intentions(pool, scope)
    assert {item["id"] for item in due} == {first["id"], second["id"]}
    assert all(item["status"] == "surfaced" for item in due)
    assert await surface_due_intentions(pool, pub, scope) == 0

    future = datetime.now(timezone.utc) + timedelta(hours=2)
    assert await snooze_intention(pool, pub, scope, first["id"], future)
    due = await due_intentions(pool, scope)
    assert first["id"] not in {item["id"] for item in due}

    assert await complete_intention(pool, pub, scope, second["id"])
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM intentions WHERE scope = %s", (scope,))


@pytest.mark.asyncio
async def test_decision_reviews_become_intentions_once(pool):
    from datetime import datetime, timedelta, timezone

    from cognition.prospective import list_intentions, sync_decision_reviews

    pub = _Publisher()
    scope = "test:intentions"
    decision_id = mint("dec")
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM intentions WHERE scope = %s", (scope,))
            await cur.execute("DELETE FROM decisions WHERE scope = %s", (scope,))
            await cur.execute(
                "INSERT INTO decisions "
                "(id, scope, title, chosen, rationale, review_at, decided_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, now())",
                (decision_id, scope, "Test decision", "option A", "because", past),
            )

    assert await sync_decision_reviews(pool, pub, scope) == 1
    assert await sync_decision_reviews(pool, pub, scope) == 0

    items = await list_intentions(pool, scope)
    assert any(
        item["source"] == "decision_review" and item["source_ref"] == decision_id
        for item in items
    )

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM intentions WHERE scope = %s", (scope,))
            await cur.execute("DELETE FROM decisions WHERE scope = %s", (scope,))


@pytest.mark.asyncio
async def test_resolve_already_closed_identity_candidate(pool):
    from identity import invalidate_identity_fact

    pub = _Publisher()
    scope = "test:integration"
    epi = await record_episode(pool, pub, scope, content="already closed conflict")
    ids = []
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            for obj in ("alpha", "beta"):
                fact_id = mint("idf")
                ids.append(fact_id)
                await cur.execute(
                    "INSERT INTO identity_facts "
                    "(id, scope, predicate, object, confidence, epistemic_status, "
                    " valid_from, ingested_at, created_at, source_episode_id) "
                    "VALUES (%s, %s, %s, %s, 0.9, 'canonical', now(), now(), now(), %s)",
                    (fact_id, scope, "already_closed_predicate", obj, epi["epi_id"]),
                )

    await scan(pool, pub, scope)
    candidates = await list_candidates(pool, scope)
    candidate = next(
        c for c in candidates
        if c["kind"] == "identity_identity" and c.get("predicate") == "already_closed_predicate"
    )
    # Close the victim before the user resolves the proposal.
    await invalidate_identity_fact(pool, pub, scope, candidate["left_id"])

    resolved = await resolve_candidate(pool, pub, scope, candidate["id"], "supersede_left")
    assert resolved["status"] == "resolved"
    assert resolved["already_closed"] is True

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE identity_facts SET valid_to = now() "
                "WHERE scope = %s AND predicate = 'already_closed_predicate' AND valid_to IS NULL",
                (scope,),
            )
            await cur.execute(
                "DELETE FROM contradiction_candidates WHERE scope = %s", (scope,),
            )
