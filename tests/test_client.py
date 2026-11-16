"""Tests for the shared CurlyOS client contract."""

from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import patch

import pytest

from shared.client import (
    ClientError, SyncCurlyOSClient, clean, validate_id, validate_scope,
)


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_scope_validation():
    assert validate_scope("user:usr_hiten") == "user:usr_hiten"
    with pytest.raises(ValueError):
        validate_scope("")
    with pytest.raises(ValueError):
        validate_scope("no-separator")


def test_id_validation():
    assert validate_id("mem_01ABC") == "mem_01ABC"
    with pytest.raises(ValueError):
        validate_id("bad id!")
    with pytest.raises(ValueError):
        validate_id("epi_01ABC", prefix="mem_")


def test_clean_drops_embeddings():
    payload = {"id": "x", "embedding": [0.1], "vector": [0.2], "nested": {"embedding": [0.3], "ok": 1}}
    assert clean(payload) == {"id": "x", "nested": {"ok": 1}}


def test_request_success_and_cleaning():
    client = SyncCurlyOSClient(base_url="http://example", scope="user:test")
    with patch("shared.client.urllib.request.urlopen", return_value=_Response({"ok": True, "embedding": [1]})):
        assert client.request("GET", "/api/health") == {"ok": True}


def test_error_payload_is_an_error():
    client = SyncCurlyOSClient(base_url="http://example", scope="user:test")
    with patch("shared.client.urllib.request.urlopen", return_value=_Response({"error": "boom"})):
        with pytest.raises(ClientError):
            client.request("POST", "/api/recall", body={"query": "x"})


def test_http_error_carries_status():
    client = SyncCurlyOSClient(base_url="http://example", scope="user:test")
    error = urllib.error.HTTPError(
        "http://example/api/memories/x", 404, "Not Found", {},
        io.BytesIO(b'{"detail": "missing"}'),
    )
    with patch("shared.client.urllib.request.urlopen", side_effect=error):
        with pytest.raises(ClientError) as exc:
            client.request("GET", "/api/memories/x")
    assert exc.value.status == 404


def test_recall_falls_back_to_search():
    client = SyncCurlyOSClient(base_url="http://example", scope="user:test")
    with patch.object(client, "request", side_effect=[ClientError("down"), {"results": []}]) as req:
        assert client.recall("query", fallback=True) == {"results": []}
    assert req.call_args_list[0].kwargs["body"]["mode"] == "fast"
    assert req.call_args_list[1].args[0] == "GET"
    assert req.call_args_list[1].args[1] == "/api/search"


def test_invalidate_refuses_another_scope():
    client = SyncCurlyOSClient(base_url="http://example", scope="user:test")
    with patch.object(client, "get_memory", return_value={"memory": {"scope": "user:someone"}}):
        with pytest.raises(ClientError):
            client.invalidate("mem_01ABC")


def test_identity_encodes_predicates():
    client = SyncCurlyOSClient(base_url="http://example", scope="user:test")
    with patch.object(client, "request", return_value={"facts": []}) as req:
        client.identity(["name", "work"])
    assert req.call_args.kwargs["params"]["predicates"] == "name,work"
    assert req.call_args.kwargs["params"]["scope"] == "user:test"


def test_token_is_sent_when_configured(monkeypatch):
    """Half-arming is the failure this guards: a token on the server and a
    client that silently sends none looks like an outage."""
    monkeypatch.setenv("CURLYOS_API_TOKEN", "tok-123")
    client = SyncCurlyOSClient(base_url="http://example", scope="user:test")
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["headers"] = dict(request.header_items())
        return _Response({"ok": True})

    with patch("shared.client.urllib.request.urlopen", side_effect=fake_urlopen):
        client.request("GET", "/api/engines")
    assert captured["headers"].get("Authorization") == "Bearer tok-123"


def test_no_token_header_when_the_gate_is_dormant(monkeypatch):
    monkeypatch.delenv("CURLYOS_API_TOKEN", raising=False)
    client = SyncCurlyOSClient(base_url="http://example", scope="user:test")
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["headers"] = dict(request.header_items())
        return _Response({"ok": True})

    with patch("shared.client.urllib.request.urlopen", side_effect=fake_urlopen):
        client.request("GET", "/api/engines")
    assert "Authorization" not in captured["headers"]


def test_explicit_token_overrides_the_environment(monkeypatch):
    monkeypatch.setenv("CURLYOS_API_TOKEN", "env-token")
    client = SyncCurlyOSClient(base_url="http://example", scope="user:test",
                               token="explicit-token")
    assert client._auth_headers() == {"Authorization": "Bearer explicit-token"}
