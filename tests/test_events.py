"""Tests for the event system — CloudEvents envelope, type conversion, publisher."""

import pytest

from shared.events import build_event, full_type, short_type, EventPublisher
from shared.events.implementations import PgOnlyPublisher, PgNatsPublisher


FULL_PREFIX = "art.curlybrackets.curlyos."


class TestBuildEvent:
    def test_build_event(self):
        ev = build_event(
            short_type="memory.fact.stored",
            subject="mem_01TEST",
            scope={"level": "user", "user_id": "usr_test"},
            data={"mem_id": "mem_01TEST", "scope": "user:usr_test"},
        )
        assert ev["specversion"] == "1.0"
        assert ev["type"] == f"{FULL_PREFIX}memory.fact.stored"
        assert ev["subject"] == "mem_01TEST"
        assert ev["data"]["mem_id"] == "mem_01TEST"
        assert "id" in ev
        assert ev["id"].startswith("evt_")
        assert "time" in ev
        assert ev["actor"] == "system"
        assert ev["source"] == "curlyos-core"
        assert ev["scope"]["level"] == "user"

    def test_build_event_custom_actor_source(self):
        ev = build_event(
            short_type="identity.fact.updated",
            subject="identity_fact:idf_01TEST",
            scope={"level": "user", "user_id": "usr_test"},
            data={},
            actor="agent:curly",
            source="curlyos-core/identity",
        )
        assert ev["actor"] == "agent:curly"
        assert ev["source"] == "curlyos-core/identity"

    def test_build_event_type_prefix(self):
        """Event id must have evt_ prefix."""
        ev = build_event(
            short_type="memory.episode.recorded",
            subject="epi_01TEST",
            scope={"level": "user", "user_id": "usr_test"},
            data={},
        )
        assert ev["id"].startswith("evt_")


class TestFullType:
    def test_full_type(self):
        assert full_type("memory.fact.stored") == f"{FULL_PREFIX}memory.fact.stored"

    def test_full_type_nested(self):
        assert full_type("agent.run.completed") == f"{FULL_PREFIX}agent.run.completed"

    def test_short_type_strips_prefix(self):
        full = f"{FULL_PREFIX}memory.fact.stored"
        assert short_type(full) == "memory.fact.stored"

    def test_short_type_no_prefix_unchanged(self):
        assert short_type("memory.fact.stored") == "memory.fact.stored"

    def test_short_type_empty(self):
        assert short_type("") == ""

    def test_roundtrip(self):
        original = "identity.fact.updated"
        assert short_type(full_type(original)) == original


class TestPgOnlyPublisher:
    def test_pg_only_publisher_import(self):
        """PgOnlyPublisher is importable and instantiable."""
        pub = PgOnlyPublisher()
        assert pub is not None
        assert pub._nats is None

    def test_pg_only_publisher_is_event_publisher(self):
        pub = PgOnlyPublisher()
        assert isinstance(pub, EventPublisher)


class TestPgNatsPublisher:
    def test_pg_nats_publisher_import(self):
        """PgNatsPublisher is importable."""
        pub = PgNatsPublisher(nats_client=None)
        assert pub is not None

    def test_pg_nats_publisher_with_mock_nats(self):
        class FakeNats:
            pass

        pub = PgNatsPublisher(nats_client=FakeNats(), stream="CURLYOS_TEST")
        assert pub._nats is not None
        assert pub._stream == "CURLYOS_TEST"
