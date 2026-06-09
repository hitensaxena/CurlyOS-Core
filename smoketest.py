"""Smoketest — end-to-end memory lifecycle test.

Tests:
  1. record_episode → add → retrieve (basic write + read)
  2. Invalidate a fact (soft-invalidate, never delete)
  3. Bi-temporal "true at T" query
  4. Consolidation: embed memories from events
  5. Identity facts: propose + conflict resolution
  6. Retrieval — hybrid search
  7. Scope isolation
"""
import asyncio
import sys
import os
import json
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shared.types.ulid import mint, is_valid
from shared.types import RetrievalRequest, RankWeights
from shared.events.implementations import PgOnlyPublisher
from shared.embeddings.implementations import FakeEmbedder, FakeReranker

import psycopg

DSN = "postgresql://curlyos:***@localhost:54321/curlyos"
SCOPE = "user:usr_test001"


class SyncPool:
    """Minimal async pool wrapper for psycopg connections."""
    def __init__(self, dsn: str):
        self._dsn = dsn

    def connection(self):
        return _CtxMgr(self._dsn)


class _CtxMgr:
    def __init__(self, dsn):
        self._dsn = dsn
        self._conn = None

    async def __aenter__(self):
        self._conn = psycopg.connect(self._dsn, autocommit=False)
        return _SyncConnWrapper(self._conn)

    async def __aexit__(self, *args):
        if self._conn:
            self._conn.commit()
            self._conn.close()


class _SyncConnWrapper:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return _SyncCursorCtx(self._conn)


class _SyncCursorCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        self._cur = self._conn.cursor()
        return _AsyncCursorAdapter(self._cur)

    async def __aexit__(self, *args):
        pass


class _AsyncCursorAdapter:
    def __init__(self, cur):
        self._cur = cur

    async def execute(self, query, params=None):
        self._cur.execute(query, params)

    async def fetchone(self):
        return self._cur.fetchone()

    async def fetchall(self):
        return self._cur.fetchall()


class FakeRedis:
    def __init__(self):
        self._data = {}

    async def set(self, key, value, nx=False, px=None):
        if nx and key in self._data:
            return False
        self._data[key] = value
        return True

    async def get(self, key):
        v = self._data.get(key)
        return v if isinstance(v, (str, bytes)) else v

    async def delete(self, *keys):
        for k in keys:
            self._data.pop(k, None)

    async def hset(self, name, key, value):
        if name not in self._data:
            self._data[name] = {}
        self._data[name][key] = value

    async def hdel(self, name, *keys):
        h = self._data.get(name, {})
        for k in keys:
            h.pop(k, None)

    async def scan_iter(self, match=None):
        import fnmatch
        for k in self._data:
            if match is None or fnmatch.fnmatch(k, match):
                yield k

    async def keys(self, pattern=None):
        import fnmatch
        if pattern:
            return [k for k in self._data if fnmatch.fnmatch(k, pattern)]
        return list(self._data.keys())


async def main():
    from memory.governance import (
        record_episode, add, invalidate, list_memories, list_episodes,
        SourceEpisodeNotFound, AlreadyInvalidated,
    )
    from memory.retrieval import retrieve
    from memory.consolidation import run_once
    from identity import propose_identity_fact, get_identity_context

    pool = SyncPool(DSN)
    publisher = PgOnlyPublisher()
    embedder = FakeEmbedder()
    reranker = FakeReranker()
    redis = FakeRedis()

    passed = 0
    failed = 0

    def check(label, condition, detail=""):
        nonlocal passed, failed
        if condition:
            passed += 1
            print(f"  ✅ {label}")
        else:
            failed += 1
            print(f"  ❌ {label} — {detail}")

    # ═══════════════════════════════════════════════════════════════════
    # 1. record_episode + add — basic write path
    # ═══════════════════════════════════════════════════════════════════
    print("\n[1] record_episode + add — basic write path")

    # Set Zed fact's valid_from in the past for bi-temporal test later
    past_time = datetime.now(timezone.utc) - timedelta(days=7)

    epi = await record_episode(pool, publisher, SCOPE,
        content="Hiten switched from VS Code to Zed for all development work.",
        source_ref="smoketest")
    check("episode recorded", bool(epi.get("epi_id")))
    check("episode ID valid", is_valid("epi", epi["epi_id"]), epi["epi_id"])

    fact = await add(pool, publisher, SCOPE,
        statement="Hiten's primary code editor is Zed",
        source_episode_id=epi["epi_id"],
        valid_from=past_time)  # Set in the past for time-travel
    check("fact added", bool(fact.get("mem_id")))
    check("fact ID valid", is_valid("mem", fact["mem_id"]), fact["mem_id"])
    check("fact valid_from set", fact.get("valid_from") is not None)

    # Ungrounded fact should fail
    try:
        await add(pool, publisher, SCOPE,
            statement="Bad fact", source_episode_id="epi_INVALID")
        check("ungrounded fact rejected", False, "should have raised")
    except SourceEpisodeNotFound:
        check("ungrounded fact rejected", True)

    # ═══════════════════════════════════════════════════════════════════
    # 2. Invalidate — soft-invalidate, never delete
    # ═══════════════════════════════════════════════════════════════════
    print("\n[2] Invalidate — soft-invalidate, never delete")

    epi2 = await record_episode(pool, publisher, SCOPE,
        content="Hiten switched back to VS Code after trying Zed for two weeks.")
    fact2 = await add(pool, publisher, SCOPE,
        statement="Hiten's primary code editor is VS Code",
        source_episode_id=epi2["epi_id"])

    inv = await invalidate(pool, publisher, SCOPE,
        mem_id=fact["mem_id"],
        superseded_by=fact2["mem_id"],
        reason="Superseded by newer observation")
    check("fact invalidated", inv.get("valid_to") is not None, str(inv))
    check("never deleted", inv.get("deleted") is False)

    try:
        await invalidate(pool, publisher, SCOPE, mem_id=fact["mem_id"])
        check("double-invalidate rejected", False)
    except AlreadyInvalidated:
        check("double-invalidate rejected", True)

    current = await list_memories(pool, SCOPE)
    check("only current facts returned", len(current) >= 1)
    check("invalidated fact not in current list",
          all(m["id"] != fact["mem_id"] for m in current))

    # ═══════════════════════════════════════════════════════════════════
    # 3. Bi-temporal "true at T" query
    # ═══════════════════════════════════════════════════════════════════
    print("\n[3] Bi-temporal — time-travel query")

    conn = psycopg.connect(DSN, autocommit=True)

    # "True now" — only VS Code
    true_now = conn.execute(
        "SELECT id, statement FROM memories "
        "WHERE scope = %s AND valid_from <= now() AND (valid_to IS NULL OR valid_to > now())",
        [SCOPE]).fetchall()
    check("true-now returns only VS Code",
          any("VS Code" in r[1] for r in true_now) and not any("Zed" in r[1] for r in true_now),
          f"found: {[r[1] for r in true_now]}")

    # "True at T" — 3 days ago, Zed was still valid
    three_days_ago = datetime.now(timezone.utc) - timedelta(days=3)
    true_at_past = conn.execute(
        "SELECT id, statement FROM memories "
        "WHERE scope = %s AND valid_from <= %s AND (valid_to IS NULL OR valid_to > %s)",
        [SCOPE, three_days_ago, three_days_ago]).fetchall()
    check("true-at-T returns Zed (before switch back)",
          any("Zed" in r[1] for r in true_at_past),
          f"found: {[r[1] for r in true_at_past]}")

    # Both facts exist in the table (never deleted)
    all_facts = conn.execute(
        "SELECT count(*) FROM memories WHERE scope = %s", [SCOPE]).fetchone()
    check("both facts retained (never deleted)", all_facts[0] >= 2, f"count: {all_facts[0]}")
    conn.close()

    # ═══════════════════════════════════════════════════════════════════
    # 4. Consolidation — embed memories from events
    # ═══════════════════════════════════════════════════════════════════
    print("\n[4] Consolidation — projection worker")

    # Run with replay=True to rebuild from event log
    result = await run_once(pool, redis, embedder, publisher, scope=SCOPE, replay=True, live=False)
    check("consolidation ran", result.get("scopes") is not None, str(result))
    if result.get("scopes"):
        scope_result = result["scopes"][0]
        check("events processed", scope_result.get("processed", 0) > 0, str(scope_result))
        check("memories embedded or invalidated",
              scope_result.get("embedded", 0) + scope_result.get("invalidated", 0) + scope_result.get("episode", 0) > 0,
              str(scope_result))

    # ═══════════════════════════════════════════════════════════════════
    # 5. Identity facts — propose + conflict resolution
    # ═══════════════════════════════════════════════════════════════════
    print("\n[5] Identity facts — propose + conflict + get_context")

    idf1 = await propose_identity_fact(pool, publisher, SCOPE,
        predicate="prefers_editor", object="Zed", confidence=0.70,
        source_episode_id=epi["epi_id"])
    check("identity fact proposed", idf1.get("id") is not None, str(idf1))

    # Higher-confidence should supersede
    idf2 = await propose_identity_fact(pool, publisher, SCOPE,
        predicate="prefers_editor", object="VS Code", confidence=0.90,
        source_episode_id=epi2["epi_id"])
    check("higher-confidence identity accepted", idf2.get("id") is not None, str(idf2))

    ctx = await get_identity_context(pool, SCOPE, predicates=["prefers_editor"])
    check("identity context returned", bool(ctx), str(ctx))
    check("high-confidence editor returned",
          ctx.get("prefers_editor", {}).get("object") == "VS Code",
          str(ctx))

    # ═══════════════════════════════════════════════════════════════════
    # 6. Retrieval — hybrid search
    # ═══════════════════════════════════════════════════════════════════
    print("\n[6] Retrieval — hybrid search")

    result = await retrieve(
        RetrievalRequest(query="What editor does Hiten use?", scope=SCOPE, token_budget=2000),
        pool=pool, embedder=embedder, reranker=reranker, redis=redis,
    )
    check("retrieval returned results", len(result.items) > 0, f"items: {len(result.items)}")
    check("retrieval tokens tracked", result.used_tokens > 0)
    check("retrieval cache key set", bool(result.cache_key))

    # ═══════════════════════════════════════════════════════════════════
    # 7. Scope isolation
    # ═══════════════════════════════════════════════════════════════════
    print("\n[7] Scope isolation")

    other_scope = "user:usr_other999"
    epi_other = await record_episode(pool, publisher, other_scope,
        content="Other user uses Neovim.")
    fact_other = await add(pool, publisher, other_scope,
        statement="Other user's editor is Neovim",
        source_episode_id=epi_other["epi_id"])

    hiten_memories = await list_memories(pool, SCOPE)
    check("cross-scope isolation",
          all(m["id"] != fact_other["mem_id"] for m in hiten_memories))

    # ═══════════════════════════════════════════════════════════════════
    # Summary
    # ═══════════════════════════════════════════════════════════════════
    total = passed + failed
    print(f"\n{'='*60}")
    print(f"SMOKETEST: {passed}/{total} passed, {failed} failed")
    print(f"{'='*60}")
    return failed == 0


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
