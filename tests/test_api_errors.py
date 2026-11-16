"""Failures must be HTTP failures, not 200s carrying an "error" key.

Seven endpoints used to log the exception and return `{"error": ...}` with a 200
status. Anything that checks status — the webapp's fetch wrapper, a proxy, a
retry policy — could not tell a broken request from a successful one, and the
UI showed empty state where it should have shown an error.

The mapping: data-path failures (recall, ingest, consolidation) are 500;
engine triggers that depend on a model (reflection, meta, narrative, attention
scan, mood) are 503, so a client can say "needs a model" rather than "broken".
"""

from __future__ import annotations

import api_server
import pytest

@pytest.mark.asyncio
async def test_recall_failure_is_a_500(monkeypatch):
    """The endpoint raises HTTPException, not a 200 carrying {"error": ...}.

    Called directly rather than through TestClient: the route touches a pool,
    settings and redis before the failure point, and a test client brings its own
    machinery for those. Everything up to the embedder is stubbed.
    """
    import shared.settings as settings_mod
    from fastapi import HTTPException

    async def fake_pool(*args, **kwargs):
        return object()

    async def fake_setting(pool, key, default=None):
        return default

    async def boom():
        raise RuntimeError("embedder unavailable")

    monkeypatch.setattr(api_server, "_get_async_pool", fake_pool)
    monkeypatch.setattr(api_server, "_make_redis", lambda: None)
    monkeypatch.setattr(settings_mod, "get_setting_cached", fake_setting)
    monkeypatch.setattr(api_server, "get_shared_embedder", boom)

    body = api_server.RecallRequest(query="anything", k=3, mode="fast")
    with pytest.raises(HTTPException) as excinfo:
        await api_server.recall(body)
    assert excinfo.value.status_code == 500
    assert "embedder unavailable" in excinfo.value.detail


@pytest.mark.asyncio
async def test_narrative_failure_is_a_503(monkeypatch):
    """Engine triggers report 503, so a client can say "needs a model"."""
    import cognition.narrative as narrative
    from fastapi import HTTPException

    async def fake_pool(*args, **kwargs):
        return object()

    async def boom(**kwargs):
        raise RuntimeError("no model configured")

    monkeypatch.setattr(api_server, "_get_async_pool", fake_pool)
    monkeypatch.setattr(api_server, "_make_llm_client", lambda tier="fast": (None, ""))
    monkeypatch.setattr(narrative, "surface_themes", boom)

    body = api_server.NarrativeGenerateRequest()
    with pytest.raises(HTTPException) as excinfo:
        await api_server.narrative_generate(body)
    assert excinfo.value.status_code == 503
    assert "no model configured" in excinfo.value.detail
