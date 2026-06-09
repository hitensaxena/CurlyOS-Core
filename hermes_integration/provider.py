"""CurlyOS MemoryProvider for Hermes Agent.

Wraps the curlyos-core memory engine (Postgres+pgvector) as a Hermes MemoryProvider.
"""
from __future__ import annotations

import json
import logging
import os
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
                "type": "string", "enum": ["fast", "deep", "divergent"],
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
                "type": "array", "items": {"type": "string"},
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
        self._auto_record = True
        self._prefetch_result = ""
        self._prefetch_lock = threading.Lock()
        self._consolidation_scheduler = None

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
        # Try to get user_id from kwargs
        user_id = kwargs.get("user_id", "") or kwargs.get("user_id_alt", "") or "hiten"
        self._scope_text = f"user:usr_{user_id}"
        logger.info("CurlyOS provider initialized: scope=%s session=%s", self._scope_text, session_id)

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

    def _get_embedder(self):
        """Load the configured embedder."""
        embedder_type = os.environ.get("CURLYOS_EMBEDDER", "fake")
        if embedder_type == "bge-m3":
            try:
                from shared.embeddings.implementations import LocalBgeM3
                return LocalBgeM3()
            except Exception as e:
                logger.warning("Failed to load LocalBgeM3: %s, falling back to FakeEmbedder", e)
        from shared.embeddings.implementations import FakeEmbedder
        return FakeEmbedder()

    def _run_async(self, coro):
        """Run an async coroutine from sync context."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're inside an existing event loop — create a new one in a thread
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

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if not self._dsn:
            return json.dumps({"error": "CURLYOS_DATABASE_URL not set"})

        try:
            from memory.governance import record_episode, add, invalidate, list_memories
            from memory.retrieval import retrieve as mem_retrieve
            from identity import get_identity_context, propose_identity_fact
            from shared.types import RetrievalRequest
            from shared.events.implementations import PgOnlyPublisher
            from shared.embeddings.implementations import FakeEmbedder, FakeReranker

            # Create sync pool + publisher for this call
            import psycopg

            class SyncPool:
                def __init__(self, dsn):
                    self._dsn = dsn
                def connection(self):
                    return _CtxMgr(self._dsn)

            class _CtxMgr:
                def __init__(self, dsn):
                    self._dsn = dsn
                    self._conn = None
                async def __aenter__(self):
                    self._conn = psycopg.connect(self._dsn, autocommit=False)
                    return _ConnWrap(self._conn)
                async def __aexit__(self, *a):
                    if self._conn:
                        self._conn.commit()
                        self._conn.close()

            class _ConnWrap:
                def __init__(self, c):
                    self._c = c
                def cursor(self):
                    return _CurCtx(self._c)

            class _CurCtx:
                def __init__(self, c):
                    self._c = c
                async def __aenter__(self):
                    return _CurAdapt(self._c.cursor())
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

            pool = SyncPool(self._dsn)
            pub = PgOnlyPublisher()

            if tool_name == "curlyos_recall":
                import math
                query = args.get("query", "")
                k = min(int(args.get("k", 6)), 20)
                mode = args.get("mode", "fast")
                embedder = self._get_embedder()
                reranker = FakeReranker()
                result = self._run_async(mem_retrieve(
                    RetrievalRequest(query=query, scope=self._scope_text, mode=mode),
                    pool=pool, embedder=embedder, reranker=reranker,
                ))
                items = []
                for i in result.items[:k]:
                    score = i.score
                    if isinstance(score, float) and (math.isnan(score) or math.isinf(score)):
                        score = 0.0
                    items.append({
                        "id": i.id,
                        "text": i.text[:300],
                        "score": round(score, 4),
                        "tier": i.tier,
                        "epistemic_status": i.epistemic_status,
                    })
                return json.dumps({"results": items, "count": len(items), "used_tokens": result.used_tokens})

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
                result = self._run_async(invalidate(pool, pub, self._scope_text,
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

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "", **kwargs) -> None:
        if not self._auto_record or not self._dsn:
            return
        self._turn_count += 1
        if len(user_content) < 20 and len(assistant_content) < 50:
            return
        try:
            from memory.governance import record_episode
            from shared.events.implementations import PgOnlyPublisher
            import psycopg

            class SyncPool:
                def __init__(self, dsn): self._dsn = dsn
                def connection(self): return _CtxMgr2(self._dsn)
            class _CtxMgr2:
                def __init__(self, dsn): self._dsn = dsn; self._conn = None
                async def __aenter__(self):
                    self._conn = psycopg.connect(self._dsn, autocommit=False)
                    return _CW2(self._conn)
                async def __aexit__(self, *a):
                    if self._conn: self._conn.commit(); self._conn.close()
            class _CW2:
                def __init__(self, c): self._c = c
                def cursor(self): return _CC2(self._c)
            class _CC2:
                def __init__(self, c): self._c = c
                async def __aenter__(self): return _CA2(self._c.cursor())
                async def __aexit__(self, *a): pass
            class _CA2:
                def __init__(self, c): self._c = c
                async def execute(self, q, p=None): self._c.execute(q, p)
                async def fetchone(self): return self._c.fetchone()
                async def fetchall(self): return self._c.fetchall()

            pool = SyncPool(self._dsn)
            pub = PgOnlyPublisher()
            episode_content = f"[turn {self._turn_count}] User: {user_content[:800]}\n\nAssistant: {assistant_content[:1200]}"
            epi = self._run_async(record_episode(pool, pub, self._scope_text,
                content=episode_content[:3000], source_ref=f"hermes:{self._session_id}"))
            if epi and not self._session_episode_id:
                self._session_episode_id = epi["epi_id"]
        except Exception as e:
            logger.debug("CurlyOS sync_turn failed: %s", e)

    def on_session_switch(self, new_session_id: str, *, reset: bool = False, **kwargs) -> None:
        self._session_id = new_session_id
        if reset:
            self._session_episode_id = None
            self._turn_count = 0

    def shutdown(self) -> None:
        if self._consolidation_scheduler:
            self._consolidation_scheduler.stop()


def register(ctx) -> None:
    ctx.register_memory_provider(CurlyOSMemoryProvider())
