"""The API token gate.

Dormant by default: with CURLYOS_API_TOKEN unset the server behaves exactly as
before, because the API binds 127.0.0.1 and nothing can reach it off-box. When
the variable is set, /api/* requires a bearer token and /api/health stays open
so a supervisor can probe the process before any credential exists.

The danger this guards against is a half-armed deployment: a token on the
server and a client that silently sends none. These tests pin both directions.
"""

from __future__ import annotations

import api_server
import pytest
from fastapi.testclient import TestClient

TOKEN = "test-token-0001"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("CURLYOS_API_TOKEN", raising=False)
    # No lifespan: these tests exercise the middleware, not startup wiring.
    # Route-level failures (no database in this process) become 500s rather than
    # escaping, so each assertion is purely about the gate: 401 or not.
    return TestClient(api_server.app, raise_server_exceptions=False)


def test_dormant_gate_allows_everything(client):
    assert client.get("/api/health").status_code == 200
    # /api/engines reads the in-process registry: no database, so the only
    # thing that can make it fail here is the gate itself.
    assert client.get("/api/engines").status_code == 200


def test_armed_gate_rejects_a_missing_token(client, monkeypatch):
    monkeypatch.setenv("CURLYOS_API_TOKEN", TOKEN)
    assert client.get("/api/engines").status_code == 401
    assert client.get("/api/recall").status_code == 401


def test_armed_gate_rejects_a_wrong_token(client, monkeypatch):
    monkeypatch.setenv("CURLYOS_API_TOKEN", TOKEN)
    response = client.get(
        "/api/engines", headers={"Authorization": "Bearer wrong-token"})
    assert response.status_code == 401


def test_armed_gate_accepts_the_token(client, monkeypatch):
    monkeypatch.setenv("CURLYOS_API_TOKEN", TOKEN)
    response = client.get(
        "/api/engines", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200


def test_health_stays_open_when_armed(client, monkeypatch):
    monkeypatch.setenv("CURLYOS_API_TOKEN", TOKEN)
    assert client.get("/api/health").status_code == 200


def test_non_api_paths_are_not_gated(client, monkeypatch):
    monkeypatch.setenv("CURLYOS_API_TOKEN", TOKEN)
    assert client.get("/openapi.json").status_code != 401
