"""Contract tests for the restored MCP integration and capture boundaries."""
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from mcp_server import server


@pytest.mark.asyncio
async def test_tool_surface():
    tools = await server.mcp.list_tools()
    assert {t.name for t in tools} == {
        "recall", "add_fact", "add_note", "invalidate", "identity", "list_goals",
        "list_opportunities", "list_projects", "list_runs", "get_run", "list_inbox",
        "list_approvals", "start_run", "resume_run", "cancel_run", "approve", "deny",
    }
    assert next(t for t in tools if t.name == "recall").annotations.readOnlyHint
    assert not next(t for t in tools if t.name == "approve").annotations.readOnlyHint


@pytest.mark.asyncio
async def test_recall_fallback_and_scope(monkeypatch):
    calls = []
    async def fake(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if method == "POST":
            raise httpx.ConnectError("offline")
        return {"items": [{"id": "mem_123", "statement": "grounded"}]}
    monkeypatch.setattr(server, "_api", fake)
    result = await server.recall("topic", k=100)
    assert result["fallback"] == "keyword"
    assert calls[0][2]["body"]["k"] == 20
    assert calls[1][2]["params"]["scope"] == server.SCOPE


@pytest.mark.asyncio
async def test_cached_recall_has_provenance_and_filters_invalidated(monkeypatch):
    async def fake(method, path, **kwargs):
        if path == "/api/recall":
            return {"results": [{"id": "active"}, {"id": "old"}], "cached": True}
        return {"memory": {"scope": server.SCOPE, "statement": "Full supported statement",
                           "source_episode_id": "epi_source", "valid_to": "2026-01-01" if path.endswith("old") else None}}
    monkeypatch.setattr(server, "_api", fake)
    result = await server.recall("topic")
    assert result["count"] == 1
    assert result["results"][0]["source_episode_id"] == "epi_source"
    assert result["results"][0]["text"] == "Full supported statement"


@pytest.mark.asyncio
async def test_project_filter_preserves_scope(monkeypatch):
    async def fake(*args, **kwargs):
        return {"items": [{"scope": server.SCOPE, "status": "active"},
                          {"scope": server.SCOPE, "status": "done"},
                          {"scope": "user:other", "status": "active"}]}
    monkeypatch.setattr(server, "_api", fake)
    result = await server.list_projects("active")
    assert result["items"] == [{"scope": server.SCOPE, "status": "active"}]


@pytest.mark.asyncio
async def test_ingest_errors_are_not_reported_as_success(monkeypatch):
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def request(self, *args, **kwargs):
            return httpx.Response(200, json={"error": "database write failed"}, request=httpx.Request("POST", "http://localhost"))
    monkeypatch.setattr(server.httpx, "AsyncClient", Client)
    with pytest.raises(ValueError, match="database write failed"):
        await server.add_fact("a real fact")


@pytest.mark.asyncio
async def test_approval_and_identifier_validation():
    with pytest.raises(ValueError):
        await server.approve("not-an-approval")
    with pytest.raises(ValueError):
        await server.get_run("../../api/safety/kill")


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
    assert server._clean({"items": [{"id": "x", "embedding": [1, 2]}]}) == {"items": [{"id": "x"}]}
