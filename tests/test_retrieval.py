"""Tests for memory retrieval engine — import, models, basic structure."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from memory.retrieval import retrieve
from shared.types import RetrievalRequest, RetrievedItem, RetrievalResult


class TestRetrieveImport:
    def test_retrieve_import(self):
        """retrieve function is importable from memory.retrieval."""
        assert callable(retrieve)


class TestRetrievalRequestModel:
    def test_retrieve_request_model(self):
        req = RetrievalRequest(
            query="What is CurlyOS?",
            scope="user:usr_test",
        )
        assert req.query == "What is CurlyOS?"
        assert req.scope == "user:usr_test"

    def test_retrieve_request_defaults(self):
        req = RetrievalRequest(query="q", scope="s")
        assert req.token_budget == 4000
        assert req.mode == "fast"
        assert req.max_rounds == 3
        assert "semantic" in req.tiers

    def test_retrieve_request_modes(self):
        for mode in ("fast", "deep", "divergent"):
            req = RetrievalRequest(query="q", scope="s", mode=mode)
            assert req.mode == mode


class TestRetrievedItemModel:
    def test_retrieved_item_model(self):
        now = datetime.now(timezone.utc)
        item = RetrievedItem(
            id="mem_01TEST",
            tier="semantic",
            text="a fact",
            score=0.95,
            valid_from=now,
            valid_to=None,
            source_episode_id="epi_01TEST",
            signals={"dense": 0.95},
        )
        assert item.id == "mem_01TEST"
        assert item.tier == "semantic"
        assert item.score == 0.95
        assert item.epistemic_status == "canonical"
        assert item.simulated is False

    def test_retrieved_item_signals(self):
        now = datetime.now(timezone.utc)
        item = RetrievedItem(
            id="mem_02",
            tier="graph",
            text="connected",
            score=0.7,
            valid_from=now,
            valid_to=None,
            source_episode_id="epi_01",
            signals={"dense": 0.5, "graph": 0.7},
        )
        assert item.signals["graph"] == 0.7


class TestRetrievalResultModel:
    def test_retrieval_result(self):
        now = datetime.now(timezone.utc)
        items = [
            RetrievedItem(
                id="mem_01",
                tier="semantic",
                text="fact",
                score=0.9,
                valid_from=now,
                valid_to=None,
                source_episode_id="epi_01",
                signals={},
            ),
        ]
        result = RetrievalResult(
            items=items,
            used_tokens=100,
            rounds=1,
            truncated=False,
            cache_key="key123",
        )
        assert len(result.items) == 1
        assert result.used_tokens == 100
        assert result.truncated is False


class TestRetrieveAsync:
    @pytest.mark.asyncio
    async def test_retrieve_no_candidates(self):
        """retrieve() with None embedder/reranker returns empty result."""
        request = RetrievalRequest(
            query="test query",
            scope="user:usr_test",
        )

        pool = AsyncMock()
        conn = AsyncMock()
        cursor = AsyncMock()
        cursor.fetchall.return_value = []

        cm = AsyncMock()
        cm.__aenter__ = AsyncMock(return_value=cursor)
        cm.__aexit__ = AsyncMock(return_value=False)
        conn.cursor = lambda: cm

        conn_cm = AsyncMock()
        conn_cm.__aenter__ = AsyncMock(return_value=conn)
        conn_cm.__aexit__ = AsyncMock(return_value=False)
        pool.connection = lambda: conn_cm

        embedder = AsyncMock()
        embedder.embed_single = AsyncMock(return_value=[0.1] * 384)

        result = await retrieve(
            request=request,
            pool=pool,
            embedder=embedder,
            reranker=None,
            redis=None,
        )
        assert isinstance(result, RetrievalResult)
        assert result.items == []
