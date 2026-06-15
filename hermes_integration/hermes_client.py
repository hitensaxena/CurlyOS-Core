"""Thin async client to the Hermes agent's OpenAI-compatible API (:8642).

Hermes is NOT a discrete-tool API — it's an AGENTIC endpoint: you send it a task
and its own agent loop autonomously uses its full toolset (web search, browser,
image generation, terminal, skills) and returns the final result. So CurlyOS
"using Hermes' tools" means DELEGATING a sub-task to Hermes.

This client is the single transport. It is used by:
  * the CurlyOS worker tools (web_research / browse / generate_image / delegate),
  * the Hermes MCP adapter (which re-exposes the same delegation over MCP).

Key + base URL resolution (first hit wins):
  HERMES_API_URL / HERMES_API_KEY env  →  ~/.hermes/config.yaml (API_SERVER_*)
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

import httpx

log = logging.getLogger("curlyos-core.hermes.client")

DEFAULT_BASE = "http://127.0.0.1:8642"
DEFAULT_TIMEOUT = 180.0  # Hermes runs a full agent loop (web/browser) — be patient
_CONFIG = Path.home() / ".hermes" / "config.yaml"


@lru_cache(maxsize=1)
def _resolve() -> tuple[str, str | None]:
    """(base_url, api_key) from env or ~/.hermes/config.yaml. Cached."""
    base = os.environ.get("HERMES_API_URL")
    key = os.environ.get("HERMES_API_KEY") or os.environ.get("API_SERVER_KEY")
    if not base or not key:
        try:
            import yaml
            cfg = yaml.safe_load(_CONFIG.read_text()) if _CONFIG.is_file() else {}
            if isinstance(cfg, dict):
                key = key or cfg.get("API_SERVER_KEY")
                host = cfg.get("API_SERVER_HOST") or "127.0.0.1"
                port = cfg.get("API_SERVER_PORT") or 8642
                base = base or (f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}")
        except Exception:  # noqa: BLE001
            log.warning("hermes: could not read %s", _CONFIG, exc_info=True)
    return (base or DEFAULT_BASE).rstrip("/"), (str(key) if key else None)


def hermes_available() -> bool:
    _, key = _resolve()
    return bool(key)


async def complete(task: str, *, system: str | None = None,
                   timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Delegate one task to the Hermes agent. Returns
    {"ok": bool, "text": str, "error"?: str}. Never raises."""
    base, key = _resolve()
    if not key:
        return {"ok": False, "text": "", "error": "Hermes API key not configured"}
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": task}]
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{base}/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": "hermes-agent", "messages": messages, "stream": False},
            )
    except httpx.TimeoutException:
        return {"ok": False, "text": "", "error": f"Hermes timed out after {timeout:.0f}s"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "text": "", "error": f"Hermes request failed: {e}"}
    if resp.status_code != 200:
        return {"ok": False, "text": "", "error": f"Hermes HTTP {resp.status_code}: {resp.text[:300]}"}
    try:
        data = resp.json()
        text = data["choices"][0]["message"]["content"] or ""
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "text": "", "error": f"Hermes bad response: {e}"}
    return {"ok": True, "text": text}
