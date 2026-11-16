"""Contract tests for the restored MCP integration and capture boundaries."""
import importlib.util
import json
from pathlib import Path

import pytest

from mcp_server import server
from shared.client import ClientError, clean


@pytest.mark.asyncio
async def test_tool_surface():
    tools = await server.mcp.list_tools()
    assert {t.name for t in tools} == {
        "recall", "add_fact", "add_note", "invalidate", "identity",
        "list_goals", "list_opportunities",
    }
    assert next(t for t in tools if t.name == "recall").annotations.readOnlyHint
    assert not next(t for t in tools if t.name == "add_fact").annotations.readOnlyHint


@pytest.mark.asyncio
async def test_recall_fallback_and_scope(monkeypatch):
    calls = []

    async def fake_recall(query, k=6, mode="fast", *, fallback=False):
        calls.append((k, fallback))
        if not fallback:
            raise ClientError("offline")
        return {"items": [{"id": "mem_123", "statement": "grounded"}]}

    monkeypatch.setattr(server.CLIENT, "recall", fake_recall)
    result = await server.recall("topic", k=100)
    assert result["fallback"] == "keyword"
    assert calls[0] == (20, False)   # k is clamped before the call
    assert calls[1] == (20, True)


@pytest.mark.asyncio
async def test_cached_recall_has_provenance_and_filters_invalidated(monkeypatch):
    async def fake_recall(query, k=6, mode="fast", *, fallback=False):
        return {"results": [{"id": "active"}, {"id": "old"}], "cached": True}

    async def fake_get(mem_id):
        return {"memory": {
            "scope": server.CLIENT.scope,
            "statement": "Full supported statement",
            "source_episode_id": "epi_source",
            "valid_to": "2026-01-01" if mem_id == "old" else None,
        }}

    monkeypatch.setattr(server.CLIENT, "recall", fake_recall)
    monkeypatch.setattr(server.CLIENT, "get_memory", fake_get)
    result = await server.recall("topic")
    assert result["count"] == 1
    assert result["results"][0]["source_episode_id"] == "epi_source"
    assert result["results"][0]["text"] == "Full supported statement"


@pytest.mark.asyncio
async def test_ingest_errors_are_not_reported_as_success(monkeypatch):
    async def fake_request(*args, **kwargs):
        raise ClientError("database write failed")

    monkeypatch.setattr(server.CLIENT, "request", fake_request)
    with pytest.raises(ClientError, match="database write failed"):
        await server.add_fact("a real fact")


def test_capture_excludes_system_tools_and_analysis(tmp_path):
    spec = importlib.util.spec_from_file_location("curlyos_hook_test", Path(__file__).parents[1] / "integrations/codex_hook.py")
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    path = tmp_path / "transcript.jsonl"
    def item(role, text, phase=None):
        return {"type": "response_item", "payload": {"type": "message", "role": role, "phase": phase, "content": [{"type": "input_text", "text": text}]}}
    rows = [item("developer", "private instructions"), item("user", "Useful personal context"),
            item("assistant", "hidden reasoning", "analysis"), item("assistant", "A supported answer", "final_answer"),
            {"type": "response_item", "payload": {"type": "function_call_output", "output": "credentials"}}]
    path.write_text("\n".join(json.dumps(row) for row in rows))
    assert hook.messages(path) == [("user", "Useful personal context"), ("assistant", "A supported answer")]
    assert "sensitive-secret" not in hook.redact("api_key=sensitive-secret")


def test_clean_vectors():
    assert clean({"items": [{"id": "x", "embedding": [1, 2]}]}) == {"items": [{"id": "x"}]}
