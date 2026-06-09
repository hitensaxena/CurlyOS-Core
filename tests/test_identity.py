"""Tests for identity engine — propose_identity_fact, get_identity_context, list_identity_facts."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from identity import propose_identity_fact, get_identity_context, list_identity_facts
from shared.types.ulid import mint


def _make_mock_pool(scope_text, existing_rows=None):
    """Create a mock async pool with cursor/context-manager support.

    propose_identity_fact calls conn.cursor() multiple times:
      1. SELECT existing facts
      2. INSERT new fact (RETURNING valid_from, ingested_at)
      3. (optional) UPDATE old fact if superseding
    """
    if existing_rows is None:
        existing_rows = []

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)

    conn = AsyncMock()

    # Build cursor context managers in order
    cursor_cms = []

    # Cursor 1: SELECT existing identity facts
    mc1 = AsyncMock()
    mc1.fetchone.side_effect = list(existing_rows) + [None] * 10
    cm1 = AsyncMock()
    cm1.__aenter__ = AsyncMock(return_value=mc1)
    cm1.__aexit__ = AsyncMock(return_value=False)
    cursor_cms.append(cm1)

    # Cursor 2: INSERT identity_facts RETURNING valid_from, ingested_at
    mc2 = AsyncMock()
    mc2.fetchone.return_value = (now, now)
    cm2 = AsyncMock()
    cm2.__aenter__ = AsyncMock(return_value=mc2)
    cm2.__aexit__ = AsyncMock(return_value=False)
    cursor_cms.append(cm2)

    # Cursor 3: UPDATE identity_facts (invalidate old) — optional
    mc3 = AsyncMock()
    mc3.fetchone.return_value = (now,)
    cm3 = AsyncMock()
    cm3.__aenter__ = AsyncMock(return_value=mc3)
    cm3.__aexit__ = AsyncMock(return_value=False)
    cursor_cms.append(cm3)

    # Extra cursors as fallback
    for _ in range(10):
        mc_extra = AsyncMock()
        mc_extra.fetchone.return_value = None
        mc_extra.fetchall.return_value = []
        cm_extra = AsyncMock()
        cm_extra.__aenter__ = AsyncMock(return_value=mc_extra)
        cm_extra.__aexit__ = AsyncMock(return_value=False)
        cursor_cms.append(cm_extra)

    cursor_iter = iter(cursor_cms)

    def cursor_factory():
        try:
            return next(cursor_iter)
        except StopIteration:
            cm = AsyncMock()
            cm.__aenter__ = AsyncMock(return_value=AsyncMock())
            cm.__aexit__ = AsyncMock(return_value=False)
            return cm

    conn.cursor = cursor_factory

    # connection() as async context manager
    conn_cm = AsyncMock()
    conn_cm.__aenter__ = AsyncMock(return_value=conn)
    conn_cm.__aexit__ = AsyncMock(return_value=False)

    pool = AsyncMock()
    pool.connection = MagicMock(return_value=conn_cm)

    return pool, cursor_cms[0]


@pytest.fixture
def epi_id():
    return mint("epi")


@pytest.fixture
def mock_publisher():
    pub = AsyncMock()
    pub.stage = AsyncMock(return_value=("evt_01TEST", "curlyos.fact", {}))
    return pub


class TestProposeIdentityFact:
    @pytest.mark.asyncio
    async def test_propose_identity_fact_basic(self, scope, epi_id, mock_publisher):
        pool, _ = _make_mock_pool(scope)
        result = await propose_identity_fact(
            pool=pool,
            publisher=mock_publisher,
            scope_text=scope,
            predicate="name",
            object="Alice",
            confidence=0.95,
            source_episode_id=epi_id,
        )
        assert "idf_id" in result
        assert result["idf_id"].startswith("idf_")
        assert result["predicate"] == "name"
        assert result["object"] == "Alice"
        assert result["confidence"] == 0.95
        assert result["action_taken"] == "inserted"

    @pytest.mark.asyncio
    async def test_propose_auto_promote_high_confidence(self, scope, epi_id, mock_publisher):
        pool, _ = _make_mock_pool(scope)
        result = await propose_identity_fact(
            pool=pool,
            publisher=mock_publisher,
            scope_text=scope,
            predicate="lang",
            object="Python",
            confidence=0.90,
            source_episode_id=epi_id,
        )
        assert result["epistemic_status"] == "canonical"

    @pytest.mark.asyncio
    async def test_propose_hypothesis_low_confidence(self, scope, epi_id, mock_publisher):
        pool, _ = _make_mock_pool(scope)
        result = await propose_identity_fact(
            pool=pool,
            publisher=mock_publisher,
            scope_text=scope,
            predicate="interest",
            object="maybe chess",
            confidence=0.5,
            source_episode_id=epi_id,
        )
        assert result["epistemic_status"] == "hypothesis"

    @pytest.mark.asyncio
    async def test_propose_invalid_source_episode(self, scope, mock_publisher):
        pool, _ = _make_mock_pool(scope)
        with pytest.raises(ValueError, match="Invalid source_episode_id"):
            await propose_identity_fact(
                pool=pool,
                publisher=mock_publisher,
                scope_text=scope,
                predicate="x",
                object="y",
                confidence=0.8,
                source_episode_id="not_a_valid_epi_id",
            )


class TestProposeConflict:
    @pytest.mark.asyncio
    async def test_propose_conflict_higher_confidence(self, scope, epi_id, mock_publisher):
        existing_id = mint("idf")
        existing_row = (existing_id, 0.70, "OldValue")
        pool, cursor = _make_mock_pool(scope, existing_rows=[existing_row])

        # Add an extra fetchone for the INSERT RETURNING
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        cursor.fetchone.side_effect = [existing_row, (now, now), None, None]

        result = await propose_identity_fact(
            pool=pool,
            publisher=mock_publisher,
            scope_text=scope,
            predicate="name",
            object="NewValue",
            confidence=0.95,
            source_episode_id=epi_id,
        )
        assert result["action_taken"] == "superseded"

    @pytest.mark.asyncio
    async def test_propose_conflict_lower_confidence(self, scope, epi_id, mock_publisher):
        existing_id = mint("idf")
        existing_row = (existing_id, 0.90, "ExistingValue")
        pool, cursor = _make_mock_pool(scope, existing_rows=[existing_row])

        # Next fetchone after the existing row — return None for INSERT path
        cursor.fetchone.side_effect = [existing_row, None, None]

        result = await propose_identity_fact(
            pool=pool,
            publisher=mock_publisher,
            scope_text=scope,
            predicate="name",
            object="WorseValue",
            confidence=0.60,
            source_episode_id=epi_id,
        )
        assert result["action_taken"] == "no_change"
        assert result["idf_id"] == existing_id
        assert result["object"] == "ExistingValue"


class TestGetIdentityContext:
    @pytest.mark.asyncio
    async def test_get_identity_context(self, scope):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)

        conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall.return_value = [
            (mint("idf"), "name", "Alice", 0.95, now, "canonical"),
            (mint("idf"), "lang", "Python", 0.85, now, "canonical"),
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

        ctx = await get_identity_context(pool, scope)
        assert "name" in ctx
        assert "lang" in ctx
        assert ctx["name"]["object"] == "Alice"
        assert ctx["name"]["confidence"] == 0.95
        assert ctx["name"]["epistemic_status"] == "canonical"

    @pytest.mark.asyncio
    async def test_get_identity_context_with_predicates(self, scope):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)

        conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall.return_value = [
            (mint("idf"), "name", "Bob", 0.8, now, "canonical"),
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

        ctx = await get_identity_context(pool, scope, predicates=["name", "lang"])
        assert "name" in ctx
        assert ctx["name"]["object"] == "Bob"


class TestListIdentityFacts:
    @pytest.mark.asyncio
    async def test_list_identity_facts(self, scope):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)

        conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall.return_value = [
            (mint("idf"), scope, "name", "Alice", 0.95, "canonical", now, None, None, mint("epi")),
            (mint("idf"), scope, "lang", "Python", 0.80, "canonical", now, None, None, mint("epi")),
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

        facts = await list_identity_facts(pool, scope)
        assert len(facts) == 2
        assert facts[0]["predicate"] == "name"
        assert facts[0]["object"] == "Alice"
        assert facts[0]["idf_id"].startswith("idf_")

    @pytest.mark.asyncio
    async def test_list_identity_facts_empty(self, scope):
        conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall.return_value = []

        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=mock_cursor)
        cm.__aexit__ = AsyncMock(return_value=False)
        conn.cursor = MagicMock(return_value=cm)

        conn_cm = AsyncMock()
        conn_cm.__aenter__ = AsyncMock(return_value=conn)
        conn_cm.__aexit__ = AsyncMock(return_value=False)
        pool = AsyncMock()
        pool.connection = MagicMock(return_value=conn_cm)

        facts = await list_identity_facts(pool, scope)
        assert facts == []
