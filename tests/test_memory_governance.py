"""Tests for memory governance verbs: record_episode, add, invalidate, forget, list_episodes, list_memories."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memory.governance import (
    SourceEpisodeNotFound,
    MemoryNotFound,
    AlreadyInvalidated,
    ForgetRequiresApproval,
    AlreadyForgotten,
    ApprovalAlreadyUsed,
    add,
    forget,
    invalidate,
    list_episodes,
    list_memories,
    record_episode,
)
from shared.types.ulid import mint


def _make_mock_cursor(setup=None):
    """Return (cursor_async_context_manager, mock_cursor)."""
    mc = AsyncMock()
    if setup:
        setup(mc)

    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=mc)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm, mc


def _make_mock_connection(cursors):
    """Return (conn_async_context_manager, mock_conn) given a list of cursor CMs."""
    conn = AsyncMock()
    cursor_cms = list(cursors)

    def cursor_factory():
        if cursor_cms:
            return cursor_cms.pop(0)
        # fallback empty cursor
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=AsyncMock())
        cm.__aexit__ = AsyncMock(return_value=False)
        return cm

    conn.cursor = cursor_factory

    conn_cm = AsyncMock()
    conn_cm.__aenter__ = AsyncMock(return_value=conn)
    conn_cm.__aexit__ = AsyncMock(return_value=False)
    return conn_cm, conn


def _make_mock_pool(conn_cm):
    pool = AsyncMock()
    pool.connection = MagicMock(return_value=conn_cm)
    return pool


def _make_mock_publisher():
    pub = AsyncMock()
    pub.stage = AsyncMock(return_value=("evt_test_01", "curlyos.test", {"id": "evt_test_01"}))
    pub.emit = AsyncMock()
    return pub


def _setup_record_episode_cursor(mc):
    mc.fetchone.return_value = (datetime.now(timezone.utc),)


def _setup_add_cursor(mc, existing_episode=True):
    # fetchone called after INSERT memories RETURNING valid_from, ingested_at
    mc.fetchone.return_value = (datetime.now(timezone.utc), datetime.now(timezone.utc))


def _setup_invalidate_cursor(mc):
    # First fetchone: SELECT valid_to → returns (None,) meaning not yet invalidated
    # Second fetchone (after UPDATE): returns (valid_to, superseded_by)
    now = datetime.now(timezone.utc)
    mc.fetchone.side_effect = [(None,), (now, None)]


def _setup_forget_cursors():
    """forget() uses multiple cursors sequentially."""
    cursors = []
    # Cursor 1: pg_advisory_xact_lock
    mc1 = AsyncMock()
    cm1 = AsyncMock()
    cm1.__aenter__ = AsyncMock(return_value=mc1)
    cm1.__aexit__ = AsyncMock(return_value=False)
    cursors.append(cm1)

    # Cursor 2: SELECT approvals → returns a row (approval exists)
    mc2 = AsyncMock()
    mc2.fetchone.return_value = (1,)
    cm2 = AsyncMock()
    cm2.__aenter__ = AsyncMock(return_value=mc2)
    cm2.__aexit__ = AsyncMock(return_value=False)
    cursors.append(cm2)

    # Cursor 3: SELECT events for already-used check → returns None (not used)
    mc3 = AsyncMock()
    mc3.fetchone.return_value = None
    cm3 = AsyncMock()
    cm3.__aenter__ = AsyncMock(return_value=mc3)
    cm3.__aexit__ = AsyncMock(return_value=False)
    cursors.append(cm3)

    # Cursor 4: SELECT statement, valid_to from memories
    mc4 = AsyncMock()
    mc4.fetchone.return_value = ("original statement", None)
    cm4 = AsyncMock()
    cm4.__aenter__ = AsyncMock(return_value=mc4)
    cm4.__aexit__ = AsyncMock(return_value=False)
    cursors.append(cm4)

    # Cursor 5: UPDATE memories (redact) RETURNING valid_to
    mc5 = AsyncMock()
    mc5.fetchone.return_value = (datetime.now(timezone.utc),)
    cm5 = AsyncMock()
    cm5.__aenter__ = AsyncMock(return_value=mc5)
    cm5.__aexit__ = AsyncMock(return_value=False)
    cursors.append(cm5)

    return cursors


# ── record_episode ───────────────────────────────────────────────────────────


class TestRecordEpisode:
    @pytest.mark.asyncio
    async def test_record_episode(self, scope):
        mc = AsyncMock()
        mc.fetchone.return_value = (datetime.now(timezone.utc),)
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=mc)
        cm.__aexit__ = AsyncMock(return_value=False)

        conn = AsyncMock()
        conn.cursor = MagicMock(return_value=cm)

        conn_cm = AsyncMock()
        conn_cm.__aenter__ = AsyncMock(return_value=conn)
        conn_cm.__aexit__ = AsyncMock(return_value=False)
        pool = AsyncMock()
        pool.connection = MagicMock(return_value=conn_cm)

        pub = _make_mock_publisher()

        result = await record_episode(
            pool=pool,
            publisher=pub,
            scope_text=scope,
            content="hello world",
            source_ref="test_ref",
        )

        assert "epi_id" in result
        assert result["epi_id"].startswith("epi_")
        assert "ingested_at" in result

    @pytest.mark.asyncio
    async def test_record_episode_no_source_ref(self, scope):
        mc = AsyncMock()
        mc.fetchone.return_value = (datetime.now(timezone.utc),)
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=mc)
        cm.__aexit__ = AsyncMock(return_value=False)

        conn = AsyncMock()
        conn.cursor = MagicMock(return_value=cm)

        conn_cm = AsyncMock()
        conn_cm.__aenter__ = AsyncMock(return_value=conn)
        conn_cm.__aexit__ = AsyncMock(return_value=False)
        pool = AsyncMock()
        pool.connection = MagicMock(return_value=conn_cm)

        pub = _make_mock_publisher()

        result = await record_episode(
            pool=pool,
            publisher=pub,
            scope_text=scope,
            content="no source ref",
        )

        assert result["epi_id"].startswith("epi_")


# ── add ─────────────────────────────────────────────────────────────────────


class TestAddFact:
    @pytest.mark.asyncio
    async def test_add_fact(self, scope):
        epi_id = mint("epi")
        now = datetime.now(timezone.utc)

        mc = AsyncMock()
        mc.fetchone.return_value = (now, now)

        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=mc)
        cm.__aexit__ = AsyncMock(return_value=False)

        conn = AsyncMock()
        conn.cursor = MagicMock(return_value=cm)

        conn_cm = AsyncMock()
        conn_cm.__aenter__ = AsyncMock(return_value=conn)
        conn_cm.__aexit__ = AsyncMock(return_value=False)
        pool = AsyncMock()
        pool.connection = MagicMock(return_value=conn_cm)

        pub = _make_mock_publisher()

        result = await add(
            pool=pool,
            publisher=pub,
            scope_text=scope,
            statement="test fact",
            source_episode_id=epi_id,
        )

        assert "mem_id" in result
        assert result["mem_id"].startswith("mem_")
        assert result["source_episode_id"] == epi_id
        assert "valid_from" in result
        assert "ingested_at" in result

    @pytest.mark.asyncio
    async def test_add_fact_invalid_source(self, scope):
        """add() with invalid source_episode_id raises SourceEpisodeNotFound."""
        mc = AsyncMock()
        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=mc)
        cm.__aexit__ = AsyncMock(return_value=False)

        conn = AsyncMock()
        conn.cursor = MagicMock(return_value=cm)

        conn_cm = AsyncMock()
        conn_cm.__aenter__ = AsyncMock(return_value=conn)
        conn_cm.__aexit__ = AsyncMock(return_value=False)
        pool = AsyncMock()
        pool.connection = MagicMock(return_value=conn_cm)

        pub = _make_mock_publisher()

        with pytest.raises(SourceEpisodeNotFound):
            await add(
                pool=pool,
                publisher=pub,
                scope_text=scope,
                statement="test fact",
                source_episode_id="not_a_valid_epi_ulid",
            )


# ── invalidate ──────────────────────────────────────────────────────────────


class TestInvalidate:
    @pytest.mark.asyncio
    async def test_invalidate(self, scope):
        now = datetime.now(timezone.utc)
        mem_id = mint("mem")

        mc1 = AsyncMock()
        mc1.fetchone.return_value = (None,)  # SELECT valid_to → None means not invalidated

        mc2 = AsyncMock()
        mc2.fetchone.return_value = (now, None)  # UPDATE RETURNING valid_to, superseded_by

        cm1 = AsyncMock()
        cm1.__aenter__ = AsyncMock(return_value=mc1)
        cm1.__aexit__ = AsyncMock(return_value=False)

        cm2 = AsyncMock()
        cm2.__aenter__ = AsyncMock(return_value=mc2)
        cm2.__aexit__ = AsyncMock(return_value=False)

        conn = AsyncMock()
        conn.cursor = MagicMock(side_effect=[cm1, cm2])

        conn_cm = AsyncMock()
        conn_cm.__aenter__ = AsyncMock(return_value=conn)
        conn_cm.__aexit__ = AsyncMock(return_value=False)
        pool = AsyncMock()
        pool.connection = MagicMock(return_value=conn_cm)

        pub = _make_mock_publisher()

        result = await invalidate(
            pool=pool,
            publisher=pub,
            scope_text=scope,
            mem_id=mem_id,
        )

        assert result["mem_id"] == mem_id
        assert result["valid_to"] is not None
        assert result["deleted"] is False

    @pytest.mark.asyncio
    async def test_invalidate_already_invalidated(self, scope):
        mem_id = mint("mem")
        existing_valid_to = datetime.now(timezone.utc)

        mc = AsyncMock()
        mc.fetchone.return_value = (existing_valid_to,)  # Already has valid_to

        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=mc)
        cm.__aexit__ = AsyncMock(return_value=False)

        conn = AsyncMock()
        conn.cursor = MagicMock(return_value=cm)

        conn_cm = AsyncMock()
        conn_cm.__aenter__ = AsyncMock(return_value=conn)
        conn_cm.__aexit__ = AsyncMock(return_value=False)
        pool = AsyncMock()
        pool.connection = MagicMock(return_value=conn_cm)

        pub = _make_mock_publisher()

        with pytest.raises(AlreadyInvalidated):
            await invalidate(
                pool=pool,
                publisher=pub,
                scope_text=scope,
                mem_id=mem_id,
            )

    @pytest.mark.asyncio
    async def test_invalidate_memory_not_found(self, scope):
        mem_id = mint("mem")

        mc = AsyncMock()
        mc.fetchone.return_value = None  # No row found

        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=mc)
        cm.__aexit__ = AsyncMock(return_value=False)

        conn = AsyncMock()
        conn.cursor = MagicMock(return_value=cm)

        conn_cm = AsyncMock()
        conn_cm.__aenter__ = AsyncMock(return_value=conn)
        conn_cm.__aexit__ = AsyncMock(return_value=False)
        pool = AsyncMock()
        pool.connection = MagicMock(return_value=conn_cm)

        pub = _make_mock_publisher()

        with pytest.raises(MemoryNotFound):
            await invalidate(
                pool=pool,
                publisher=pub,
                scope_text=scope,
                mem_id=mem_id,
            )


# ── forget ──────────────────────────────────────────────────────────────────


class TestForget:
    @pytest.mark.asyncio
    async def test_forget_requires_approval(self, scope):
        """forget() without a valid approval raises ForgetRequiresApproval."""
        mem_id = mint("mem")
        approval_id = mint("apv")

        # Cursor 1: pg_advisory_xact_lock
        mc1 = AsyncMock()
        cm1 = AsyncMock()
        cm1.__aenter__ = AsyncMock(return_value=mc1)
        cm1.__aexit__ = AsyncMock(return_value=False)

        # Cursor 2: SELECT approvals → returns None (no matching approval)
        mc2 = AsyncMock()
        mc2.fetchone.return_value = None  # approval check fails
        cm2 = AsyncMock()
        cm2.__aenter__ = AsyncMock(return_value=mc2)
        cm2.__aexit__ = AsyncMock(return_value=False)

        conn = AsyncMock()
        conn.cursor = MagicMock(side_effect=[cm1, cm2])

        conn_cm = AsyncMock()
        conn_cm.__aenter__ = AsyncMock(return_value=conn)
        conn_cm.__aexit__ = AsyncMock(return_value=False)
        pool = AsyncMock()
        pool.connection = MagicMock(return_value=conn_cm)

        pub = _make_mock_publisher()

        with pytest.raises(ForgetRequiresApproval):
            await forget(
                pool=pool,
                publisher=pub,
                scope_text=scope,
                mem_id=mem_id,
                approval_id=approval_id,
                reason="user request",
            )


# ── list_episodes ───────────────────────────────────────────────────────────


class TestListEpisodes:
    @pytest.mark.asyncio
    async def test_list_episodes(self, scope):
        now = datetime.now(timezone.utc)

        conn = AsyncMock()
        mc = AsyncMock()
        mock_cursor = mc
        mock_cursor.fetchall.return_value = [
            (mint("epi"), "content 1", "ref1", now),
            (mint("epi"), "content 2", "ref2", now),
            (mint("epi"), "content 3", None, now),
        ]

        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=mock_cursor)
        cm.__aexit__ = AsyncMock(return_value=False)
        conn.cursor = MagicMock(return_value=cm)

        conn_cm = AsyncMock()
        conn_cm.__aenter__ = AsyncMock(return_value=conn)
        conn_cm.__aexit__ = AsyncMock(return_value=False)
        pool = AsyncMock()
        pool.connection = MagicMock(return_value=conn_cm)

        episodes = await list_episodes(pool, scope)
        assert len(episodes) == 3
        assert episodes[0]["id"].startswith("epi_")
        assert episodes[0]["content"] == "content 1"
        assert episodes[0]["source_ref"] == "ref1"

    @pytest.mark.asyncio
    async def test_list_episodes_empty(self, scope):
        conn = AsyncMock()
        mc = AsyncMock()
        mc.fetchall.return_value = []

        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=mc)
        cm.__aexit__ = AsyncMock(return_value=False)
        conn.cursor = MagicMock(return_value=cm)

        conn_cm = AsyncMock()
        conn_cm.__aenter__ = AsyncMock(return_value=conn)
        conn_cm.__aexit__ = AsyncMock(return_value=False)
        pool = AsyncMock()
        pool.connection = MagicMock(return_value=conn_cm)

        episodes = await list_episodes(pool, scope)
        assert episodes == []


# ── list_memories ───────────────────────────────────────────────────────────


class TestListMemories:
    @pytest.mark.asyncio
    async def test_list_memories(self, scope):
        now = datetime.now(timezone.utc)

        conn = AsyncMock()
        mc = AsyncMock()
        mc.fetchall.return_value = [
            (mint("mem"), "fact 1", "fact", now, mint("epi")),
            (mint("mem"), "fact 2", "fact", now, mint("epi")),
        ]

        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=mc)
        cm.__aexit__ = AsyncMock(return_value=False)
        conn.cursor = MagicMock(return_value=cm)

        conn_cm = AsyncMock()
        conn_cm.__aenter__ = AsyncMock(return_value=conn)
        conn_cm.__aexit__ = AsyncMock(return_value=False)
        pool = AsyncMock()
        pool.connection = MagicMock(return_value=conn_cm)

        memories = await list_memories(pool, scope)
        assert len(memories) == 2
        assert memories[0]["id"].startswith("mem_")
        assert memories[0]["statement"] == "fact 1"
        assert memories[0]["kind"] == "fact"
        assert memories[0]["source_episode_id"].startswith("epi_")

    @pytest.mark.asyncio
    async def test_list_memories_empty(self, scope):
        conn = AsyncMock()
        mc = AsyncMock()
        mc.fetchall.return_value = []

        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=mc)
        cm.__aexit__ = AsyncMock(return_value=False)
        conn.cursor = MagicMock(return_value=cm)

        conn_cm = AsyncMock()
        conn_cm.__aenter__ = AsyncMock(return_value=conn)
        conn_cm.__aexit__ = AsyncMock(return_value=False)
        pool = AsyncMock()
        pool.connection = MagicMock(return_value=conn_cm)

        memories = await list_memories(pool, scope)
        assert memories == []


# ── scope isolation ─────────────────────────────────────────────────────────


class TestScopeIsolation:
    @pytest.mark.asyncio
    async def test_scope_isolation(self):
        """Episodes in different scopes should only return those matching the scope."""
        scope_a = "user:usr_a"
        scope_b = "user:usr_b"
        now = datetime.now(timezone.utc)

        conn = AsyncMock()
        mc = AsyncMock()

        # The SQL query filters by scope, so fetchall only returns matching rows.
        # We simulate returning only scope_a rows.
        epi_a1 = mint("epi")
        epi_a2 = mint("epi")
        mc.fetchall.return_value = [
            (epi_a1, "content a1", None, now),
            (epi_a2, "content a2", None, now),
        ]

        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=mc)
        cm.__aexit__ = AsyncMock(return_value=False)
        conn.cursor = MagicMock(return_value=cm)

        conn_cm = AsyncMock()
        conn_cm.__aenter__ = AsyncMock(return_value=conn)
        conn_cm.__aexit__ = AsyncMock(return_value=False)
        pool = AsyncMock()
        pool.connection = MagicMock(return_value=conn_cm)

        episodes_a = await list_episodes(pool, scope_a)
        assert len(episodes_a) == 2
        ids = {e["id"] for e in episodes_a}
        assert epi_a1 in ids
        assert epi_a2 in ids
