"""Tests for the event system — CloudEvents envelope, type conversion, publisher."""

import pytest

from shared.events import build_event, full_type, short_type, EventPublisher
from shared.events.implementations import PgOnlyPublisher, _stored_scope


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

    @pytest.mark.asyncio
    async def test_emit_is_a_no_op_without_a_broker(self):
        await PgOnlyPublisher().emit("curlyos.memory.fact.stored", {"id": "evt_x"})

    def test_pg_only_publisher_is_event_publisher(self):
        pub = PgOnlyPublisher()
        assert isinstance(pub, EventPublisher)


class TestStoredScope:
    """`events.scope` must carry the full "level:id" string.

    A row stored as bare "user" never matches the scoped query behind /api/events,
    so it is written successfully and silently invisible. 34k rows were lost this
    way — mostly knowledge.edge.created.
    """

    def test_envelope_parts(self):
        assert _stored_scope({"level": "user", "user_id": "usr_hiten"}, {}) == "user:usr_hiten"

    def test_legacy_envelope_shape(self):
        assert _stored_scope({"scope": "user:usr_hiten"}, {}) == "user:usr_hiten"

    def test_bare_level_never_beats_a_full_string_in_data(self):
        assert _stored_scope({"level": "user"}, {"scope": "user:usr_hiten"}) == "user:usr_hiten"

    def test_bare_string_envelope(self):
        assert _stored_scope("user:usr_hiten", {}) == "user:usr_hiten"

    def test_no_scope_anywhere_falls_back_to_level(self):
        assert _stored_scope(None, {}) == "user"


class _ScopeCursor:
    def __init__(self, log):
        self.log = log

    async def execute(self, sql, params=None):
        self.log.append(params)

    async def fetchone(self):
        return (1,)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _ScopeConn:
    def __init__(self, log):
        self.log = log

    def cursor(self):
        return _ScopeCursor(self.log)


@pytest.mark.asyncio
async def test_stage_writes_the_full_scope_for_every_legacy_shape():
    shapes = [
        {"level": "user", "user_id": "usr_hiten"},
        {"scope": "user:usr_hiten"},
        "user:usr_hiten",
    ]
    for shape in shapes:
        log: list = []
        ev = build_event(
            short_type="identity.fact.updated",
            subject="identity_fact:idf_TEST",
            scope=shape,
            data={"idf_id": "idf_TEST"},
        )
        await PgOnlyPublisher().stage(ev, _ScopeConn(log))
        assert log[0][3] == "user:usr_hiten", shape
