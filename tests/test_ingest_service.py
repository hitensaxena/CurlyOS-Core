"""Tests for the ingest service's scaffolding filter."""

from __future__ import annotations

from memory.ingest_service import strip_scaffolding


def test_plain_text_is_unchanged():
    assert strip_scaffolding("  a normal note  ") == "a normal note"


def test_scaffolding_blocks_are_removed():
    text = (
        "<system-reminder>internal reasoning</system-reminder>\n"
        "the actual note\n"
        "<command-name>/model</command-name>"
    )
    assert strip_scaffolding(text) == "the actual note"


def test_pure_scaffolding_is_empty():
    assert strip_scaffolding("<task-notification>done</task-notification>") == ""
    assert strip_scaffolding("") == ""
