"""Integration tests that exercise real SQL against Postgres.

Skipped unless CURLYOS_TEST_DATABASE_URL is set. CI supplies a pgvector
service container; local runs can point at a scratch database.
"""

from __future__ import annotations

import os
import uuid

import psycopg.rows
import pytest
from psycopg_pool import AsyncConnectionPool

import migrate
from cognition.working_set import (
    get_working_set, pin_memory, refresh_working_set, working_set_boost,
)
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
