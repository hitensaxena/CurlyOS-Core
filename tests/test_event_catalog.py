"""Closed event catalog — build_event() rejects unregistered types (P10/08 §4)."""
from __future__ import annotations

import pytest

from shared.events import build_event, full_type
from shared.events.catalog import (
    EVENT_CATALOG,
    FULL_TYPE_PREFIX,
    UnknownEventType,
    group_for,
    short_of,
    subject_for,
    validate_short_type,
)

_GROUPS = {"MEMORY", "AGENTS", "SAFETY", "EVENTS", "EVOLUTION"}


def test_catalog_entries_are_well_formed():
    for short, group in EVENT_CATALOG.items():
        assert group in _GROUPS, short
        assert 2 <= len(short.split(".")) <= 3, f"bad grammar: {short}"
        assert short == short.lower()


def test_every_live_emit_site_type_is_registered():
    # the types curlyos-core emitted before the catalog existed — removing any
    # of these from the catalog would crash a live write path.
    live = [
        "memory.episode.recorded", "memory.fact.stored", "memory.fact.consolidated",
        "memory.fact.invalidated", "identity.fact.updated",
        "knowledge.entity.created", "knowledge.entity.invalidated",
        "knowledge.edge.created", "knowledge.edge.invalidated",
        "metacog.assumption.created", "metacog.model.created",
        "studio.created", "studio.sketch.created", "studio.sketch.updated",
        "studio.sketch.invalidated", "studio.sketch.graduated", "studio.sketches.linked",
        "simulation.run.created", "simulation.run.completed", "simulation.run.forked",
    ]
    for t in live:
        assert t in EVENT_CATALOG, t


def test_build_event_accepts_registered_type():
    ev = build_event("memory.fact.stored", subject="mem_x",
                     scope={"level": "user", "user_id": "usr_x"}, data={})
    assert ev["type"] == FULL_TYPE_PREFIX + "memory.fact.stored"
    assert ev["id"].startswith("evt_")


def test_build_event_rejects_unregistered_type():
    with pytest.raises(UnknownEventType):
        build_event("totally.made.up", subject="x",
                    scope={"level": "user", "user_id": "usr_x"}, data={})


def test_helpers_round_trip():
    short = "safety.approval.requested"
    assert short_of(full_type(short)) == short
    assert subject_for(short) == "curlyos." + short
    assert group_for(short) == "SAFETY"
    assert group_for(full_type(short)) == "SAFETY"
    assert validate_short_type(short) == short
    with pytest.raises(UnknownEventType):
        group_for("nope.nope")
    with pytest.raises(UnknownEventType):
        short_of("com.example.other.type")
