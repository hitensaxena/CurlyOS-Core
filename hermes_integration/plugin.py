"""CurlyOS MemoryProvider for Hermes Agent.

Wraps the curlyos-core memory engine (Postgres+pgvector) as a Hermes MemoryProvider.
This module exposes register(ctx) which calls ctx.register_memory_provider().
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

RECALL_SCHEMA = {
    "name": "curlyos_recall",
    "description": (
        "Semantic + graph retrieval over Hiten's personal knowledge base. "
        "Returns facts, episodes, and linked context. Filters out invalidated facts. "
        "Use for deep recall: personal facts, decisions, projects, relationships, goals."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "k": {"type": "integer", "description": "Max results (default: 6, max: 20)."},
            "mode": {
                "type": "string",
                "enum": ["fast", "deep", "divergent"],
                "description": "Retrieval mode. fast=cached, deep=more hops, divergent=novelty.",
            },
        },
        "required": ["query"],
    },
}

ADD_FACT_SCHEMA = {
    "name": "curlyos_add_fact",
    "description": (
        "Store a durable, grounded fact with bi-temporal validity. "
        "Use for important things: preferences, decisions, beliefs, goals, relationships."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "statement": {"type": "string", "description": "The factual statement."},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "Categorization tags."},
            "valid_from": {"type": "string", "description": "ISO date (defaults to today)."},
        },
        "required": ["statement"],
    },
}

ADD_NOTE_SCHEMA = {
    "name": "curlyos_add_note",
    "description": "Store a longer note. For simple facts, prefer curlyos_add_fact.",
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Note content."},
            "title": {"type": "string", "description": "Short title."},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "Tags."},
        },
        "required": ["content"],
    },
}

INVALIDATE_SCHEMA = {
    "name": "curlyos_invalidate",
    "description": (
        "Soft-invalidate a fact — mark as no longer true without deleting. "
        "First use curlyos_recall to find the ID."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mem_id": {"type": "string", "description": "Fact ID to invalidate."},
            "reason": {"type": "string", "description": "Why no longer true."},
            "superseded_by": {"type": "string", "description": "Replacement fact ID (optional)."},
        },
        "required": ["mem_id", "reason"],
    },
}

IDENTITY_SCHEMA = {
    "name": "curlyos_identity",
    "description": "Query Hiten's identity context — stable self-model facts.",
    "parameters": {
        "type": "object",
        "properties": {
            "predicates": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Predicates to fetch. Omit for all.",
            },
        },
        "required": [],
    },
}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()

def _today_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# CurlyOS MemoryProvider
# ---------------------------------------------------------------------------

class CurlyOSMemoryProvider(MemoryProvider):
    """Direct Postgres+pgvector memory provider using CurlyOS Core."""

    def __init__(self):
        self._dsn = ""
        self._pool = None
        self._loop = None
        self._session_id = ""
        self._session_episode_id: Optional[str] = None
        self._scope_text = "user:usr_hiten"
        self._turn_count = 0
        self._consolidation_turn_count = 0
        self._auto_record = True
        self._prefetch_result = ""
        self._prefetch_lock = threading.Lock()

    @property
    def name(self) -> str:
        return "curlyos"

    def is_available(self) -> bool:
        return bool(os.environ.get("CURLYOS_DATABASE_URL", ""))

    def initialize(self, session_id: str, **kwargs) -> None:
        self._dsn = os.environ.get("CURLYOS_DATABASE_URL", "")
        self._session_id = session_id
        self._session_episode_id = None
        self._turn_count = 0
        self._consolidation_turn_count = 0
        self._api_thread = None
        # Single-user deployment: all memory is owned by Hiten. The gateway
        # passes the platform user_id (e.g. the numeric Telegram ID), which
        # would fork reads/writes into an empty phantom scope. Pin to a
        # canonical user so memory is shared across CLI/TUI/Telegram/etc.
        # Override with CURLYOS_CANONICAL_USER to opt out of this behavior.
        platform_user_id = kwargs.get("user_id", "") or kwargs.get("user_id_alt", "")
        canonical_user = os.environ.get("CURLYOS_CANONICAL_USER", "hiten")
        user_id = canonical_user or platform_user_id or "hiten"
        self._scope_text = f"user:usr_{user_id}"
        logger.info("CurlyOS scope pinned to %s (platform_user_id=%r)",
                    self._scope_text, platform_user_id)
        logger.info("CurlyOS provider initialized: scope=%s session=%s", self._scope_text, session_id)

        # Ensure curlyos-core is on sys.path for api_server imports
        curlyos_path = os.path.join(os.path.expanduser("~"), "curlyos-core")
        if curlyos_path not in sys.path:
            sys.path.insert(0, curlyos_path)

        # Eagerly load all curlyos-core subpackages into sys.modules so that
        # later imports inside handle_tool_call (which runs in a different
        # import context) resolve correctly.
        if self._dsn:
            try:
                self._import_curlyos()
            except Exception as e:
                logger.debug("CurlyOS eager import during init failed (will retry on tool call): %s", e)

        # Start API server in background thread (optional — only if deps available)
        api_port = int(os.environ.get("CURLYOS_API_PORT", "8643"))
        try:
            import threading

            def _run_api():
                import uvicorn
                from api_server import app
                uvicorn.run(app, host="127.0.0.1", port=api_port, log_level="warning")

            self._api_thread = threading.Thread(target=_run_api, daemon=True, name="curlyos-api")
            self._api_thread.start()
            logger.info("CurlyOS API server started on port %d", api_port)
        except ImportError as e:
            logger.info("CurlyOS API server not started (dependency missing: %s) — optional, continuing", e)
        except Exception as e:
            logger.warning("Failed to start CurlyOS API server: %s", e)

    def system_prompt_block(self) -> str:
        return (
            "# CurlyOS Memory (Bi-Temporal Knowledge Graph)\n"
            "Active. Episodic provenance + semantic facts + hybrid retrieval.\n"
            "- curlyos_recall: retrieve facts, episodes, identity context\n"
            "- curlyos_add_fact: store a bi-temporal fact (grounded in episode)\n"
            "- curlyos_add_note: store longer notes / reference material\n"
            "- curlyos_invalidate: soft-invalidate outdated facts (never deletes)\n"
            "- curlyos_identity: query Hiten's stable self-model\n"
            "Facts are invalidated-not-deleted. Always check curlyos_recall first."
        )

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [RECALL_SCHEMA, ADD_FACT_SCHEMA, ADD_NOTE_SCHEMA, INVALIDATE_SCHEMA, IDENTITY_SCHEMA]

    def get_config_schema(self):
        return [
            {"key": "database_url", "description": "PostgreSQL DSN", "required": True, "env_var": "CURLYOS_DATABASE_URL"},
            {"key": "redis_url", "description": "Redis URL", "env_var": "CURLYOS_REDIS_URL"},
        ]

    def _run_async(self, coro):
        """Run an async coroutine from sync context."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                result = [None, None]
                def _run():
                    try:
                        result[0] = asyncio.run(coro)
                    except Exception as e:
                        result[1] = e
                t = threading.Thread(target=_run, daemon=True)
                t.start()
                t.join(timeout=15)
                if result[1]:
                    raise result[1]
                return result[0]
            else:
                return loop.run_until_complete(coro)
        except RuntimeError:
            return asyncio.run(coro)

    @staticmethod
    def _curlyos_path():
        """Return the absolute path to curlyos-core."""
        return os.path.join(os.path.expanduser("~"), "curlyos-core")

    def _import_curlyos(self):
        """Import CurlyOS Core modules, ensuring sys.path is set and caches are clean."""
        import importlib
        import importlib.util
        import sys
        path = self._curlyos_path()

        if path not in sys.path:
            sys.path.insert(0, path)

        _prefixes = ("memory.", "shared.", "identity.", "cognition.")
        _stale = [k for k in sys.modules
                  if any(k.startswith(p) for p in _prefixes)]
        for _extra in ("memory", "shared", "identity", "cognition"):
            if _extra in sys.modules:
                _stale.append(_extra)
        for _key in _stale:
            del sys.modules[_key]

        _pkg_map = {
            "memory":                 "memory/__init__.py",
            "memory.governance":      "memory/governance/__init__.py",
            "memory.retrieval":       "memory/retrieval/__init__.py",
            "memory.consolidation":   "memory/consolidation/__init__.py",
            "shared":                 "shared/__init__.py",
            "shared.types":           "shared/types/__init__.py",
            "shared.events":          "shared/events/__init__.py",
            "shared.events.implementations": "shared/events/implementations/__init__.py",
            "shared.embeddings":      "shared/embeddings/__init__.py",
            "shared.embeddings.implementations": "shared/embeddings/implementations/__init__.py",
            "identity":               "identity/__init__.py",
            "cognition":              "cognition/__init__.py",
            "cognition.reflection":   "cognition/reflection/__init__.py",
        }
        for _mod_name, _rel_path in _pkg_map.items():
            _file = os.path.join(path, _rel_path)
            if os.path.isfile(_file):
                _spec = importlib.util.spec_from_file_location(
                    _mod_name, _file,
                    submodule_search_locations=[os.path.dirname(_file)],
                )
                _mod = importlib.util.module_from_spec(_spec)
                sys.modules[_mod_name] = _mod
                _spec.loader.exec_module(_mod)

        from memory.governance import record_episode, add, invalidate, list_memories
        from memory.retrieval import retrieve as mem_retrieve
        from identity import get_identity_context, propose_identity_fact
        from shared.types import RetrievalRequest
        from shared.events.implementations import PgOnlyPublisher
        from shared.embeddings.implementations import LocalBgeM3, FakeReranker
        return (
            record_episode, add, invalidate, list_memories,
            mem_retrieve, get_identity_context, propose_identity_fact,
            RetrievalRequest, PgOnlyPublisher, LocalBgeM3, FakeReranker,
        )

    def _make_sync_pool(self, dsn):
        """Create a synchronous wrapper around psycopg for async-style usage."""
        import psycopg

        class SyncPool:
            def __init__(self, dsn):
                self._dsn = dsn
            def connection(self):
                return SyncPool._CtxMgr(self._dsn)

            class _CtxMgr:
                def __init__(self, dsn):
                    self._dsn = dsn
                    self._conn = None
                async def __aenter__(self):
                    self._conn = psycopg.connect(self._dsn, autocommit=False)
                    return SyncPool._ConnWrap(self._conn)
                async def __aexit__(self, *a):
                    if self._conn:
                        self._conn.commit()
                        self._conn.close()

            class _ConnWrap:
                def __init__(self, c):
                    self._c = c
                def cursor(self):
                    return SyncPool._CurCtx(self._c)

            class _CurCtx:
                def __init__(self, c):
                    self._c = c
                async def __aenter__(self):
                    return SyncPool._CurAdapt(self._c.cursor())
                async def __aexit__(self, *a):
                    pass

            class _CurAdapt:
                def __init__(self, c):
                    self._c = c
                async def execute(self, q, p=None):
                    self._c.execute(q, p)
                async def fetchone(self):
                    return self._c.fetchone()
                async def fetchall(self):
                    return self._c.fetchall()

        return SyncPool(dsn)

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if not self._dsn:
            return json.dumps({"error": "CURLYOS_DATABASE_URL not set"})

        try:
            (
                record_episode, add, invalidate_op, list_memories,
                mem_retrieve, get_identity_context, propose_identity_fact,
                RetrievalRequest, PgOnlyPublisher, LocalBgeM3, FakeReranker,
            ) = self._import_curlyos()

            pool = self._make_sync_pool(self._dsn)
            pub = PgOnlyPublisher()

            if tool_name == "curlyos_recall":
                query = args.get("query", "")
                k = min(int(args.get("k", 6)), 20)
                mode = args.get("mode", "fast")
                # Semantic recall needs query embedding (sentence_transformers),
                # which THIS plugin's venv (hermes-agent) does NOT have. Running
                # LocalBgeM3 in-process either errors (no torch/ST) or — worse —
                # silently returns zero vectors (FakeEmbedder), which makes every
                # pgvector distance equal so rows come back in ULID order and the
                # ranking is garbage. So we route recall through the curlyos-core
                # API (:8643), which runs in a venv that HAS the model warm.
                import urllib.request
                api_url = os.environ.get(
                    "CURLYOS_API_URL", "http://127.0.0.1:8643").rstrip("/")
                try:
                    payload = json.dumps({
                        "query": query, "scope": self._scope_text,
                        "mode": mode, "k": k,
                    }).encode()
                    req = urllib.request.Request(
                        api_url + "/api/recall", data=payload,
                        headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=30) as resp:
                        data = json.loads(resp.read().decode())
                    if data.get("error"):
                        raise RuntimeError(data["error"])
                    return json.dumps({
                        "results": data.get("results", [])[:k],
                        "count": data.get("count", len(data.get("results", []))),
                    })
                except Exception as e:
                    # Fallback: keyword full-text search via API (no embedding).
                    try:
                        import urllib.parse
                        qs = urllib.parse.urlencode({
                            "q": query, "scope": self._scope_text, "limit": k})
                        with urllib.request.urlopen(
                                api_url + "/api/search?" + qs, timeout=15) as resp:
                            data = json.loads(resp.read().decode())
                        items = [{
                            "id": it.get("id"),
                            "text": (it.get("statement") or "")[:200],
                            "score": it.get("score", 0.0),
                            "tier": it.get("kind", "fact"),
                            "epistemic_status": it.get("epistemic_status", "canonical"),
                        } for it in data.get("items", [])]
                        return json.dumps({
                            "results": items, "count": len(items),
                            "fallback": "keyword"})
                    except Exception as e2:
                        return json.dumps({
                            "error": f"recall via API failed: {e}; "
                                     f"keyword fallback failed: {e2}",
                            "results": [], "count": 0})

            elif tool_name == "curlyos_add_fact":
                statement = args.get("statement", "")
                valid_from = args.get("valid_from") or _today_iso()
                if not self._session_episode_id:
                    epi = self._run_async(record_episode(pool, pub, self._scope_text,
                        content=f"Session context: {statement[:200]}", source_ref="hermes"))
                    self._session_episode_id = epi["epi_id"]
                ref = self._run_async(add(pool, pub, self._scope_text,
                    statement=statement, source_episode_id=self._session_episode_id))
                return json.dumps({"result": "Fact stored.", "id": ref["mem_id"],
                                   "valid_from": valid_from})

            elif tool_name == "curlyos_add_note":
                content = args.get("content", "")
                if not self._session_episode_id:
                    epi = self._run_async(record_episode(pool, pub, self._scope_text,
                        content=content[:200], source_ref="hermes"))
                    self._session_episode_id = epi["epi_id"]
                ref = self._run_async(add(pool, pub, self._scope_text,
                    statement=content, source_episode_id=self._session_episode_id,
                    kind="procedure"))
                return json.dumps({"result": "Note stored.", "id": ref["mem_id"]})

            elif tool_name == "curlyos_invalidate":
                mem_id = args.get("mem_id", "")
                reason = args.get("reason", "")
                sup = args.get("superseded_by")
                result = self._run_async(invalidate_op(pool, pub, self._scope_text,
                    mem_id=mem_id, superseded_by=sup, reason=reason))
                return json.dumps({"result": "Fact invalidated.", "valid_to": str(result.get("valid_to", ""))})

            elif tool_name == "curlyos_identity":
                predicates = args.get("predicates")
                ctx = self._run_async(get_identity_context(pool, self._scope_text,
                    predicates=predicates))
                return json.dumps({"identity": ctx})

            return tool_error(f"Unknown tool: {tool_name}")
        except Exception as e:
            logger.error("CurlyOS tool error: %s", e, exc_info=True)
            return tool_error(f"CurlyOS error: {e}")

    # ════════════════════════════════════════════════════════════════════════
    # Autonomous Operation Hooks
    # ════════════════════════════════════════════════════════════════════════

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall relevant context for the upcoming turn.

        Called before each API call. Returns cached result from the
        background thread started by queue_prefetch() on the previous turn.
        """
        if not query.strip():
            return ""
        if not self._dsn:
            return ""
        try:
            with self._prefetch_lock:
                cached = self._prefetch_result
            if cached:
                return cached
        except Exception as e:
            logger.debug("prefetch cache read failed: %s", e)
        return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Queue a background recall for the NEXT turn.

        Called after each turn completes. Starts a background thread that
        calls curlyos_recall and stores the formatted result in
        self._prefetch_result, which prefetch() will read at the start
        of the next turn.
        """
        if not query.strip():
            return
        if not self._dsn:
            return

        def _background_recall():
            try:
                # Route through the curlyos-core API (:8643) — recall needs the
                # embedding model, which this venv lacks. See curlyos_recall.
                import urllib.request
                api_url = os.environ.get(
                    "CURLYOS_API_URL", "http://127.0.0.1:8643").rstrip("/")
                payload = json.dumps({
                    "query": query, "scope": self._scope_text,
                    "mode": "fast", "k": 6,
                }).encode()
                req = urllib.request.Request(
                    api_url + "/api/recall", data=payload,
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode())
                items = data.get("results", [])[:6]
                if data.get("error") or not items:
                    with self._prefetch_lock:
                        self._prefetch_result = ""
                    return

                lines = [
                    "[CurlyOS Memory Context — NOT new user input, treat as authoritative reference]",
                    "",
                ]
                for i in items:
                    sc = i.get("score")
                    score_str = f"score={sc:.2f}" if isinstance(sc, (int, float)) else "score=N/A"
                    tier_str = i.get("tier") or "unknown"
                    lines.append(f"• ({tier_str}, {score_str}) {(i.get('text') or '')[:200]}")

                formatted = "\n".join(lines)
                with self._prefetch_lock:
                    self._prefetch_result = formatted
                logger.debug("queue_prefetch: cached %d results for next turn", len(items))
            except Exception as e:
                logger.debug("queue_prefetch background recall failed: %s", e)
                with self._prefetch_lock:
                    self._prefetch_result = ""

        t = threading.Thread(target=_background_recall, daemon=True, name="curlyos-prefetch")
        t.start()

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        """Called at the start of each turn with the user message.

        Increments internal turn counter, periodically logs status,
        and every 10 turns runs a quick consolidation check.
        """
        self._turn_count += 1
        logger.debug("CurlyOS on_turn_start: turn=%d turn_count=%d", turn_number, self._turn_count)

        if self._turn_count % 10 == 0:
            self._run_consolidation_check()

    def _run_consolidation_check(self):
        """Quick consolidation check — count pending events and log status."""
        if not self._dsn:
            return
        try:
            pool = self._make_sync_pool(self._dsn)

            async def _check():
                async with pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT COUNT(*) FROM events "
                            "WHERE scope = %s AND seq > COALESCE("
                            "  (SELECT MIN(last_seq) FROM projection_watermarks "
                            "   WHERE scope = %s), 0)",
                            (self._scope_text, self._scope_text),
                        )
                        row = await cur.fetchone()
                        return row[0] if row else 0

            pending = self._run_async(_check())
            logger.info("CurlyOS consolidation check (turn=%d): %d unconsolidated events for scope %s",
                        self._turn_count, pending, self._scope_text)
        except Exception as e:
            logger.debug("CurlyOS consolidation check failed: %s", e)

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Called when a session ends (explicit exit or timeout).

        Extracts key facts from user messages, records a session-end
        episode, proposes identity facts, and triggers fast-path
        consolidation.
        """
        if not self._dsn or not messages:
            return

        logger.info("CurlyOS on_session_end: processing %d messages", len(messages))

        # ── Step 1: Extract facts from user messages ──
        identity_patterns = [
            (re.compile(r"\bmy name is\s+(\w+)", re.IGNORECASE), "name", "identity"),
            (re.compile(r"\bi am\s+(?:a\s+)?(.+?)(?:\.|$)", re.IGNORECASE), "is_a", "identity"),
            (re.compile(r"\bi'm\s+(?:a\s+)?(.+?)(?:\.|$)", re.IGNORECASE), "is_a", "identity"),
            (re.compile(r"\bi prefer\s+(.+?)(?:\.|$)", re.IGNORECASE), "preference", "preference"),
            (re.compile(r"\bi like\s+(.+?)(?:\.|$)", re.IGNORECASE), "likes", "preference"),
            (re.compile(r"\bi use\s+(\w+(?:\s+\w+)?)", re.IGNORECASE), "uses_tool", "preference"),
            (re.compile(r"\bi work on\s+(.+?)(?:\.|$)", re.IGNORECASE), "works_on", "project"),
            (re.compile(r"\bmy project is\s+(.+?)(?:\.|$)", re.IGNORECASE), "works_on", "project"),
            (re.compile(r"\bmy (\w+) is\s+(.+?)(?:\.|$)", re.IGNORECASE), "has_property", "semantic"),
        ]

        user_messages = []
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    user_messages.append(content)
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            user_messages.append(part.get("text", ""))

        # Extract identity facts
        extracted_facts = []  # (predicate, object, tag)
        semantic_facts = []   # free-form statements

        for text in user_messages:
            for pattern, predicate, tag in identity_patterns:
                for match in pattern.finditer(text):
                    if predicate == "has_property":
                        prop = match.group(1).strip().lower()
                        value = match.group(2).strip()
                        obj = f"{prop}: {value}"
                    else:
                        obj = match.group(1).strip().rstrip(".")
                    if obj and len(obj) > 1:
                        extracted_facts.append((predicate, obj, tag))

            # Also extract "I work on X" / "my project is X" patterns as semantic facts
            work_match = re.search(r"\bi(?:\'m|\s+am)?\s+(?:working on|building|developing)\s+(.+?)(?:\.|$)", text, re.IGNORECASE)
            if work_match:
                semantic_facts.append(f"Hiten is working on {work_match.group(1).strip()}")

        # Deduplicate
        seen_idf = set()
        unique_idf = []
        for pred, obj, tag in extracted_facts:
            key = (pred, obj.lower())
            if key not in seen_idf:
                seen_idf.add(key)
                unique_idf.append((pred, obj, tag))

        # ── Step 2: Build summary ──
        n_user = len(user_messages)
        n_idf = len(unique_idf)
        n_sem = len(semantic_facts)
        summary_parts = [f"Session ended. {n_user} user messages processed."]
        if n_idf:
            summary_parts.append(f"Extracted {n_idf} identity facts.")
        if n_sem:
            summary_parts.append(f"Extracted {n_sem} semantic facts.")
        summary = " ".join(summary_parts)

        # ── Step 3: Record session-end episode and write facts ──
        try:
            (
                record_episode, add, invalidate_op, list_memories,
                mem_retrieve, get_identity_ctx, propose_identity_fact,
                RetrievalRequest, PgOnlyPublisher, LocalBgeM3, FakeReranker,
            ) = self._import_curlyos()
            pool = self._make_sync_pool(self._dsn)
            pub = PgOnlyPublisher()

            # Record session-end episode
            epi = self._run_async(record_episode(pool, pub, self._scope_text,
                content=summary[:2000], source_ref=f"hermes:{self._session_id}:session_end"))
            epi_id = epi.get("epi_id")

            # Write identity facts
            for predicate, obj, tag in unique_idf:
                try:
                    self._run_async(propose_identity_fact(
                        pool, pub, self._scope_text,
                        predicate=predicate,
                        object=obj,
                        confidence=0.8,
                        source_episode_id=epi_id,
                    ))
                except Exception as e:
                    logger.debug("Failed to propose identity fact (%s=%s): %s", predicate, obj, e)

            # Write semantic facts
            for stmt in semantic_facts:
                try:
                    self._run_async(add(pool, pub, self._scope_text,
                        statement=stmt, source_episode_id=epi_id))
                except Exception as e:
                    logger.debug("Failed to write semantic fact: %s", e)

            logger.info("CurlyOS session-end: recorded episode, wrote %d identity + %d semantic facts",
                        len(unique_idf), len(semantic_facts))
        except Exception as e:
            logger.warning("CurlyOS on_session_end episode/fact write failed: %s", e)

        # ── Step 4: Trigger fast-path consolidation in background ──
        self._trigger_async_consolidation()

    def _trigger_async_consolidation(self):
        """Run fast-path consolidation in a background thread (non-blocking)."""
        if not self._dsn:
            return

        def _run():
            try:
                from memory.consolidation import run_consolidation
                import redis.asyncio as aioredis

                (
                    record_episode, add, invalidate_op, list_memories,
                    mem_retrieve, get_identity_ctx, propose_identity_fact,
                    RetrievalRequest, PgOnlyPublisher, LocalBgeM3, FakeReranker,
                ) = self._import_curlyos()
                pool = self._make_sync_pool(self._dsn)
                pub = PgOnlyPublisher()

                # Try Redis, fall back to FakeRedis
                redis_url = os.environ.get("CURLYOS_REDIS_URL", "")
                if redis_url:
                    redis_client = aioredis.from_url(redis_url)
                else:
                    from memory.consolidation.scheduler import FakeRedis
                    redis_client = None  # will create inline

                loop = asyncio.new_event_loop()
                try:
                    if redis_client is None:
                        # Inline FakeRedis
                        class FakeRedis:
                            def __init__(self): self._data = {}
                            async def set(self, k, v, nx=False, px=None):
                                if nx and k in self._data: return False
                                self._data[k] = v; return True
                            async def get(self, k): return self._data.get(k)
                            async def delete(self, *keys):
                                for k in keys: self._data.pop(k, None)
                            async def hset(self, n, k, v):
                                if n not in self._data: self._data[n] = {}
                                self._data[n][k] = v
                            async def hdel(self, n, *keys):
                                for k in keys: self._data.get(n, {}).pop(k, None)
                            async def scan_iter(self, match=None):
                                import fnmatch
                                for k in self._data:
                                    if match is None or fnmatch.fnmatch(k, match): yield k
                            async def keys(self, pattern=None):
                                import fnmatch
                                if pattern: return [k for k in self._data if fnmatch.fnmatch(k, pattern)]
                                return list(self._data.keys())
                        redis_client = FakeRedis()

                    result = loop.run_until_complete(run_consolidation(
                        pool, redis_client, LocalBgeM3(), pub, FakeReranker(),
                        scope=self._scope_text, deep=False,
                    ))
                    scopes = result.get("scopes", [])
                    total_processed = sum(
                        s.get("projection", {}).get("processed", 0) for s in scopes
                    )
                    logger.info("CurlyOS async consolidation complete: %d events processed across %d scopes",
                                total_processed, len(scopes))
                finally:
                    loop.close()
            except Exception as e:
                logger.warning("CurlyOS async consolidation failed (non-fatal): %s", e)

        t = threading.Thread(target=_run, daemon=True, name="curlyos-consolidation")
        t.start()

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Called before context compression discards old messages.

        Extracts insights from messages about to be compressed and writes
        them as hypothesis-status memories so they survive compression.

        Returns empty string (the insights are in the DB, not needed in
        the compression prompt).
        """
        if not self._dsn or not messages:
            return ""

        try:
            # Extract user-assistant pairs from messages about to be compressed
            insights = []
            for i, msg in enumerate(messages):
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role", "")
                if role == "user":
                    content = msg.get("content", "")
                    if isinstance(content, str) and len(content.strip()) > 20:
                        # Simple extraction: keep substantive user statements
                        insights.append(content.strip()[:300])
                    elif isinstance(content, list):
                        for part in content:
                            if isinstance(part, dict) and part.get("type") == "text":
                                text = part.get("text", "").strip()
                                if len(text) > 20:
                                    insights.append(text[:300])

            if not insights:
                return ""

            # Write as hypothesis memories
            (
                record_episode, add, invalidate_op, list_memories,
                mem_retrieve, get_identity_ctx, propose_identity_fact,
                RetrievalRequest, PgOnlyPublisher, LocalBgeM3, FakeReranker,
            ) = self._import_curlyos()
            pool = self._make_sync_pool(self._dsn)
            pub = PgOnlyPublisher()

            # Record a compression episode
            epi = self._run_async(record_episode(pool, pub, self._scope_text,
                content=f"Pre-compression extraction: {len(insights)} insights from {len(messages)} messages",
                source_ref=f"hermes:{self._session_id}:pre_compress"))
            epi_id = epi.get("epi_id")

            for insight in insights[:5]:  # cap at 5 to avoid spam
                try:
                    self._run_async(add(pool, pub, self._scope_text,
                        statement=insight, source_episode_id=epi_id,
                        epistemic_status="hypothesis"))
                except Exception as e:
                    logger.debug("Failed to write pre-compress insight: %s", e)

            logger.debug("CurlyOS on_pre_compress: wrote %d hypothesis insights from %d messages",
                         min(len(insights), 5), len(messages))
        except Exception as e:
            logger.warning("CurlyOS on_pre_compress failed (non-fatal): %s", e)

        return ""

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Called when the built-in memory tool writes an entry.

        Mirrors built-in memory writes to the CurlyOS backend:
        - action='add', target='memory' → store as semantic fact
        - action='add', target='user'   → store as identity fact (preference)
        - action='replace'              → invalidate old, add new
        - action='remove'               → invalidate
        """
        if not self._dsn or not content.strip():
            return

        try:
            (
                record_episode, add, invalidate_op, list_memories,
                mem_retrieve, get_identity_ctx, propose_identity_fact,
                RetrievalRequest, PgOnlyPublisher, LocalBgeM3, FakeReranker,
            ) = self._import_curlyos()
            pool = self._make_sync_pool(self._dsn)
            pub = PgOnlyPublisher()

            if action == "add":
                if target == "memory":
                    # Store as a semantic fact
                    epi = self._run_async(record_episode(pool, pub, self._scope_text,
                        content=f"Memory: {content[:200]}", source_ref="hermes:builtin_memory"))
                    self._run_async(add(pool, pub, self._scope_text,
                        statement=content, source_episode_id=epi["epi_id"]))
                    logger.debug("CurlyOS on_memory_write: stored semantic fact (%d chars)", len(content))

                elif target == "user":
                    # Store as identity fact (preference/self-model)
                    epi = self._run_async(record_episode(pool, pub, self._scope_text,
                        content=f"User fact: {content[:200]}", source_ref="hermes:builtin_memory"))
                    self._run_async(propose_identity_fact(
                        pool, pub, self._scope_text,
                        predicate="stated_preference",
                        object=content,
                        confidence=0.9,
                        source_episode_id=epi["epi_id"],
                    ))
                    logger.debug("CurlyOS on_memory_write: stored identity fact (%d chars)", len(content))

            elif action == "replace":
                # Invalidate old (by content match) and add new
                metadata = metadata or {}
                old_content = metadata.get("old_content", "")
                if old_content:
                    try:
                        result = self._run_async(
                            list_memories(pool, self._scope_text, min_similarity=0.85)
                        )
                        for mem in getattr(result, "items", []):
                            if mem.text and old_content[:80] in mem.text:
                                self._run_async(invalidate_op(
                                    pool, pub, self._scope_text,
                                    mem_id=mem.id,
                                    reason="Replaced via built-in memory tool",
                                ))
                                break
                    except Exception as e:
                        logger.debug("Failed to invalidate old memory for replace: %s", e)

                # Add new
                epi = self._run_async(record_episode(pool, pub, self._scope_text,
                    content=f"Memory (replaced): {content[:200]}", source_ref="hermes:builtin_memory"))
                self._run_async(add(pool, pub, self._scope_text,
                    statement=content, source_episode_id=epi["epi_id"]))
                logger.debug("CurlyOS on_memory_write: replaced memory fact")

            elif action == "remove":
                # Invalidate by content match
                try:
                    result = self._run_async(
                        list_memories(pool, self._scope_text, min_similarity=0.85)
                    )
                    for mem in getattr(result, "items", []):
                        if mem.text and content[:80] in mem.text:
                            self._run_async(invalidate_op(
                                pool, pub, self._scope_text,
                                mem_id=mem.id,
                                reason="Removed via built-in memory tool",
                            ))
                            logger.debug("CurlyOS on_memory_write: invalidated memory fact")
                            break
                except Exception as e:
                    logger.debug("Failed to invalidate memory for remove: %s", e)

        except Exception as e:
            logger.warning("CurlyOS on_memory_write failed (non-fatal): %s", e)

    def on_delegation(
        self,
        task: str,
        result: str,
        *,
        child_session_id: str = "",
        **kwargs,
    ) -> None:
        """Called on the PARENT agent when a subagent completes.

        Records the delegation as an episode and extracts any facts
        from the subagent's result.
        """
        if not self._dsn:
            return

        logger.info("CurlyOS on_delegation: child_session=%s task=%.60s", child_session_id or "?", task)

        try:
            (
                record_episode, add, invalidate_op, list_memories,
                mem_retrieve, get_identity_ctx, propose_identity_fact,
                RetrievalRequest, PgOnlyPublisher, LocalBgeM3, FakeReranker,
            ) = self._import_curlyos()
            pool = self._make_sync_pool(self._dsn)
            pub = PgOnlyPublisher()

            # Record delegation episode
            epi_content = (
                f"Delegated task (child_session={child_session_id}):\n"
                f"{task[:500]}\n\n"
                f"Result:\n{result[:1000]}"
            )
            epi = self._run_async(record_episode(pool, pub, self._scope_text,
                content=epi_content[:2000],
                source_ref=f"hermes:delegation:{child_session_id}"))
            epi_id = epi.get("epi_id")

            # Extract facts from the result using simple pattern matching
            self_ref_patterns = [
                (re.compile(r"\b(i|we)\s+found\s+(?:that\s+)?(.+?)(?:\.|$)", re.IGNORECASE), "found"),
                (re.compile(r"\bthe\s+result\s+(?:is|was)\s+(.+?)(?:\.|$)", re.IGNORECASE), "result"),
                (re.compile(r"\bkey\s+(?:finding|insight|takeaway):\s*(.+?)(?:\.|$)", re.IGNORECASE), "insight"),
            ]

            extracted = []
            result_text = result if isinstance(result, str) else str(result)
            for pattern, kind in self_ref_patterns:
                for match in pattern.finditer(result_text):
                    stmt = match.group(2) if kind == "found" else match.group(1)
                    stmt = stmt.strip().rstrip(".")
                    if stmt and len(stmt) > 5:
                        extracted.append((kind, stmt[:300]))

            # Write extracted facts as hypothesis memories
            for kind, stmt in extracted[:3]:
                try:
                    full_stmt = f"[Delegation {kind}] {stmt}"
                    self._run_async(add(pool, pub, self._scope_text,
                        statement=full_stmt, source_episode_id=epi_id,
                        epistemic_status="hypothesis"))
                except Exception as e:
                    logger.debug("Failed to write delegation fact: %s", e)

            logger.debug("CurlyOS on_delegation: recorded episode, extracted %d facts",
                         len(extracted))
        except Exception as e:
            logger.warning("CurlyOS on_delegation failed (non-fatal): %s", e)

    # ════════════════════════════════════════════════════════════════════════
    # Sync turn (enhanced with consolidation trigger)
    # ════════════════════════════════════════════════════════════════════════

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "", **kwargs) -> None:
        """Persist a completed turn to the backend, with periodic consolidation."""
        if not self._auto_record or not self._dsn:
            return
        self._turn_count += 1
        self._consolidation_turn_count += 1
        if len(user_content) < 20 and len(assistant_content) < 50:
            return
        try:
            record_episode = self._import_curlyos()[0]
            LocalBgeM3 = self._import_curlyos()[9]  # not used here, but import is cached
            PgOnlyPublisher = self._import_curlyos()[8]
            pool = self._make_sync_pool(self._dsn)
            pub = PgOnlyPublisher()
            episode_content = f"[turn {self._turn_count}] User: {user_content[:800]}\n\nAssistant: {assistant_content[:1200]}"
            epi = self._run_async(record_episode(pool, pub, self._scope_text,
                content=episode_content[:3000], source_ref=f"hermes:{self._session_id}"))
            if epi and not self._session_episode_id:
                self._session_episode_id = epi["epi_id"]
        except Exception as e:
            logger.debug("CurlyOS sync_turn failed: %s", e)
            return

        # Every 20 turns: run fast-path consolidation in background (non-blocking)
        if self._consolidation_turn_count % 20 == 0:
            logger.info("CurlyOS sync_turn: triggering consolidation (turn_count=%d)", self._turn_count)
            self._trigger_async_consolidation()

    def on_session_switch(self, new_session_id: str, *, reset: bool = False, **kwargs) -> None:
        self._session_id = new_session_id
        if reset:
            self._session_episode_id = None
            self._turn_count = 0
            self._consolidation_turn_count = 0

    def shutdown(self) -> None:
        if self._api_thread is not None:
            logger.info("CurlyOS API server thread is daemon — will stop when process exits")
            self._api_thread = None


def register(ctx) -> None:
    ctx.register_memory_provider(CurlyOSMemoryProvider())
