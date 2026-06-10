"""CurlyOS MemoryProvider for Hermes Agent — HTTP-only transport.

Every operation goes through the curlyos-core API on :8643 (systemd unit
`curlyos-api`). The plugin holds no database connection and imports no
curlyos-core code: the API's venv owns the embedding model, the governance
write path, and the event log. This venv (hermes-agent) only needs stdlib.

Exposes register(ctx) which calls ctx.register_memory_provider().
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "http://127.0.0.1:8643"

# ---------------------------------------------------------------------------
# HTTP transport
# ---------------------------------------------------------------------------


def _api_url() -> str:
    return os.environ.get("CURLYOS_API_URL", DEFAULT_API_URL).rstrip("/")


def _api(method: str, path: str, payload: Optional[dict] = None,
         params: Optional[dict] = None, timeout: float = 30.0) -> dict:
    """One round-trip to curlyos-core. HTTP errors raise RuntimeError carrying
    the server's detail message — 404/409 are meaningful (not found / already
    decided) and get relayed to the model verbatim."""
    url = _api_url() + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("detail", "")
        except Exception:
            detail = ""
        raise RuntimeError(f"HTTP {e.code} {path}: {detail or e.reason}") from None


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

PENDING_APPROVALS_SCHEMA = {
    "name": "curlyos_pending_approvals",
    "description": (
        "List CurlyOS approvals waiting on Hiten — agent actions parked until a "
        "human grants or denies them. Use when Hiten asks what's pending, or to "
        "find the apv_ id before curlyos_approve / curlyos_deny."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

APPROVE_SCHEMA = {
    "name": "curlyos_approve",
    "description": (
        "Grant a pending CurlyOS approval — the parked agent run resumes and "
        "executes the gated action. ONLY call this when Hiten explicitly says to "
        "approve/grant (e.g. 'approve apv_…'). Never approve on your own judgment. "
        "If Hiten says 'approve' without an id, list pending approvals first and "
        "confirm which one he means unless exactly one is pending."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "apv_id": {"type": "string", "description": "Approval id (apv_…)."},
        },
        "required": ["apv_id"],
    },
}

DENY_SCHEMA = {
    "name": "curlyos_deny",
    "description": (
        "Deny a pending CurlyOS approval — the parked agent run resumes, records "
        "the denial, and skips the gated action. ONLY call this when Hiten "
        "explicitly says to deny/reject."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "apv_id": {"type": "string", "description": "Approval id (apv_…)."},
            "reason": {"type": "string", "description": "Why it was denied (optional)."},
        },
        "required": ["apv_id"],
    },
}


# ---------------------------------------------------------------------------
# CurlyOS MemoryProvider
# ---------------------------------------------------------------------------

class CurlyOSMemoryProvider(MemoryProvider):
    """HTTP client over the curlyos-core API (:8643)."""

    def __init__(self):
        self._session_id = ""
        self._scope_text = "user:usr_hiten"
        self._turn_count = 0
        self._consolidation_turn_count = 0
        self._auto_record = True
        self._prefetch_result = ""
        self._prefetch_lock = threading.Lock()
        self._available: Optional[bool] = None
        self._available_at = 0.0

    @property
    def name(self) -> str:
        return "curlyos"

    def is_available(self) -> bool:
        """Probe /api/health (cached 60s). The API being down means no memory
        this session — honest, and the only mode HTTP-only transport has."""
        now = time.monotonic()
        if self._available is not None and now - self._available_at < 60:
            return self._available
        try:
            _api("GET", "/api/health", timeout=2)
            self._available = True
        except Exception as e:
            logger.debug("CurlyOS API unreachable at %s: %s", _api_url(), e)
            self._available = False
        self._available_at = now
        return self._available

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        self._turn_count = 0
        self._consolidation_turn_count = 0
        # Single-user deployment: all memory is owned by Hiten. The gateway
        # passes the platform user_id (e.g. the numeric Telegram ID), which
        # would fork reads/writes into an empty phantom scope. Pin to a
        # canonical user so memory is shared across CLI/TUI/Telegram/etc.
        # Override with CURLYOS_CANONICAL_USER to opt out of this behavior.
        platform_user_id = kwargs.get("user_id", "") or kwargs.get("user_id_alt", "")
        canonical_user = os.environ.get("CURLYOS_CANONICAL_USER", "hiten")
        user_id = canonical_user or platform_user_id or "hiten"
        self._scope_text = f"user:usr_{user_id}"
        logger.info("CurlyOS provider initialized: scope=%s session=%s api=%s reachable=%s",
                    self._scope_text, session_id, _api_url(), self.is_available())

    def system_prompt_block(self) -> str:
        return (
            "# CurlyOS Memory (Bi-Temporal Knowledge Graph)\n"
            "Active. Episodic provenance + semantic facts + hybrid retrieval.\n"
            "- curlyos_recall: retrieve facts, episodes, identity context\n"
            "- curlyos_add_fact: store a bi-temporal fact (grounded in episode)\n"
            "- curlyos_add_note: store longer notes / reference material\n"
            "- curlyos_invalidate: soft-invalidate outdated facts (never deletes)\n"
            "- curlyos_identity: query Hiten's stable self-model\n"
            "- curlyos_pending_approvals / curlyos_approve / curlyos_deny: agent "
            "actions parked for Hiten's sign-off ('CurlyOS run … needs approval' "
            "messages). Grant or deny ONLY on Hiten's explicit instruction.\n"
            "Facts are invalidated-not-deleted. Always check curlyos_recall first."
        )

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [RECALL_SCHEMA, ADD_FACT_SCHEMA, ADD_NOTE_SCHEMA, INVALIDATE_SCHEMA,
                IDENTITY_SCHEMA, PENDING_APPROVALS_SCHEMA, APPROVE_SCHEMA, DENY_SCHEMA]

    def get_config_schema(self):
        return [
            {"key": "api_url", "description": "curlyos-core API base URL",
             "env_var": "CURLYOS_API_URL", "default": DEFAULT_API_URL},
        ]

    # ── write helper ─────────────────────────────────────────────────────────

    def _ingest(self, text: str, source_ref: str, *, add_memory: bool = True,
                extract_knowledge: bool = True, kind: str = "fact",
                epistemic_status: str = "canonical") -> dict:
        """Record via the governance path: episode + (optionally) a recallable
        memory + background knowledge extraction. Returns {epi_id, mem_id?} or
        {error} — /api/ingest reports write failures in-band, not as HTTP errors."""
        return _api("POST", "/api/ingest", {
            "text": text, "source_ref": source_ref, "scope": self._scope_text,
            "add_memory": add_memory, "extract_knowledge": extract_knowledge,
            "kind": kind, "epistemic_status": epistemic_status,
        })

    # ── tools ────────────────────────────────────────────────────────────────

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        try:
            if tool_name == "curlyos_recall":
                return self._tool_recall(args)

            elif tool_name == "curlyos_add_fact":
                statement = args.get("statement", "")
                res = self._ingest(statement, f"hermes:{self._session_id}:fact")
                if res.get("error"):
                    return tool_error(f"CurlyOS error: {res['error']}")
                return json.dumps({"result": "Fact stored.", "id": res.get("mem_id"),
                                   "episode": res.get("epi_id")})

            elif tool_name == "curlyos_add_note":
                content = args.get("content", "")
                title = args.get("title", "")
                text = f"{title}\n\n{content}" if title else content
                res = self._ingest(text, f"hermes:{self._session_id}:note",
                                   kind="procedure")
                if res.get("error"):
                    return tool_error(f"CurlyOS error: {res['error']}")
                return json.dumps({"result": "Note stored.", "id": res.get("mem_id")})

            elif tool_name == "curlyos_invalidate":
                mem_id = args.get("mem_id", "")
                reason = args.get("reason", "")
                res = _api("POST", f"/api/memories/{mem_id}/invalidate",
                           {"reason": reason}, timeout=15)
                return json.dumps({"result": "Fact invalidated.",
                                   "valid_to": res.get("valid_to", "")})

            elif tool_name == "curlyos_identity":
                predicates = args.get("predicates")
                params = {"predicates": ",".join(predicates)} if predicates else None
                data = _api("GET", "/api/identity", params=params, timeout=15)
                identity = [
                    {"predicate": it.get("predicate"), "object": it.get("object"),
                     "confidence": it.get("confidence")}
                    for it in data.get("items", [])
                ]
                return json.dumps({"identity": identity, "count": len(identity)})

            elif tool_name == "curlyos_pending_approvals":
                data = _api("GET", "/api/approvals", timeout=15)
                items = [
                    {"apv_id": it.get("apv_id"), "action_class": it.get("action_class"),
                     "origin": it.get("origin"), "run_id": it.get("run_id"),
                     "payload": json.dumps(it.get("payload") or {})[:300],
                     "expires_at": it.get("expires_at")}
                    for it in data.get("items", [])
                ]
                return json.dumps({"pending": items, "count": len(items)})

            elif tool_name == "curlyos_approve":
                apv_id = (args.get("apv_id") or "").strip()
                if not apv_id.startswith("apv_"):
                    return tool_error(
                        "curlyos_approve needs an explicit apv_… id — "
                        "use curlyos_pending_approvals to find it")
                data = _api("POST", f"/api/approvals/{apv_id}/grant", timeout=30)
                return json.dumps({"result": "Approval granted.",
                                   "apv_id": data.get("apv_id"),
                                   "action_class": data.get("action_class"),
                                   "run_id": data.get("run_id"),
                                   "run_resumed": data.get("resumed", False)})

            elif tool_name == "curlyos_deny":
                apv_id = (args.get("apv_id") or "").strip()
                if not apv_id.startswith("apv_"):
                    return tool_error(
                        "curlyos_deny needs an explicit apv_… id — "
                        "use curlyos_pending_approvals to find it")
                data = _api("POST", f"/api/approvals/{apv_id}/deny",
                            {"reason": (args.get("reason") or "user_denied")[:500]},
                            timeout=30)
                return json.dumps({"result": "Approval denied.",
                                   "apv_id": data.get("apv_id"),
                                   "action_class": data.get("action_class"),
                                   "run_id": data.get("run_id"),
                                   "run_resumed": data.get("resumed", False)})

            return tool_error(f"Unknown tool: {tool_name}")
        except Exception as e:
            logger.error("CurlyOS tool error: %s", e, exc_info=True)
            return tool_error(f"CurlyOS error: {e}")

    def _tool_recall(self, args: dict) -> str:
        query = args.get("query", "")
        k = min(int(args.get("k", 6)), 20)
        mode = args.get("mode", "fast")
        try:
            data = _api("POST", "/api/recall", {
                "query": query, "scope": self._scope_text, "mode": mode, "k": k})
            if data.get("error"):
                raise RuntimeError(data["error"])
            return json.dumps({
                "results": data.get("results", [])[:k],
                "count": data.get("count", len(data.get("results", []))),
            })
        except Exception as e:
            # Fallback: keyword full-text search (no embedding involved).
            try:
                data = _api("GET", "/api/search",
                            params={"q": query, "scope": self._scope_text, "limit": k},
                            timeout=15)
                items = [{
                    "id": it.get("id"),
                    "text": (it.get("statement") or "")[:200],
                    "score": it.get("score", 0.0),
                    "tier": it.get("kind", "fact"),
                    "epistemic_status": it.get("epistemic_status", "canonical"),
                } for it in data.get("items", [])]
                return json.dumps({"results": items, "count": len(items),
                                   "fallback": "keyword"})
            except Exception as e2:
                return json.dumps({
                    "error": f"recall via API failed: {e}; "
                             f"keyword fallback failed: {e2}",
                    "results": [], "count": 0})

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
        try:
            with self._prefetch_lock:
                return self._prefetch_result
        except Exception as e:
            logger.debug("prefetch cache read failed: %s", e)
        return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Queue a background recall for the NEXT turn.

        Called after each turn completes. Starts a background thread that
        calls /api/recall and stores the formatted result in
        self._prefetch_result, which prefetch() will read at the start
        of the next turn.
        """
        if not query.strip():
            return

        def _background_recall():
            try:
                data = _api("POST", "/api/recall", {
                    "query": query, "scope": self._scope_text,
                    "mode": "fast", "k": 6})
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
        self._turn_count += 1
        logger.debug("CurlyOS on_turn_start: turn=%d turn_count=%d", turn_number, self._turn_count)

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Called when a session ends (explicit exit or timeout).

        Extracts key facts from user messages, records a session-end
        episode, proposes identity facts, and triggers fast-path
        consolidation.
        """
        if not messages:
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
            epi = self._ingest(summary[:2000],
                               f"hermes:{self._session_id}:session_end",
                               add_memory=False, extract_knowledge=False)
            epi_id = epi.get("epi_id")

            # Identity facts go through the governance path (conflict
            # resolution + supersession live in the identity module).
            for predicate, obj, tag in unique_idf:
                try:
                    _api("POST", "/api/identity", {
                        "predicate": predicate, "object": obj,
                        "confidence": 0.8,
                        "source_episode_id": epi_id or ""}, timeout=20)
                except Exception as e:
                    logger.debug("Failed to propose identity fact (%s=%s): %s", predicate, obj, e)

            # Write semantic facts
            for stmt in semantic_facts:
                try:
                    self._ingest(stmt, f"hermes:{self._session_id}:session_end",
                                 extract_knowledge=False)
                except Exception as e:
                    logger.debug("Failed to write semantic fact: %s", e)

            logger.info("CurlyOS session-end: recorded episode, wrote %d identity + %d semantic facts",
                        len(unique_idf), len(semantic_facts))
        except Exception as e:
            logger.warning("CurlyOS on_session_end episode/fact write failed: %s", e)

        # ── Step 4: Trigger fast-path consolidation in background ──
        self._trigger_async_consolidation()

    def _trigger_async_consolidation(self):
        """Run fast-path consolidation via the API in a background thread."""
        def _run():
            try:
                res = _api("POST", "/api/consolidation/run",
                           {"mode": "fast", "scope": self._scope_text}, timeout=300)
                scopes = res.get("scopes", [])
                total_processed = sum(
                    s.get("projection", {}).get("processed", 0) for s in scopes
                ) if isinstance(scopes, list) else 0
                logger.info("CurlyOS async consolidation complete: %d events processed across %d scopes",
                            total_processed, len(scopes) if isinstance(scopes, list) else 0)
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
        if not messages:
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

            # Each insight is its own episode+hypothesis memory (cap 5).
            # Hypothesis status keeps them out of default recall.
            for insight in insights[:5]:
                try:
                    self._ingest(insight,
                                 f"hermes:{self._session_id}:pre_compress",
                                 extract_knowledge=False,
                                 epistemic_status="hypothesis")
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
        if not content.strip():
            return

        try:
            if action == "add":
                if target == "memory":
                    self._ingest(content, "hermes:builtin_memory",
                                 extract_knowledge=False)
                    logger.debug("CurlyOS on_memory_write: stored semantic fact (%d chars)", len(content))

                elif target == "user":
                    # /api/identity records its own provenance episode when
                    # source_episode_id is empty.
                    _api("POST", "/api/identity", {
                        "predicate": "stated_preference", "object": content,
                        "confidence": 0.9, "source_episode_id": ""}, timeout=20)
                    logger.debug("CurlyOS on_memory_write: stored identity fact (%d chars)", len(content))

            elif action == "replace":
                # Invalidate old (by content match) and add new
                metadata = metadata or {}
                old_content = metadata.get("old_content", "")
                if old_content:
                    self._invalidate_by_content(old_content,
                                                "Replaced via built-in memory tool")
                self._ingest(content, "hermes:builtin_memory",
                             extract_knowledge=False)
                logger.debug("CurlyOS on_memory_write: replaced memory fact")

            elif action == "remove":
                if self._invalidate_by_content(content,
                                               "Removed via built-in memory tool"):
                    logger.debug("CurlyOS on_memory_write: invalidated memory fact")

        except Exception as e:
            logger.warning("CurlyOS on_memory_write failed (non-fatal): %s", e)

    def _invalidate_by_content(self, content: str, reason: str) -> bool:
        """Find a current memory whose statement contains the given content
        (keyword search) and soft-invalidate it. Best-effort."""
        try:
            data = _api("GET", "/api/search",
                        params={"q": content[:80], "scope": self._scope_text,
                                "limit": 5}, timeout=15)
            for it in data.get("items", []):
                stmt = it.get("statement") or ""
                if it.get("id") and content[:80] in stmt:
                    _api("POST", f"/api/memories/{it['id']}/invalidate",
                         {"reason": reason}, timeout=15)
                    return True
        except Exception as e:
            logger.debug("Failed to invalidate by content match: %s", e)
        return False

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
        logger.info("CurlyOS on_delegation: child_session=%s task=%.60s", child_session_id or "?", task)

        try:
            # Record delegation episode
            epi_content = (
                f"Delegated task (child_session={child_session_id}):\n"
                f"{task[:500]}\n\n"
                f"Result:\n{result[:1000]}"
            )
            self._ingest(epi_content[:2000],
                         f"hermes:delegation:{child_session_id}",
                         add_memory=False, extract_knowledge=False)

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
                    self._ingest(f"[Delegation {kind}] {stmt}",
                                 f"hermes:delegation:{child_session_id}",
                                 extract_knowledge=False,
                                 epistemic_status="hypothesis")
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
        if not self._auto_record:
            return
        self._turn_count += 1
        self._consolidation_turn_count += 1
        if len(user_content) < 20 and len(assistant_content) < 50:
            return
        try:
            episode_content = f"[turn {self._turn_count}] User: {user_content[:800]}\n\nAssistant: {assistant_content[:1200]}"
            self._ingest(episode_content[:3000], f"hermes:{self._session_id}",
                         add_memory=False, extract_knowledge=False)
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
            self._turn_count = 0
            self._consolidation_turn_count = 0

    def shutdown(self) -> None:
        pass


def register(ctx) -> None:
    ctx.register_memory_provider(CurlyOSMemoryProvider())
