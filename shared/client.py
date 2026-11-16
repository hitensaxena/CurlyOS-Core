"""One client contract for every CurlyOS surface.

The REST server, MCP bridge, Hermes plugin and Claude/Codex hooks all speak the
same memory API. This module owns the shared semantics that used to drift
between them: scope validation, id validation, JSON/error handling, embedding
stripping, the recall fallback, and the canonical endpoints.

Two transports are provided because the callers differ:
- ``CurlyOSClient`` (async, httpx) for MCP and async integrations.
- ``SyncCurlyOSClient`` (urllib) for Hermes and hooks, which run synchronously.
Both expose the same methods and raise ``ClientError`` on failure. A 200 body
of ``{"error": ...}`` is treated as an error, never as success.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:8643"
DEFAULT_SCOPE = "user:usr_hiten"
VALID_MODES = ("fast", "deep", "divergent")
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,100}")


class ClientError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None,
                 body: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


def validate_scope(scope: str) -> str:
    if not scope or ":" not in scope or len(scope) > 200:
        raise ValueError(f"invalid scope: {scope!r}")
    return scope


def validate_id(value: str, prefix: str | None = None) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ValueError("invalid identifier")
    if prefix and not value.startswith(prefix):
        raise ValueError(f"identifier must start with {prefix!r}")
    return value


def clean(value: Any) -> Any:
    """Drop embedding vectors from responses before they cross a tool boundary."""
    if isinstance(value, dict):
        return {
            k: clean(v) for k, v in value.items()
            if "embedding" not in k.lower() and k != "vector"
        }
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def _url(base_url: str, path: str, params: dict | None) -> str:
    url = base_url.rstrip("/") + path
    if params:
        filtered = {k: v for k, v in params.items() if v is not None}
        if filtered:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(filtered)
    return url


def _check(payload: Any, method: str, path: str) -> Any:
    if isinstance(payload, dict) and payload.get("error"):
        raise ClientError(f"{method} {path}: {payload['error']}", status=200, body=payload)
    return clean(payload)


class _ClientBase:
    def __init__(self, base_url: str | None = None, scope: str | None = None,
                 timeout: float = 30.0, token: str | None = None) -> None:
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.scope = validate_scope(scope or DEFAULT_SCOPE)
        self.timeout = timeout
        # The server's gate is dormant unless CURLYOS_API_TOKEN is set there.
        # Reading the same variable here means every client — hooks, MCP,
        # Hermes — arms itself when the operator arms the server, with no
        # per-call wiring. An explicit token wins over the environment.
        self.token = (token if token is not None
                      else os.environ.get("CURLYOS_API_TOKEN", "")).strip()

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _recall_body(self, query: str, k: int, mode: str) -> dict:
        if mode not in VALID_MODES:
            raise ValueError(f"mode must be one of {VALID_MODES}")
        if not 1 <= k <= 50:
            raise ValueError("k must be between 1 and 50")
        return {"query": query, "k": k, "mode": mode, "scope": self.scope}

    def _identity_params(self, predicates: list[str] | None) -> dict:
        return {
            "scope": self.scope,
            "predicates": ",".join(predicates) if predicates else None,
        }


class SyncCurlyOSClient(_ClientBase):
    """Synchronous client for Hermes and the Claude/Codex hooks."""

    def request(self, method: str, path: str, *, body: dict | None = None,
                params: dict | None = None, timeout: float | None = None) -> Any:
        url = _url(self.base_url, path, params)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json", **self._auth_headers()}
        request = urllib.request.Request(
            url, data=data, headers=headers, method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise ClientError(
                f"{method} {path} -> {exc.code}: {detail}", status=exc.code,
            ) from exc
        except Exception as exc:
            raise ClientError(f"{method} {path} failed: {exc}") from exc
        return _check(payload, method, path)

    def recall(self, query: str, k: int = 6, mode: str = "fast", *,
               fallback: bool = False) -> dict:
        try:
            return self.request("POST", "/api/recall", body=self._recall_body(query, k, mode))
        except ClientError:
            if not fallback:
                raise
            return self.request("GET", "/api/search", params={
                "q": query, "limit": k, "scope": self.scope,
            })

    def add_fact(self, statement: str, *, source_ref: str = "client:fact",
                 kind: str = "fact", epistemic_status: str = "canonical") -> dict:
        return self.request("POST", "/api/ingest", body={
            "text": statement, "scope": self.scope, "source_ref": source_ref,
            "kind": kind, "epistemic_status": epistemic_status,
        })

    def add_note(self, content: str, title: str = "", *,
                 source_ref: str = "client:note") -> dict:
        text = f"{title}\n\n{content}" if title else content
        return self.request("POST", "/api/ingest", body={
            "text": text, "scope": self.scope, "source_ref": source_ref,
            "kind": "procedure",
        })

    def get_memory(self, mem_id: str) -> dict:
        return self.request("GET", f"/api/memories/{validate_id(mem_id)}")

    def invalidate(self, mem_id: str, reason: str = "", *,
                   require_scope: bool = True) -> dict:
        mid = validate_id(mem_id)
        if require_scope:
            record = self.get_memory(mid)
            owner = (record.get("memory") or {}).get("scope")
            if owner != self.scope:
                raise ClientError(f"memory {mid} is outside scope {self.scope}")
        return self.request(
            "POST", f"/api/memories/{mid}/invalidate", body={"reason": reason},
        )

    def identity(self, predicates: list[str] | None = None) -> dict:
        return self.request("GET", "/api/identity", params=self._identity_params(predicates))

    def health(self) -> dict:
        return self.request("GET", "/api/health", timeout=5)


class CurlyOSClient(_ClientBase):
    """Async client for MCP and other async integrations."""

    async def request(self, method: str, path: str, *, body: dict | None = None,
                      params: dict | None = None, timeout: float | None = None) -> Any:
        import httpx

        url = _url(self.base_url, path, params)
        try:
            async with httpx.AsyncClient(timeout=timeout or self.timeout, trust_env=False) as client:
                response = await client.request(
                    method, url, json=body, headers=self._auth_headers(),
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500]
            raise ClientError(
                f"{method} {path} -> {exc.response.status_code}: {detail}",
                status=exc.response.status_code,
            ) from exc
        except Exception as exc:
            raise ClientError(f"{method} {path} failed: {exc}") from exc
        return _check(payload, method, path)

    async def recall(self, query: str, k: int = 6, mode: str = "fast", *,
                     fallback: bool = False) -> dict:
        try:
            return await self.request("POST", "/api/recall",
                                      body=self._recall_body(query, k, mode))
        except ClientError:
            if not fallback:
                raise
            return await self.request("GET", "/api/search", params={
                "q": query, "limit": k, "scope": self.scope,
            })

    async def add_fact(self, statement: str, *, source_ref: str = "client:fact",
                       kind: str = "fact", epistemic_status: str = "canonical") -> dict:
        return await self.request("POST", "/api/ingest", body={
            "text": statement, "scope": self.scope, "source_ref": source_ref,
            "kind": kind, "epistemic_status": epistemic_status,
        })

    async def add_note(self, content: str, title: str = "", *,
                       source_ref: str = "client:note") -> dict:
        text = f"{title}\n\n{content}" if title else content
        return await self.request("POST", "/api/ingest", body={
            "text": text, "scope": self.scope, "source_ref": source_ref,
            "kind": "procedure",
        })

    async def get_memory(self, mem_id: str) -> dict:
        return await self.request("GET", f"/api/memories/{validate_id(mem_id)}")

    async def invalidate(self, mem_id: str, reason: str = "", *,
                         require_scope: bool = True) -> dict:
        mid = validate_id(mem_id)
        if require_scope:
            record = await self.get_memory(mid)
            owner = (record.get("memory") or {}).get("scope")
            if owner != self.scope:
                raise ClientError(f"memory {mid} is outside scope {self.scope}")
        return await self.request(
            "POST", f"/api/memories/{mid}/invalidate", body={"reason": reason},
        )

    async def identity(self, predicates: list[str] | None = None) -> dict:
        return await self.request("GET", "/api/identity",
                                  params=self._identity_params(predicates))

    async def health(self) -> dict:
        return await self.request("GET", "/api/health", timeout=5)
