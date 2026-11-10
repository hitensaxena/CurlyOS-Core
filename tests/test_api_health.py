"""The health check must describe the embedder it actually has.

`/api/health` used to import a symbol that does not exist and turn the
ImportError into `ok: True`, so the embedder could be a FakeEmbedder returning
zero vectors while every dashboard showed green.
"""
from __future__ import annotations

import api_server
from shared.embeddings.implementations import FakeEmbedder


def test_no_embedder_loaded_is_not_healthy():
    api_server._EMBEDDER = None
    assert api_server._embedder_health()["ok"] is False


def test_fake_embedder_is_never_reported_healthy():
    api_server._EMBEDDER = FakeEmbedder()
    health = api_server._embedder_health()
    assert health["ok"] is False
    assert "fake" in health["model"]


def test_real_embedder_reports_its_model_name():
    class Real:
        model_name = "bge-m3"

    api_server._EMBEDDER = Real()
    assert api_server._embedder_health() == {"ok": True, "model": "bge-m3"}
