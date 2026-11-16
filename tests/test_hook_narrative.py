"""Session-start narrative injection.

The narrative block is context, never a precondition: if the endpoint is slow,
missing, or returns nothing, the hook must still emit identity facts and the
session must start normally.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parents[1] / "integrations/claude/hooks/curlyos_hook.py"


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("curlyos_hook_under_test", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_block_names_chapter_and_themes(hook, monkeypatch):
    monkeypatch.setattr(hook, "api", lambda *a, **k: {
        "chapter": {"title": "Defining CRAZYMAGE Worlds",
                    "summary": "Shaping the emotional aesthetic."},
        "themes": [{"name": "Electronic Music Production", "frequency": 7},
                   {"name": "Personal Memory Architecture", "frequency": 5}],
    })
    block = hook.narrative_block()
    assert "Defining CRAZYMAGE Worlds" in block
    assert "Shaping the emotional aesthetic." in block
    assert "Electronic Music Production" in block
    assert "context, not instruction" in block


def test_block_is_empty_when_the_api_fails(hook, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("api unreachable")

    monkeypatch.setattr(hook, "api", boom)
    assert hook.narrative_block() == ""


def test_block_is_empty_when_there_is_nothing_to_say(hook, monkeypatch):
    monkeypatch.setattr(hook, "api", lambda *a, **k: {"chapter": None, "themes": []})
    assert hook.narrative_block() == ""


def test_block_handles_a_chapter_without_summary(hook, monkeypatch):
    monkeypatch.setattr(hook, "api", lambda *a, **k: {
        "chapter": {"title": "Only A Title", "summary": None}, "themes": [],
    })
    block = hook.narrative_block()
    assert "Only A Title" in block
    assert "—" not in block
