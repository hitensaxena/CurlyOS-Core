"""Tests for CurlyOS Core type models, ULID minting/validation, and enums."""

import re
from datetime import datetime, timezone

import pytest

from shared.types import (
    Episode,
    EpistemicStatus,
    IdentityFact,
    Memory,
    MemoryKind,
    RetrievalRequest,
    RetrievedItem,
    RetrievalResult,
    ScopeLevel,
)
from shared.types.ulid import mint, is_valid, PREFIXES, ALL_PREFIXES


# ── Episode ──────────────────────────────────────────────────────────────────


class TestEpisodeModel:
    def test_episode_model(self):
        now = datetime.now(timezone.utc)
        ep = Episode(
            id=mint("epi"),
            scope="user:usr_test",
            content="test content",
            source_ref="ref1",
            modality="text",
            ingested_at=now,
            created_at=now,
        )
        assert ep.scope == "user:usr_test"
        assert ep.content == "test content"
        assert ep.source_ref == "ref1"
        assert ep.modality == "text"

    def test_episode_default_modality(self):
        now = datetime.now(timezone.utc)
        ep = Episode(
            id=mint("epi"),
            scope="user:usr_test",
            content="hello",
            ingested_at=now,
            created_at=now,
        )
        assert ep.modality == "text"

    def test_episode_none_source_ref(self):
        now = datetime.now(timezone.utc)
        ep = Episode(
            id=mint("epi"),
            scope="user:usr_test",
            content="no source",
            source_ref=None,
            ingested_at=now,
            created_at=now,
        )
        assert ep.source_ref is None


# ── Memory ───────────────────────────────────────────────────────────────────


class TestMemoryModel:
    def test_memory_model(self):
        now = datetime.now(timezone.utc)
        mem = Memory(
            id=mint("mem"),
            scope="user:usr_test",
            statement="test fact",
            kind=MemoryKind.FACT,
            epistemic_status=EpistemicStatus.CANONICAL,
            valid_from=now,
            valid_to=None,
            ingested_at=now,
            created_at=now,
            source_episode_id=mint("epi"),
        )
        assert mem.statement == "test fact"
        assert mem.kind == MemoryKind.FACT
        assert mem.epistemic_status == EpistemicStatus.CANONICAL
        assert mem.valid_to is None

    def test_memory_default_kind(self):
        now = datetime.now(timezone.utc)
        mem = Memory(
            id=mint("mem"),
            scope="user:usr_test",
            statement="another fact",
            valid_from=now,
            ingested_at=now,
            created_at=now,
            source_episode_id=mint("epi"),
        )
        assert mem.kind == MemoryKind.FACT

    def test_memory_procedure_kind(self):
        now = datetime.now(timezone.utc)
        mem = Memory(
            id=mint("mem"),
            scope="user:usr_test",
            statement="how to do X",
            kind=MemoryKind.PROCEDURE,
            valid_from=now,
            ingested_at=now,
            created_at=now,
            source_episode_id=mint("epi"),
        )
        assert mem.kind == MemoryKind.PROCEDURE


# ── IdentityFact ─────────────────────────────────────────────────────────────


class TestIdentityFactModel:
    def test_identity_fact_model(self):
        now = datetime.now(timezone.utc)
        idf = IdentityFact(
            id=mint("idf"),
            scope="user:usr_test",
            predicate="name",
            object="Alice",
            confidence=0.95,
            epistemic_status=EpistemicStatus.CANONICAL,
            valid_from=now,
            valid_to=None,
            ingested_at=now,
            created_at=now,
            source_episode_id=mint("epi"),
        )
        assert idf.predicate == "name"
        assert idf.object == "Alice"
        assert idf.confidence == 0.95
        assert idf.valid_to is None

    def test_identity_fact_default_scope(self):
        now = datetime.now(timezone.utc)
        idf = IdentityFact(
            id=mint("idf"),
            predicate="lang",
            object="Python",
            confidence=0.8,
            valid_from=now,
            ingested_at=now,
            created_at=now,
            source_episode_id=mint("epi"),
        )
        assert idf.scope == "user"

    def test_identity_fact_confidence_bounds(self):
        now = datetime.now(timezone.utc)
        # confidence = 0.0 should be valid
        idf = IdentityFact(
            id=mint("idf"),
            predicate="x",
            object="y",
            confidence=0.0,
            valid_from=now,
            ingested_at=now,
            created_at=now,
            source_episode_id=mint("epi"),
        )
        assert idf.confidence == 0.0

    def test_identity_fact_confidence_one(self):
        now = datetime.now(timezone.utc)
        idf = IdentityFact(
            id=mint("idf"),
            predicate="x",
            object="y",
            confidence=1.0,
            valid_from=now,
            ingested_at=now,
            created_at=now,
            source_episode_id=mint("epi"),
        )
        assert idf.confidence == 1.0


# ── RetrievalRequest ─────────────────────────────────────────────────────────


class TestRetrievalRequest:
    def test_retrieval_request(self):
        req = RetrievalRequest(
            query="What is CurlyOS?",
            scope="user:usr_test",
        )
        assert req.query == "What is CurlyOS?"
        assert req.scope == "user:usr_test"
        assert req.token_budget == 4000
        assert req.mode == "fast"
        assert req.max_rounds == 3

    def test_retrieval_request_custom(self):
        req = RetrievalRequest(
            query="deep query",
            scope="session:ses_abc",
            token_budget=8000,
            mode="deep",
            max_rounds=5,
        )
        assert req.token_budget == 8000
        assert req.mode == "deep"
        assert req.max_rounds == 5

    def test_retrieval_request_tiers(self):
        req = RetrievalRequest(
            query="test",
            scope="user:usr_test",
            tiers={"semantic", "episodic"},
        )
        assert "semantic" in req.tiers
        assert "episodic" in req.tiers


# ── RetrievedItem ────────────────────────────────────────────────────────────


class TestRetrievedItem:
    def test_retrieved_item_model(self):
        now = datetime.now(timezone.utc)
        item = RetrievedItem(
            id=mint("mem"),
            tier="semantic",
            text="a fact",
            score=0.95,
            valid_from=now,
            valid_to=None,
            source_episode_id=mint("epi"),
            signals={"dense": 0.95},
        )
        assert item.id.startswith("mem_")
        assert item.tier == "semantic"
        assert item.score == 0.95
        assert item.epistemic_status == "canonical"
        assert item.simulated is False

    def test_retrieved_item_with_signals(self):
        now = datetime.now(timezone.utc)
        item = RetrievedItem(
            id=mint("mem"),
            tier="graph",
            text="connected fact",
            score=0.7,
            valid_from=now,
            valid_to=None,
            source_episode_id=mint("epi"),
            signals={"dense": 0.5, "bm25": 0.8, "graph": 0.7},
        )
        assert item.signals["bm25"] == 0.8


# ── RetrievalResult ──────────────────────────────────────────────────────────


class TestRetrievalResult:
    def test_retrieval_result(self):
        now = datetime.now(timezone.utc)
        items = [
            RetrievedItem(
                id=mint("mem"),
                tier="semantic",
                text="fact 1",
                score=0.9,
                valid_from=now,
                valid_to=None,
                source_episode_id=mint("epi"),
                signals={},
            ),
        ]
        result = RetrievalResult(
            items=items,
            used_tokens=100,
            rounds=1,
            truncated=False,
            cache_key="abc123",
        )
        assert len(result.items) == 1
        assert result.used_tokens == 100
        assert result.rounds == 1
        assert result.truncated is False
        assert result.reranked is True
        assert result.graph_skipped is False


# ── ULID mint + validate ─────────────────────────────────────────────────────


class TestUlid:
    def test_ulid_mint_epi(self):
        uid = mint("epi")
        assert uid.startswith("epi_")
        assert len(uid) == 30  # "epi_" (4) + 26
        assert is_valid("epi", uid)

    def test_ulid_mint_mem(self):
        uid = mint("mem")
        assert uid.startswith("mem_")
        assert is_valid("mem", uid)

    def test_ulid_mint_idf(self):
        uid = mint("idf")
        assert uid.startswith("idf_")
        assert is_valid("idf", uid)

    def test_ulid_mint_evt(self):
        uid = mint("evt")
        assert uid.startswith("evt_")
        assert is_valid("evt", uid)

    def test_ulid_mint_all_registered_prefixes(self):
        """Every prefix in PREFIXES should be mintable."""
        for prefix_val in ALL_PREFIXES:
            uid = mint(prefix_val)
            assert uid.startswith(f"{prefix_val}_")
            assert is_valid(prefix_val, uid)

    def test_ulid_mint_unknown_prefix(self):
        with pytest.raises(ValueError, match="Unknown prefix"):
            mint("zzz")

    def test_ulid_validate_good(self):
        good = mint("epi")
        assert is_valid("epi", good) is True

    def test_ulid_validate_wrong_prefix(self):
        uid = mint("epi")
        assert is_valid("mem", uid) is False

    def test_ulid_validate_bad_format(self):
        assert is_valid("epi", "not_a_ulid") is False
        assert is_valid("epi", "epi_short") is False
        assert is_valid("epi", "") is False

    def test_ulid_validate_wrong_body_length(self):
        # 25 chars instead of 26
        assert is_valid("epi", "epi_0123456789ABCDEFGHJKMNPQ") is False

    def test_ulid_uniqueness(self):
        ids = {mint("epi") for _ in range(100)}
        assert len(ids) == 100


# ── EpistemicStatus enum ─────────────────────────────────────────────────────


class TestEpistemicStatus:
    def test_epistemic_status_enum(self):
        assert EpistemicStatus.SEED == "seed"
        assert EpistemicStatus.CONJECTURE == "conjecture"
        assert EpistemicStatus.POSSIBLE_WORLD == "possible_world"
        assert EpistemicStatus.HYPOTHESIS == "hypothesis"
        assert EpistemicStatus.BELIEF == "belief"
        assert EpistemicStatus.CANONICAL == "canonical"

    def test_epistemic_status_values(self):
        values = {s.value for s in EpistemicStatus}
        assert values == {
            "seed",
            "conjecture",
            "possible_world",
            "hypothesis",
            "belief",
            "canonical",
        }


# ── ScopeLevel enum ──────────────────────────────────────────────────────────


class TestScopeLevel:
    def test_scope_level_enum(self):
        assert ScopeLevel.USER == "user"
        assert ScopeLevel.SESSION == "session"
        assert ScopeLevel.AGENT == "agent"
        assert ScopeLevel.WORKSPACE == "workspace"
        assert ScopeLevel.STUDIO == "studio"
        assert ScopeLevel.SCENARIO == "scenario"

    def test_scope_level_values(self):
        values = {s.value for s in ScopeLevel}
        assert values == {
            "user",
            "session",
            "agent",
            "workspace",
            "studio",
            "scenario",
        }
