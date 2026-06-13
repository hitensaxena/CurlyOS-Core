"""CurlyOS API server — serves memory, knowledge graph, identity, cognition data.

Runs as a FastAPI app on port 8643. Called by Next.js API routes or directly.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Literal

import psycopg
import psycopg.rows
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DSN = os.environ.get("CURLYOS_DATABASE_URL", "postgresql://curlyos:***@localhost:54321/curlyos")
REDIS_URL = os.environ.get("CURLYOS_REDIS_URL", "")
SCOPE = os.environ.get("CURLYOS_SCOPE", "user:usr_hiten")

# Root logging config: module loggers (memory/, knowledge/, cognition/) and
# our own logger all flow to stderr → journald, independent of uvicorn's
# --log-level (which only governs uvicorn's own loggers).
logging.basicConfig(
    level=os.environ.get("CURLYOS_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("curlyos.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Self-heal: re-embed anything a crash/restart left without embeddings.
    sweep = asyncio.create_task(_sweep_unembedded())

    # The cognitive heartbeat (curlyos-final/06 §3) — replaces Hermes cron.
    # CURLYOS_SCHEDULER=0 disables (tests, one-off scripts).
    scheduler = None
    if os.environ.get("CURLYOS_SCHEDULER", "1").lower() not in ("0", "false", "off"):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from orchestration.scheduler import Scheduler
        from shared.notify import get_notifier

        scheduler = Scheduler(
            _scheduler_jobs(),
            scope=SCOPE,
            pool_factory=lambda: _get_async_pool(row_factory=psycopg.rows.tuple_row),
            publisher_factory=_make_publisher_sync,
            redis_factory=_make_redis,
            notifier=get_notifier(),
        )
        scheduler.start()
    app.state.scheduler = scheduler

    # The Executive runner (Phase A) — LangGraph + Postgres checkpointer.
    # CURLYOS_RUNNER=0 disables (tests, machines without the orchestration extra).
    runner = None
    if os.environ.get("CURLYOS_RUNNER", "1").lower() not in ("0", "false", "off"):
        try:
            from orchestration.runner import Runner
            from shared.notify import get_notifier as _gn

            runner = Runner(
                dsn=DSN,
                scope=SCOPE,
                pool_factory=lambda: _get_async_pool(row_factory=psycopg.rows.tuple_row),
                publisher_factory=_make_publisher_sync,
                redis_factory=_make_redis,
                embedder_factory=get_shared_embedder,
                llm=_runner_llm(),
                notifier=_gn(),
            )
            await runner.start()
        except Exception:
            logger.exception("runner failed to start — agent runs disabled")
            runner = None
    app.state.runner = runner

    yield
    if runner is not None:
        await runner.stop()
    if scheduler is not None:
        await scheduler.stop()
    sweep.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await sweep
    for pool in _POOLS.values():
        await pool.close()
    _POOLS.clear()


app = FastAPI(title="CurlyOS API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    # Server-side clients (Next.js proxy, voice, hermes) ignore CORS; this only
    # gates direct browser access. Local dev ports + the prod webapp origin.
    allow_origins=[
        "http://localhost:3000", "http://127.0.0.1:3000",
        "http://localhost:3100", "http://127.0.0.1:3100",
        "https://os.curlybrackets.art",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


# Goal OS router (Phase G) — the first router split out of this file; new
# endpoint groups go in <package>/api.py from here on (curlyos-final/03 §6).
def _include_goal_router() -> None:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from goals.api import make_router

    app.include_router(make_router(
        pool_factory=lambda: _get_async_pool(row_factory=psycopg.rows.tuple_row),
        publisher_factory=lambda: _make_publisher_sync(),
        scope=SCOPE,
    ))


_include_goal_router()


def _runner_llm():
    """The runner's LLM seam: async (system, user) -> text over the shared
    OpenRouter client, or None (graph falls back to deterministic planning)."""
    client, model = _make_llm_client()
    if client is None:
        return None

    async def llm(system: str, user: str) -> str:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            temperature=0.2,
            max_tokens=1500,
        )
        return resp.choices[0].message.content or ""

    return llm


def _include_agents_router() -> None:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from orchestration.api import make_router as make_agents_router

    app.include_router(make_agents_router(
        pool_factory=lambda: _get_async_pool(row_factory=psycopg.rows.tuple_row),
        scope=SCOPE,
        # These factories are defined further down in this module, but this
        # router is included at import time (above their defs), so wrap the bare
        # names in lambdas to defer resolution to request time — same pattern as
        # _include_goal_router(). A bare reference NameErrors at import.
        publisher_factory=lambda: _make_publisher_sync(),
        redis_factory=lambda: _make_redis(),
        embedder_factory=lambda: get_shared_embedder(),
        llm_factory=_runner_llm,
    ))


_include_agents_router()


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"error": str(exc)})


def get_conn():
    """Synchronous connection for the simple CRUD endpoints (which are plain
    `def` routes — FastAPI runs them in its threadpool, so connecting here
    never blocks the event loop). Use as a context manager: closes on exit,
    including on exceptions."""
    return psycopg.connect(DSN, row_factory=psycopg.rows.dict_row, autocommit=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Request models (validated bodies for the write endpoints)
# ---------------------------------------------------------------------------

class AddMemoryRequest(BaseModel):
    statement: str = Field(min_length=1, max_length=8000)
    source_episode_id: str = Field(min_length=1)
    kind: str = "fact"
    epistemic_status: str = "canonical"


class InvalidateRequest(BaseModel):
    reason: str = ""


class ProposeIdentityRequest(BaseModel):
    predicate: str = Field(min_length=1, max_length=200)
    object: str = Field(min_length=1, max_length=2000)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    source_episode_id: str = ""


class RecallRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    scope: str = SCOPE
    mode: Literal["fast", "deep", "divergent"] = "fast"
    k: int = Field(default=6, ge=1, le=20)


class ComposeNarrativeRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    since: str | None = None
    domain: str | None = None


class CreateStudioRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    properties: dict = Field(default_factory=dict)


class CreateSketchRequest(BaseModel):
    content: str = Field(min_length=1, max_length=20000)
    kind: str = "text"
    properties: dict = Field(default_factory=dict)


class UpdateSketchRequest(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=20000)
    epistemic_status: Literal["conjecture", "hypothesis"] | None = None


class GraduateSketchRequest(BaseModel):
    target_type: str = "project"


class CreateSimulationRunRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    world_model_id: str | None = None
    parameters: dict = Field(default_factory=dict)


class CreateWorkspaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    kind: str = "project"
    properties: dict = Field(default_factory=dict)


class CreateProjectRequest(BaseModel):
    workspace_id: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=500)
    properties: dict = Field(default_factory=dict)


class IngestRequest(BaseModel):
    text: str = Field(min_length=1, max_length=100_000)
    source_ref: str = "web:capture"
    scope: str = SCOPE
    add_memory: bool = True
    extract_knowledge: bool = True
    kind: Literal["fact", "procedure", "preference"] = "fact"
    epistemic_status: Literal["canonical", "hypothesis", "belief"] = "canonical"


class ConsolidationRunRequest(BaseModel):
    mode: Literal["fast", "deep"] = "fast"
    scope: str = SCOPE


class ReflectionRequest(BaseModel):
    scope: str = SCOPE
    window_days: int = Field(default=7, ge=1, le=90)


class MetaAuditRequest(BaseModel):
    scope: str = SCOPE
    window_days: int = Field(default=30, ge=1, le=365)


class MetaDistillRequest(BaseModel):
    scope: str = SCOPE
    min_confidence: float = Field(default=0.7, ge=0.0, le=1.0)


class NarrativeGenerateRequest(BaseModel):
    scope: str = SCOPE
    min_frequency: int = Field(default=3, ge=1)


class AttentionScanRequest(BaseModel):
    scope: str = SCOPE
    window_days: int = Field(default=14, ge=1, le=365)


# ---------------------------------------------------------------------------
# Health + Stats
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    result = {"timestamp": now_iso(), "postgres": {}, "redis": {}, "embedder": {}}

    # Postgres
    try:
        with get_conn() as conn:
            ver = conn.execute("SELECT version() AS v").fetchone()["v"]
            has_vec = conn.execute("SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone()
        result["postgres"] = {
            "ok": True,
            "version": ver[:60],
            "pgvector": bool(has_vec),
        }
    except Exception as e:
        result["postgres"] = {"ok": False, "error": str(e)}

    # Redis
    if REDIS_URL:
        try:
            import redis
            r = redis.from_url(REDIS_URL, socket_timeout=3)
            r.ping()
            info = r.info("server")
            result["redis"] = {"ok": True, "version": info.get("redis_version", "?")}
        except Exception as e:
            result["redis"] = {"ok": False, "error": str(e)}
    else:
        result["redis"] = {"ok": False, "error": "No Redis URL configured"}

    # Embedder
    try:
        from shared.embeddings import get_embedder
        emb = get_embedder()
        result["embedder"] = {"ok": True, "model": getattr(emb, "model", "unknown")}
    except Exception:
        result["embedder"] = {"ok": True, "model": "n/a"}

    return result


# Fixed allowlist of tables /api/stats may count — table names are interpolated
# into SQL, so they must only ever come from this literal tuple.
_COUNT_TABLES = ("episodes", "memories", "identity_facts", "knowledge_entities", "knowledge_edges")


@app.get("/api/stats")
def stats():
    counts = {}
    with get_conn() as conn:
        for t in _COUNT_TABLES:
            try:
                counts[t] = conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"]
            except Exception:
                counts[t] = 0
    return counts


@app.get("/api/stats/composition")
def stats_composition(scope: str = SCOPE):
    """Breakdown of memory + identity by epistemic status / tier, plus how many
    beliefs changed recently. Powers the dashboard's "state of mind" view."""
    def _group(conn, sql):
        try:
            return {r["k"]: r["n"] for r in conn.execute(sql, [scope]).fetchall() if r["k"]}
        except Exception:
            return {}

    with get_conn() as conn:
        memories_by_status = _group(
            conn,
            "SELECT epistemic_status AS k, count(*) AS n FROM memories "
            "WHERE scope = %s AND valid_to IS NULL GROUP BY epistemic_status",
        )
        memories_by_tier = _group(
            conn,
            "SELECT tier AS k, count(*) AS n FROM memories "
            "WHERE scope = %s AND valid_to IS NULL GROUP BY tier",
        )
        identity_by_status = _group(
            conn,
            "SELECT epistemic_status AS k, count(*) AS n FROM identity_facts "
            "WHERE scope = %s AND valid_to IS NULL GROUP BY epistemic_status",
        )
        try:
            changed_7d = conn.execute(
                "SELECT count(*) AS n FROM memories "
                "WHERE scope = %s AND valid_to >= now() - interval '7 days'",
                [scope],
            ).fetchone()["n"]
        except Exception:
            changed_7d = 0
    return {
        "memories_by_status": memories_by_status,
        "memories_by_tier": memories_by_tier,
        "identity_by_status": identity_by_status,
        "memories_changed_7d": changed_7d,
    }


# ---------------------------------------------------------------------------
# Memories
# ---------------------------------------------------------------------------

@app.get("/api/memories")
def list_memories(
    scope: str = SCOPE,
    kind: str | None = None,
    epistemic_status: str | None = None,
    valid: bool | None = True,
    limit: int = Query(default=50, le=200),
    offset: int = 0,
    q: str | None = None,
):
    conditions = ["scope = %s"]
    params: list[Any] = [scope]

    if kind:
        conditions.append("kind = %s")
        params.append(kind)
    if epistemic_status:
        conditions.append("epistemic_status = %s")
        params.append(epistemic_status)
    if valid is True:
        conditions.append("valid_to IS NULL")
    elif valid is False:
        conditions.append("valid_to IS NOT NULL")
    if q:
        conditions.append("to_tsvector('english', statement) @@ websearch_to_tsquery('english', %s)")
        params.append(q)

    where = " AND ".join(conditions)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM memories WHERE {where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
            params + [limit, offset],
        ).fetchall()
    return {"items": rows, "count": len(rows)}


@app.get("/api/memories/{mem_id}")
def get_memory(mem_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM memories WHERE id = %s", [mem_id]).fetchone()
        if not row:
            raise HTTPException(404, "Memory not found")
        # Get source episode
        epi = conn.execute("SELECT * FROM episodes WHERE id = %s", [row["source_episode_id"]]).fetchone()
        # Get superseded_by memory if any
        sup = None
        if row.get("superseded_by"):
            sup = conn.execute("SELECT id, statement FROM memories WHERE id = %s", [row["superseded_by"]]).fetchone()
        # Get the memory THIS one replaced (reverse link), for version history
        prev = conn.execute(
            "SELECT id, statement FROM memories WHERE superseded_by = %s", [mem_id]
        ).fetchone()
    return {"memory": row, "source_episode": epi, "superseded_by": sup, "supersedes": prev}


@app.post("/api/memories")
def add_memory(body: AddMemoryRequest):
    try:
        with get_conn() as conn:
            row = conn.execute(
                "INSERT INTO memories (id, scope, statement, statement_key, kind, tier, "
                "epistemic_status, valid_from, ingested_at, source_episode_id) "
                "VALUES (gen_random_uuid()::text, %s, %s, %s, %s, 'semantic', %s, now(), now(), %s) "
                "RETURNING id, created_at",
                [SCOPE, body.statement, body.statement.lower().strip(), body.kind,
                 body.epistemic_status, body.source_episode_id],
            ).fetchone()
        return {"id": row["id"], "created_at": row["created_at"].isoformat() if row else None}
    except Exception as e:
        if "23503" in str(e):
            raise HTTPException(400, f"source_episode_id not found: {body.source_episode_id}")
        raise HTTPException(500, str(e))


@app.post("/api/memories/{mem_id}/invalidate")
def invalidate_memory(mem_id: str, body: InvalidateRequest | None = None):
    with get_conn() as conn:
        row = conn.execute("SELECT valid_to FROM memories WHERE id = %s", [mem_id]).fetchone()
        if not row:
            raise HTTPException(404, "Memory not found")
        if row["valid_to"] is not None:
            raise HTTPException(409, "Already invalidated")
        conn.execute("UPDATE memories SET valid_to = now() WHERE id = %s", [mem_id])
    return {"id": mem_id, "valid_to": now_iso(), "deleted": False}


# ---------------------------------------------------------------------------
# Episodes
# ---------------------------------------------------------------------------

@app.get("/api/episodes")
def list_episodes(
    scope: str = SCOPE,
    modality: str | None = None,
    limit: int = Query(default=50, le=200),
    offset: int = 0,
):
    conditions = ["scope = %s"]
    params: list[Any] = [scope]
    if modality:
        conditions.append("modality = %s")
        params.append(modality)
    where = " AND ".join(conditions)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM episodes WHERE {where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
            params + [limit, offset],
        ).fetchall()
    return {"items": rows, "count": len(rows)}


@app.get("/api/episodes/{epi_id}")
def get_episode(epi_id: str):
    with get_conn() as conn:
        epi = conn.execute("SELECT * FROM episodes WHERE id = %s", [epi_id]).fetchone()
        if not epi:
            raise HTTPException(404, "Episode not found")
        mems = conn.execute(
            "SELECT id, statement, epistemic_status, valid_from, valid_to FROM memories WHERE source_episode_id = %s",
            [epi_id],
        ).fetchall()
    return {"episode": epi, "memories": mems}


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

@app.get("/api/identity")
def list_identity(scope: str = SCOPE, predicates: str | None = None, valid: bool | None = True):
    """valid=True (default) → current facts; False → superseded; None → all.
    The webapp uses None to show a fact's history alongside the live value."""
    conditions = ["scope = %s"]
    params: list[Any] = [scope]
    if predicates:
        conditions.append("predicate = ANY(%s)")
        params.append([p.strip() for p in predicates.split(",")])
    if valid is True:
        conditions.append("valid_to IS NULL")
    elif valid is False:
        conditions.append("valid_to IS NOT NULL")
    where = " AND ".join(conditions)
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM identity_facts WHERE {where} ORDER BY confidence DESC",
            params,
        ).fetchall()
    return {"items": rows, "count": len(rows)}


@app.post("/api/identity")
async def propose_identity(body: ProposeIdentityRequest):
    """Propose an identity fact through the governance path — conflict
    resolution, supersession, and confidence gating live in the identity
    module, not here. A missing source_episode_id gets a provenance episode
    recorded first (every derived fact traces to an episode)."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from identity import propose_identity_fact
    from memory.governance import record_episode

    pool = await _get_async_pool(row_factory=psycopg.rows.tuple_row)
    pub = _make_publisher_sync()
    try:
        source_episode_id = body.source_episode_id
        if not source_episode_id:
            epi = await record_episode(
                pool, pub, SCOPE,
                content=f"Manual identity entry: {body.predicate} = {body.object}",
                source_ref="web:identity",
            )
            source_episode_id = epi["epi_id"]
        result = await propose_identity_fact(
            pool, pub, SCOPE,
            predicate=body.predicate,
            object=body.object,
            confidence=body.confidence,
            source_episode_id=source_episode_id,
        )
        result["id"] = result.get("idf_id")  # legacy response-shape alias
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# Knowledge Graph
# ---------------------------------------------------------------------------

@app.get("/api/graph")
def get_graph(scope: str = SCOPE, limit: int = Query(default=20000, le=50000)):
    # Order by full-graph degree (not recency) so the hubs are always in view
    # when the limit truncates — recency ordering buried Hiten et al. behind a
    # window of the newest leaf nodes. Default limit is set well above the live
    # graph size (was 1500, which silently truncated once the graph grew past it
    # — the webapp passes no limit and so only ever saw the top-degree 1500).
    with get_conn() as conn:
        entities = conn.execute(
            "WITH deg AS ("
            "  SELECT eid, count(*) AS d FROM ("
            "    SELECT src_entity_id AS eid FROM knowledge_edges WHERE valid_to IS NULL "
            "    UNION ALL "
            "    SELECT dst_entity_id FROM knowledge_edges WHERE valid_to IS NULL"
            "  ) x GROUP BY eid"
            ") "
            "SELECT e.id, e.name, e.label, e.properties, e.epistemic_status, "
            "       COALESCE(d.d, 0) AS degree "
            "FROM knowledge_entities e "
            "LEFT JOIN deg d ON d.eid = e.id "
            "WHERE e.scope = %s AND e.valid_to IS NULL "
            "ORDER BY degree DESC, e.created_at DESC LIMIT %s",
            [scope, limit],
        ).fetchall()
        entity_ids = [e["id"] for e in entities]
        edges = []
        if entity_ids:
            # Both directions — an inbound-only node must still show its edges.
            edges = conn.execute(
                "SELECT id, src_entity_id, dst_entity_id, rel_type, properties FROM knowledge_edges WHERE (src_entity_id = ANY(%s) OR dst_entity_id = ANY(%s)) AND valid_to IS NULL",
                [entity_ids, entity_ids],
            ).fetchall()
    # Keep only edges whose BOTH endpoints are in the returned node set so the
    # client never gets a link pointing at an off-list (truncated) node.
    _idset = set(entity_ids)
    edges = [e for e in edges if e["src_entity_id"] in _idset and e["dst_entity_id"] in _idset]

    # degree is the FULL-graph degree (from SQL), so a node shows its true
    # connectivity even when some neighbors fall outside a truncated window.
    nodes = [
        {"id": e["id"], "name": e["name"], "label": e["label"], "degree": e["degree"]}
        for e in entities
    ]
    links = [
        {"source": e["src_entity_id"], "target": e["dst_entity_id"], "rel_type": e["rel_type"]}
        for e in edges
    ]
    return {"nodes": nodes, "links": links}


@app.get("/api/graph/{entity_id}/expand")
def expand_graph(entity_id: str, k: int = Query(default=1, le=3)):
    visited = {entity_id}
    frontier = [entity_id]
    all_edges = []

    with get_conn() as conn:
        for _ in range(k):
            if not frontier:
                break
            rows = conn.execute(
                "SELECT id, src_entity_id, dst_entity_id, rel_type FROM knowledge_edges WHERE (src_entity_id = ANY(%s) OR dst_entity_id = ANY(%s)) AND valid_to IS NULL",
                [frontier, frontier],
            ).fetchall()
            next_frontier = []
            for r in rows:
                all_edges.append(r)
                for nid in [r["src_entity_id"], r["dst_entity_id"]]:
                    if nid not in visited:
                        visited.add(nid)
                        next_frontier.append(nid)
            frontier = next_frontier

        entities = []
        if visited:
            entities = conn.execute(
                "SELECT id, name, label, properties, epistemic_status FROM knowledge_entities WHERE id = ANY(%s) AND valid_to IS NULL",
                [list(visited)],
            ).fetchall()
    return {"entities": entities, "edges": all_edges}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@app.get("/api/search")
def search(q: str, scope: str = SCOPE, limit: int = Query(default=20, le=50)):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, statement, kind, valid_from, valid_to, source_episode_id, epistemic_status, "
            "ts_rank(to_tsvector('english', statement), plainto_tsquery('english', %s)) AS score "
            "FROM memories WHERE scope = %s AND valid_to IS NULL "
            "AND to_tsvector('english', statement) @@ plainto_tsquery('english', %s) "
            "ORDER BY score DESC LIMIT %s",
            [q, scope, q, limit],
        ).fetchall()
    return {"query": q, "items": rows, "count": len(rows)}


@app.post("/api/recall")
async def recall(body: RecallRequest):
    """Semantic + graph retrieval (dense pgvector + sparse + entity + graph + rerank).

    This is the authoritative recall path for the Hermes `curlyos` plugin, which
    cannot embed in-process (its venv lacks sentence_transformers). The plugin
    HTTP-calls this endpoint so embedding runs here, in the ST-capable venv.

    Body: {"query": "...", "scope": "user:usr_hiten", "mode": "fast"|"deep"|"divergent", "k": 6}
    """
    query, scope, mode, k = body.query, body.scope, body.mode, body.k

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from memory.retrieval import retrieve
    from shared.types import RetrievalRequest
    from shared.embeddings.implementations import FakeReranker

    # memory.retrieval uses positional/tuple row access → tuple_row (see consolidation).
    pool = await _get_async_pool(row_factory=psycopg.rows.tuple_row)
    try:
        emb = await get_shared_embedder()
        # Large token_budget so retrieve() returns its full candidate pool — its
        # assembler otherwise truncates by budget and can drop dense-strong items
        # that RRF rank-fusion ranked low. We re-rank the pool ourselves below;
        # the assembled context string is unused here.
        result = await retrieve(
            RetrievalRequest(query=query, scope=scope, mode=mode, token_budget=50000),
            pool=pool, embedder=emb, reranker=FakeReranker(),
        )
        cand = result.items
        # Rerank by true cosine relevance using the STORED memory embeddings:
        # embed the query once + one SQL pass over the candidate ids (NO per-doc
        # re-encoding, which is far too slow on this CPU box). This surfaces the
        # dense-relevant memories that rank-fusion buries.
        sims: dict[str, float] = {}
        if cand:
            qv = (await emb.embed([query]))[0]
            qlit = "[" + ",".join(repr(float(x)) for x in qv) + "]"
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT id, 1 - (embedding <=> %s::vector) FROM memories "
                        "WHERE id = ANY(%s) AND embedding IS NOT NULL",
                        (qlit, [it.id for it in cand]),
                    )
                    for rid, sim in await cur.fetchall():
                        sims[rid] = float(sim)
            cand = sorted(cand, key=lambda it: -sims.get(it.id, -1.0))
        items = [
            {"id": it.id, "text": it.text[:200],
             "score": round(sims.get(it.id, it.score or 0.0), 4),
             "tier": it.tier, "epistemic_status": it.epistemic_status}
            for it in cand[:k]
        ]
        return {"results": items, "count": len(items)}
    except Exception as e:
        logger.exception("recall failed query=%r", query[:80])
        return {"error": str(e), "results": [], "count": 0}


# ---------------------------------------------------------------------------
# Cognition
# ---------------------------------------------------------------------------

@app.get("/api/cognition/meta")
def cognition_meta(scope: str = SCOPE):
    with get_conn() as conn:
        principles = conn.execute(
            "SELECT id, statement, domain, epistemic_status FROM principles "
            "WHERE scope = %s AND valid_to IS NULL ORDER BY created_at DESC",
            [scope],
        ).fetchall()
        assumptions = conn.execute(
            "SELECT id, statement, domain, confidence, epistemic_status FROM assumptions WHERE scope = %s AND valid_to IS NULL ORDER BY confidence DESC",
            [scope],
        ).fetchall()
        decision_audits = conn.execute(
            "SELECT id, decision, domain, quality_score, created_at FROM decision_audits "
            "WHERE scope = %s ORDER BY created_at DESC LIMIT 50",
            [scope],
        ).fetchall()
        mental_models = conn.execute(
            "SELECT id, name, description, domain FROM mental_models "
            "WHERE scope = %s AND valid_to IS NULL ORDER BY created_at DESC",
            [scope],
        ).fetchall()
    return {
        "principles": principles,
        "assumptions": assumptions,
        "decision_audits": decision_audits,
        "mental_models": mental_models,
    }


@app.get("/api/cognition/reflection")
def cognition_reflection(scope: str = SCOPE):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM reflection_reports WHERE scope = %s ORDER BY created_at DESC LIMIT 10",
            [scope],
        ).fetchall()
    return {"reports": rows}


@app.get("/api/cognition/attention")
async def cognition_attention(scope: str = SCOPE, window_days: int = 7):
    def _fetch_gaps():
        with get_conn() as conn:
            return conn.execute(
                "SELECT id, signal_type, description, severity, epistemic_status FROM alignment_signals WHERE scope = %s AND valid_to IS NULL ORDER BY severity DESC",
                [scope],
            ).fetchall()

    gaps = await asyncio.to_thread(_fetch_gaps)

    # KG-grounded attention: focus areas (cognitive mass), neglected entities,
    # and breadth — computed live, read-only. Replaces the old ingested_at
    # keyword-allocation + fake heatmap (meaningless on a bulk-imported corpus).
    focus_areas = neglected = breadth = cognitive_load = None
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from cognition.attention import (
            get_focus_areas, get_neglected_entities, cognitive_breadth, estimate_cognitive_load,
        )
        pool = await _get_async_pool()
        focus_areas = await get_focus_areas(pool=pool, scope=scope)
        neglected = await get_neglected_entities(pool=pool, scope=scope)
        breadth = await cognitive_breadth(pool=pool, scope=scope)
        cognitive_load = await estimate_cognitive_load(pool=pool, scope=scope, window_days=14)
    except Exception:
        logger.warning("attention enrichment failed", exc_info=True)

    return {
        "alignment_gaps": gaps,
        "focus_areas": focus_areas,
        "neglected": neglected,
        "breadth": breadth,
        "cognitive_load": cognitive_load,
    }


@app.get("/api/cognition/narrative")
def cognition_narrative(scope: str = SCOPE):
    with get_conn() as conn:
        chapters = conn.execute(
            "SELECT id, title, summary, start_date, end_date, epistemic_status FROM life_chapters "
            "WHERE scope = %s AND valid_to IS NULL ORDER BY start_date DESC",
            [scope],
        ).fetchall()
        themes = conn.execute(
            "SELECT id, name, description, frequency, epistemic_status FROM themes "
            "WHERE scope = %s AND valid_to IS NULL ORDER BY frequency DESC",
            [scope],
        ).fetchall()
    return {"chapters": chapters, "themes": themes}


@app.post("/api/cognition/narrative/compose")
async def compose_narrative(body: ComposeNarrativeRequest):
    query, since, domain = body.query, body.since, body.domain

    like = f"%{query}%"

    def _fetch_material():
        # Material for the narrative: episodes/memories matching the query first,
        # then recent context so there's always something to weave from.
        with get_conn() as conn:
            if since:
                rel = conn.execute(
                    "SELECT id, content, created_at FROM episodes "
                    "WHERE scope = %s AND content ILIKE %s AND created_at >= %s "
                    "ORDER BY created_at DESC LIMIT 20",
                    [SCOPE, like, since],
                ).fetchall()
            else:
                rel = conn.execute(
                    "SELECT id, content, created_at FROM episodes "
                    "WHERE scope = %s AND content ILIKE %s ORDER BY created_at DESC LIMIT 20",
                    [SCOPE, like],
                ).fetchall()
            recent = conn.execute(
                "SELECT id, content, created_at FROM episodes WHERE scope = %s ORDER BY created_at DESC LIMIT 12",
                [SCOPE],
            ).fetchall()
            mems = conn.execute(
                "SELECT id, statement, created_at FROM memories "
                "WHERE scope = %s AND valid_to IS NULL AND statement ILIKE %s "
                "ORDER BY created_at DESC LIMIT 20",
                [SCOPE, like],
            ).fetchall()
        return rel, recent, mems

    rel_eps, recent_eps, memories = await asyncio.to_thread(_fetch_material)

    # Merge relevant + recent episodes, de-duped, relevant first.
    seen: set = set()
    episodes = []
    for e in rel_eps + recent_eps:
        if e["id"] in seen:
            continue
        seen.add(e["id"])
        episodes.append(e)

    def _heuristic_narrative() -> str:
        parts = [f"- {e['content']}" for e in episodes[:5]]
        text = f"Narrative for: {query}\n\n"
        text += f"Based on {len(episodes)} episodes and {len(memories)} memories:\n"
        text += "\n".join(parts) if parts else "(no relevant context found)"
        return text

    client, model = _make_llm_client()
    narrative = ""
    if client and (episodes or memories):
        ctx_lines = [f"- {e['content']}" for e in episodes[:18]]
        ctx_lines += [f"- (belief) {m['statement']}" for m in memories[:12]]
        focus = f" Stay within the domain of {domain}." if domain else ""
        prompt = (
            "You are composing a short, first-person narrative reflection for Hiten, "
            "drawn only from his own journal episodes and remembered beliefs below. "
            "Weave the material into 2-4 cohesive paragraphs that answer the question — "
            "show how things developed or connect rather than listing them. Ground every "
            "claim in the material; if it doesn't cover the question, say so plainly."
            f"{focus}\n\nQuestion: {query}\n\nMaterial:\n" + "\n".join(ctx_lines)
        )
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.6,
                max_tokens=700,
            )
            narrative = (resp.choices[0].message.content or "").strip()
        except Exception:
            logger.warning("narrative LLM compose failed, using heuristic", exc_info=True)
            narrative = ""
    if not narrative:
        narrative = _heuristic_narrative()

    return {
        "query": query,
        "narrative": narrative,
        "sources": len(episodes),
        "memories_referenced": len(memories),
        "llm": bool(client),
    }


# ---------------------------------------------------------------------------
# Events (activity feed)
# ---------------------------------------------------------------------------

@app.get("/api/events")
def list_events(
    scope: str = SCOPE,
    limit: int = Query(default=50, le=100),
    offset: int = 0,
):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, type, subject, scope, data, seq, created_at FROM events WHERE scope = %s ORDER BY seq DESC LIMIT %s OFFSET %s",
            [scope, limit, offset],
        ).fetchall()
    return {"items": rows, "count": len(rows)}


# ---------------------------------------------------------------------------
# Studio
# ---------------------------------------------------------------------------

@app.get("/api/studio")
def list_studios(scope: str = SCOPE):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, scope, title, status, properties, created_at, updated_at FROM studios WHERE scope = %s ORDER BY updated_at DESC",
            [scope],
        ).fetchall()
    return {"items": rows, "count": len(rows)}


@app.post("/api/studio")
async def create_studio(body: CreateStudioRequest):
    """Single write path: studio module mints the stu_ ULID and stages the
    studio.created event — the inline-SQL bypass (gen_random_uuid ids, no
    events) is gone."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import studio as studio_mod

    pool = await _get_async_pool(row_factory=psycopg.rows.tuple_row)
    pub = _make_publisher_sync()
    try:
        return await studio_mod.create_studio(
            pool, pub, SCOPE, title=body.title, properties=body.properties,
        )
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/studio/{studio_id}")
def get_studio(studio_id: str):
    with get_conn() as conn:
        studio = conn.execute("SELECT * FROM studios WHERE id = %s", [studio_id]).fetchone()
        if not studio:
            raise HTTPException(404, "Studio not found")
        sketches = conn.execute(
            "SELECT id, studio_id, content, kind, epistemic_status, properties, created_at, updated_at "
            "FROM studio_sketches WHERE studio_id = %s ORDER BY created_at DESC",
            [studio_id],
        ).fetchall()
        links = conn.execute(
            "SELECT id, src_sketch_id, dst_sketch_id, rel_type FROM studio_links "
            "WHERE src_sketch_id IN (SELECT id FROM studio_sketches WHERE studio_id = %s)",
            [studio_id],
        ).fetchall()
    return {"studio": studio, "sketches": sketches, "links": links}


@app.post("/api/studio/{studio_id}/sketch")
async def create_sketch(studio_id: str, body: CreateSketchRequest):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import studio as studio_mod

    with get_conn() as conn:
        exists = conn.execute("SELECT id FROM studios WHERE id = %s", [studio_id]).fetchone()
        if not exists:
            raise HTTPException(404, "Studio not found")

    pool = await _get_async_pool(row_factory=psycopg.rows.tuple_row)
    pub = _make_publisher_sync()
    try:
        return await studio_mod.create_sketch(
            pool, pub, studio_id,
            content=body.content, kind=body.kind, properties=body.properties,
        )
    except Exception as e:
        raise HTTPException(500, str(e))


@app.patch("/api/studio/sketch/{sketch_id}")
async def update_studio_sketch(sketch_id: str, body: UpdateSketchRequest):
    """Edit content or promote up the epistemic ladder (seed → conjecture →
    hypothesis). The studio module validates transitions; 'canonical' is
    unreachable for sketches — graduation is the only way out of the studio."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import studio as studio_mod

    pool = await _get_async_pool(row_factory=psycopg.rows.tuple_row)
    pub = _make_publisher_sync()
    try:
        return await studio_mod.update_sketch(
            pool, pub, sketch_id,
            content=body.content, epistemic_status=body.epistemic_status,
        )
    except ValueError as e:
        raise HTTPException(404 if "not found" in str(e) else 400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/studio/sketch/{sketch_id}/graduate")
async def graduate_studio_sketch(sketch_id: str, body: GraduateSketchRequest | None = None):
    """Graduate a sketch (≥ conjecture) into a Project in the 'Studio
    Graduates' workspace. One-way door: the sketch keeps its history and a
    properties.graduated_to pointer."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import studio as studio_mod

    body = body or GraduateSketchRequest()
    pool = await _get_async_pool(row_factory=psycopg.rows.tuple_row)
    pub = _make_publisher_sync()
    try:
        return await studio_mod.graduate_sketch(
            pool, pub, sketch_id,
            target_type=body.target_type, scope_text=SCOPE,
        )
    except ValueError as e:
        raise HTTPException(404 if "not found" in str(e) else 400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

@app.get("/api/simulation/runs")
def list_simulation_runs(scope: str = SCOPE):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, scope, question, world_model_id, status, epistemic_status, "
            "outcome_distribution, parameters, created_at, completed_at "
            "FROM simulation_runs WHERE scope = %s ORDER BY created_at DESC",
            [scope],
        ).fetchall()
    return {"items": rows, "count": len(rows)}


@app.post("/api/simulation/runs")
async def create_simulation_run(body: CreateSimulationRunRequest):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from simulation import create_simulation_run as create_sim

    pool = await _get_async_pool(row_factory=psycopg.rows.tuple_row)
    pub = _make_publisher_sync()
    try:
        return await create_sim(
            pool, pub, SCOPE,
            question=body.question,
            world_model_id=body.world_model_id,
            parameters=body.parameters,
        )
    except Exception as e:
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# Workspaces + Projects
# ---------------------------------------------------------------------------
# Safety & Approvals (Phase F.3 spine — lifted PDP substrate)
# ---------------------------------------------------------------------------

class CreateApprovalRequest(BaseModel):
    action_class: str = Field(min_length=1, max_length=50)
    payload: dict = Field(default_factory=dict)
    ttl_seconds: int | None = Field(default=None, ge=60, le=30 * 24 * 3600)


class DenyApprovalRequest(BaseModel):
    reason: str = Field(default="user_denied", max_length=500)


class KillRequest(BaseModel):
    agent: str | None = None


def _approval_errors():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from agent.approval_service import ApprovalNotActionable, ApprovalNotFound
    return ApprovalNotFound, ApprovalNotActionable


@app.get("/api/approvals")
async def approvals_pending():
    """The approval-card queue: pending, unexpired approvals in scope."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from agent.approval_service import list_pending

    pool = await _get_async_pool(row_factory=psycopg.rows.tuple_row)
    items = await list_pending(pool, SCOPE)
    return {"items": items, "count": len(items)}


@app.post("/api/approvals")
async def create_human_approval(body: CreateApprovalRequest):
    """Create a HUMAN-originated approval (run_id NULL) — e.g. the webapp's
    hard-forget flow: create here, grant explicitly, then call the gated action
    with the approval_id. Deliberate two-step friction for irreversible ops."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from agent.pdp_gate import approval_ttl_seconds, scope_parts
    from shared.events import build_event
    from shared.types.ulid import mint
    from psycopg.types.json import Jsonb

    pool = await _get_async_pool(row_factory=psycopg.rows.tuple_row)
    pub = _make_publisher_sync()
    apv_id = mint("apv")
    ttl = body.ttl_seconds or approval_ttl_seconds()
    parts = scope_parts(SCOPE)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO approvals (id, run_id, origin, scope, action_class, payload, state, expires_at) "
                "VALUES (%s, NULL, 'human', %s, %s, %s, 'pending', now() + make_interval(secs => %s))",
                (apv_id, SCOPE, body.action_class, Jsonb(body.payload), ttl),
            )
        ev = build_event(
            short_type="safety.approval.requested", subject=apv_id, scope=parts,
            data={"apv_id": apv_id, "run_id": None, "action_class": body.action_class,
                  "origin": "human"},
            actor=f"user:{parts['user_id']}", source="curlyos-core/safety",
        )
        _id, subject, stamped = await pub.stage(ev, conn)
    return {"apv_id": apv_id, "state": "pending", "origin": "human",
            "action_class": body.action_class, "ttl_seconds": ttl}


async def _resume_after_decision(request: Request, result: dict) -> dict:
    """Grant AND deny both wake a parked run — the act node reads the
    approval's final state and proceeds (execute) or records the denial and
    moves on. One resume primitive (runner.resume)."""
    run_id = result.get("run_id")
    runner = getattr(request.app.state, "runner", None)
    if run_id and runner is not None:
        try:
            result["resumed"] = await runner.resume(run_id)
        except Exception as e:
            logger.exception("resume after approval decision failed run=%s", run_id)
            result["resumed"] = False
            result["resume_error"] = str(e)
    return result


@app.post("/api/approvals/{apv_id}/grant")
async def grant_approval(apv_id: str, request: Request):
    """Grant a pending approval; a parked run resumes and executes the action."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from agent.approval_service import grant

    ApprovalNotFound, ApprovalNotActionable = _approval_errors()
    pool = await _get_async_pool(row_factory=psycopg.rows.tuple_row)
    try:
        result = await grant(pool, _make_publisher_sync(), SCOPE, apv_id)
    except ApprovalNotFound as e:
        raise HTTPException(404, str(e))
    except ApprovalNotActionable as e:
        raise HTTPException(409, str(e))
    return await _resume_after_decision(request, result)


@app.post("/api/approvals/{apv_id}/deny")
async def deny_approval(apv_id: str, request: Request,
                        body: DenyApprovalRequest | None = None):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from agent.approval_service import deny

    ApprovalNotFound, ApprovalNotActionable = _approval_errors()
    body = body or DenyApprovalRequest()
    pool = await _get_async_pool(row_factory=psycopg.rows.tuple_row)
    try:
        result = await deny(pool, _make_publisher_sync(), SCOPE, apv_id, reason=body.reason)
    except ApprovalNotFound as e:
        raise HTTPException(404, str(e))
    except ApprovalNotActionable as e:
        raise HTTPException(409, str(e))
    return await _resume_after_decision(request, result)


@app.get("/api/safety/kill")
async def safety_kill_status(agent: str | None = None):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from safety.killswitch import kill_status

    return await kill_status(_make_redis(), agent)


@app.post("/api/safety/kill")
async def safety_kill_engage(body: KillRequest | None = None):
    """The panic button: kills all (or one agent's) side-effecting actions.
    Fail-closed by design — with the flag set, every PDP verdict above read is DENY."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from agent.pdp_gate import scope_parts
    from safety.killswitch import set_kill

    body = body or KillRequest()
    pool = await _get_async_pool(row_factory=psycopg.rows.tuple_row)
    parts = scope_parts(SCOPE)
    try:
        return await set_kill(
            _make_redis(), pool, _make_publisher_sync(),
            scope_text=SCOPE, agent=body.agent, set_by=f"user:{parts['user_id']}",
        )
    except RuntimeError as e:
        raise HTTPException(503, str(e))


@app.delete("/api/safety/kill")
async def safety_kill_clear(agent: str | None = None):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from safety.killswitch import clear_kill

    try:
        return await clear_kill(_make_redis(), agent=agent)
    except RuntimeError as e:
        raise HTTPException(503, str(e))


# ---------------------------------------------------------------------------

@app.get("/api/workspaces")
def list_workspaces(scope: str = SCOPE):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, scope, name, kind, properties, created_at, updated_at "
            "FROM workspaces WHERE scope = %s ORDER BY updated_at DESC",
            [scope],
        ).fetchall()
    return {"items": rows, "count": len(rows)}


@app.post("/api/workspaces")
def create_workspace(body: CreateWorkspaceRequest):
    with get_conn() as conn:
        row = conn.execute(
            "INSERT INTO workspaces (id, scope, name, kind, properties) "
            "VALUES (gen_random_uuid()::text, %s, %s, %s, %s) "
            "RETURNING id, created_at",
            [SCOPE, body.name, body.kind, json.dumps(body.properties)],
        ).fetchone()
    return {"id": row["id"], "name": body.name, "kind": body.kind,
            "created_at": row["created_at"].isoformat() if row else None}


@app.get("/api/projects")
def list_projects(workspace_id: str | None = None, scope: str = SCOPE):
    with get_conn() as conn:
        if workspace_id:
            rows = conn.execute(
                "SELECT id, workspace_id, name, status, properties, created_at "
                "FROM projects WHERE workspace_id = %s ORDER BY created_at DESC",
                [workspace_id],
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, workspace_id, name, status, properties, created_at "
                "FROM projects ORDER BY created_at DESC",
            ).fetchall()
    return {"items": rows, "count": len(rows)}


@app.post("/api/projects")
def create_project(body: CreateProjectRequest):
    with get_conn() as conn:
        # Verify workspace exists
        ws = conn.execute("SELECT id FROM workspaces WHERE id = %s", [body.workspace_id]).fetchone()
        if not ws:
            raise HTTPException(404, f"Workspace not found: {body.workspace_id}")
        row = conn.execute(
            "INSERT INTO projects (id, workspace_id, name, status, properties) "
            "VALUES (gen_random_uuid()::text, %s, %s, 'active', %s) "
            "RETURNING id, created_at",
            [body.workspace_id, body.name, json.dumps(body.properties)],
        ).fetchone()
    return {"id": row["id"], "workspace_id": body.workspace_id, "name": body.name,
            "status": "active", "created_at": row["created_at"].isoformat() if row else None}


# ---------------------------------------------------------------------------
# Observability — log tailing + per-system status dashboard
# ---------------------------------------------------------------------------

# Short name -> absolute path for the log files the frontend can tail.
LOG_SOURCES: dict[str, str] = {
    "api": "/tmp/curlyos-api.log",
    "deploy": "/tmp/curlyos-deploy.log",
    "gate": "/tmp/curlyos_gate.log",
    "up": "/tmp/curlyos_up.log",
}

# Autonomous-cognition engines surfaced on the systems dashboard. Activity is
# derived from the `events` table by matching dotted event types via ILIKE.
SYSTEM_ENGINES: list[dict[str, str]] = [
    {"name": "consolidation", "label": "Memory Consolidation", "keyword": "consolidation"},
    {"name": "reflection", "label": "Reflection", "keyword": "reflection"},
    {"name": "meta", "label": "Meta-Cognition", "keyword": "meta"},
    {"name": "memory", "label": "Memory / Episodes", "keyword": "memory"},
    {"name": "knowledge", "label": "Knowledge Graph", "keyword": "knowledge"},
]


def _file_meta(path: str) -> dict[str, Any]:
    """Existence + size + modified-time for a path; never raises."""
    try:
        st = os.stat(path)
        return {
            "exists": True,
            "size_bytes": st.st_size,
            "modified": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
        }
    except FileNotFoundError:
        return {"exists": False, "size_bytes": 0, "modified": None}
    except Exception as e:
        return {"exists": False, "size_bytes": 0, "modified": None, "error": str(e)}


# NOTE: registered BEFORE /api/logs so the literal path is not shadowed by it.
@app.get("/api/logs/sources")
def log_sources():
    """List the known log sources and their current file metadata."""
    sources = []
    for name, path in LOG_SOURCES.items():
        meta = _file_meta(path)
        sources.append({
            "name": name,
            "path": path,
            "exists": meta["exists"],
            "size_bytes": meta["size_bytes"],
            "modified": meta["modified"],
        })
    return {"sources": sources}


@app.get("/api/logs")
def get_logs(
    source: str = "api",
    lines: int = Query(default=200, le=2000),
):
    """Tail the last `lines` lines of a known log file."""
    if source not in LOG_SOURCES:
        raise HTTPException(404, "unknown log source")

    path = LOG_SOURCES[source]
    meta = _file_meta(path)
    out: dict[str, Any] = {
        "source": source,
        "path": path,
        "exists": meta["exists"],
        "size_bytes": meta["size_bytes"],
        "modified": meta["modified"],
        "lines": [],
        "count": 0,
    }
    if meta.get("error"):
        out["error"] = meta["error"]
    if not meta["exists"]:
        return out

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        all_lines = content.split("\n")
        # Drop the trailing empty element produced by a final newline.
        if all_lines and all_lines[-1] == "":
            all_lines.pop()
        tail = all_lines[-lines:] if lines > 0 else []
        out["lines"] = tail
        out["count"] = len(tail)
    except Exception as e:
        out["error"] = str(e)
    return out


def _scheduler_summary(request: Request) -> dict:
    sched = getattr(request.app.state, "scheduler", None)
    if sched is None:
        return {"running": False, "jobs": 0, "failing": []}
    snap = sched.snapshot()
    return {
        "running": snap["running"],
        "jobs": len(snap["jobs"]),
        "failing": [j["name"] for j in snap["jobs"] if j["consecutive_failures"] > 0],
        "next_due": min((j["next_due"] for j in snap["jobs"] if j["next_due"]), default=None),
    }


@app.get("/api/systems")
def systems(request: Request):
    """Aggregate a single per-system status payload for the dashboard."""
    # Reuse the existing health() + stats() endpoint logic (plain functions —
    # this whole route runs in the threadpool).
    health_data = health()
    stats_data = stats()

    port = os.environ.get("CURLYOS_API_PORT", "8643")
    pg = health_data.get("postgres", {})
    rd = health_data.get("redis", {})
    emb = health_data.get("embedder", {})

    if pg.get("ok"):
        pg_detail = str(pg.get("version", "ok"))
        if pg.get("pgvector"):
            pg_detail = f"{pg_detail} +pgvector"
    else:
        pg_detail = str(pg.get("error", "unavailable"))

    infrastructure = [
        {"name": "postgres", "ok": bool(pg.get("ok")), "detail": pg_detail},
        {"name": "redis", "ok": bool(rd.get("ok")),
         "detail": str(rd.get("version", "")) if rd.get("ok") else str(rd.get("error", "unavailable"))},
        {"name": "embedder", "ok": bool(emb.get("ok")),
         "detail": str(emb.get("model", "")) if emb.get("ok") else str(emb.get("error", "unavailable"))},
        {"name": "api_server", "ok": True, "detail": f"uvicorn :{port}"},
    ]

    engines: list[dict[str, Any]] = []
    try:
        conn = get_conn()
    except Exception as e:
        # DB unreachable — still return infra + stats; mark every engine errored.
        for eng in SYSTEM_ENGINES:
            engines.append({"name": eng["name"], "label": eng["label"], "error": str(e)})
        return {
            "timestamp": now_iso(),
            "infrastructure": infrastructure,
            "stats": stats_data,
            "engines": engines,
            "scheduler": _scheduler_summary(request),
        }

    with conn:
        for eng in SYSTEM_ENGINES:
            like = f"%{eng['keyword']}%"
            entry: dict[str, Any] = {
                "name": eng["name"],
                "label": eng["label"],
                "last_run": None,
                "last_event_type": None,
                "runs_24h": 0,
                "runs_7d": 0,
                "recent": [],
            }
            try:
                latest = conn.execute(
                    "SELECT type, created_at FROM events WHERE scope = %s AND type ILIKE %s "
                    "ORDER BY created_at DESC LIMIT 1",
                    [SCOPE, like],
                ).fetchone()
                if latest:
                    entry["last_run"] = latest["created_at"].isoformat() if latest["created_at"] else None
                    entry["last_event_type"] = latest["type"]
                entry["runs_24h"] = conn.execute(
                    "SELECT count(*) AS n FROM events WHERE scope = %s AND type ILIKE %s "
                    "AND created_at > now() - interval '24 hours'",
                    [SCOPE, like],
                ).fetchone()["n"]
                entry["runs_7d"] = conn.execute(
                    "SELECT count(*) AS n FROM events WHERE scope = %s AND type ILIKE %s "
                    "AND created_at > now() - interval '7 days'",
                    [SCOPE, like],
                ).fetchone()["n"]
                recent = conn.execute(
                    "SELECT id, type, subject, created_at, data FROM events "
                    "WHERE scope = %s AND type ILIKE %s ORDER BY created_at DESC LIMIT 5",
                    [SCOPE, like],
                ).fetchall()
                entry["recent"] = [
                    {
                        "id": r["id"],
                        "type": r["type"],
                        "subject": r["subject"],
                        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                        "data": r["data"],
                    }
                    for r in recent
                ]
            except Exception as e:
                entry = {"name": eng["name"], "label": eng["label"], "error": str(e)}
            engines.append(entry)

    return {
        "timestamp": now_iso(),
        "infrastructure": infrastructure,
        "stats": stats_data,
        "engines": engines,
        "scheduler": _scheduler_summary(request),
    }


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("CURLYOS_API_PORT", "8643"))
    uvicorn.run(app, host="127.0.0.1", port=port)


# ---------------------------------------------------------------------------
# Autonomous Cognition Endpoints (triggered by Hermes cron)
# ---------------------------------------------------------------------------

# App-lifetime connection pools, one per row factory, created lazily on first
# use and closed by the lifespan shutdown hook. Callers must NOT close them.
_POOLS: dict[str, Any] = {}
_POOLS_LOCK = asyncio.Lock()


async def _get_async_pool(row_factory=None):
    """Return the shared app-lifetime AsyncConnectionPool for `row_factory`.

    Defaults to tuple_row: the entire curlyos-core stack (governance,
    retrieval, identity, consolidation, meta, reflection) uses positional
    row access (r[0]/r[1] and tuple unpacking). Endpoints that genuinely
    need column-by-name access can pass row_factory=psycopg.rows.dict_row.

    Lazy creation (rather than eager open at startup) keeps the API up while
    Postgres is briefly down — requests fail individually and recover.
    """
    import psycopg_pool
    if row_factory is None:
        row_factory = psycopg.rows.tuple_row
    key = getattr(row_factory, "__name__", str(row_factory))
    pool = _POOLS.get(key)
    if pool is not None:
        return pool
    async with _POOLS_LOCK:
        pool = _POOLS.get(key)
        if pool is None:
            pool = psycopg_pool.AsyncConnectionPool(
                DSN,
                min_size=1,
                max_size=3,
                kwargs={"row_factory": row_factory},
                open=False,
            )
            await pool.open()
            _POOLS[key] = pool
    return pool


def _make_redis():
    """Create a Redis connection, or None if not configured."""
    if not REDIS_URL:
        return None
    try:
        import redis.asyncio as aioredis
        return aioredis.from_url(REDIS_URL, socket_timeout=5)
    except Exception:
        return None


def _make_publisher_sync():
    """Create a PgNatsPublisher without NATS (PG-only staging)."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from shared.events.implementations import PgOnlyPublisher
    return PgOnlyPublisher()


async def _make_embedder():
    """Create an embedder — try LocalBgeM3, fall back to FakeEmbedder."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        from shared.embeddings.implementations import LocalBgeM3
        emb = LocalBgeM3()
        # Trigger lazy load
        await emb.embed(["warmup"])
        return emb
    except Exception:
        from shared.embeddings.implementations import FakeEmbedder
        return FakeEmbedder()


def _load_env_key(*names: str) -> str:
    """Resolve an API key from the process env, falling back to CORE's own
    .env file. (Previously fell back to ~/.hermes/.env — a hidden Hermes
    coupling removed in Phase C; core must boot with Hermes deleted, P1.)
    """
    for n in names:
        v = os.environ.get(n, "")
        if v:
            return v
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() in names:
                    return v.strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


# Process-wide cached LLM client: the OpenRouter key is read from disk once and
# the AsyncOpenAI client (with its httpx pool) is reused across calls.
_LLM_CLIENT: Any = None
_LLM_MODEL: str = ""


def _make_llm_client():
    """Build (cached) an OpenRouter-backed AsyncOpenAI client + model, or (None, "").

    Returns (client, model). Returns (None, "") when no key is available or
    the openai SDK is missing — callers must fall back to heuristics. The
    no-key result is NOT cached so adding a key later is picked up.
    """
    global _LLM_CLIENT, _LLM_MODEL
    if _LLM_CLIENT is not None:
        return _LLM_CLIENT, _LLM_MODEL
    key = _load_env_key("OPENROUTER_API_KEY")
    if not key:
        return None, ""
    try:
        from openai import AsyncOpenAI
    except Exception:
        return None, ""
    from shared.models import FallbackClient, general_chain, primary_model
    # Fast per-model failure (no slow same-model 429 backoff) — the chain is the
    # resilience, so we move to the next model instead of retrying a limited one.
    raw = AsyncOpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=key,
        timeout=60.0,
        max_retries=0,
    )
    # Wrap so .chat.completions.create fails over across CURLYOS_MODEL_CHAIN.
    client = FallbackClient(raw, general_chain())
    model = os.environ.get("CURLYOS_LLM_MODEL") or primary_model()
    _LLM_CLIENT, _LLM_MODEL = client, model
    return client, model


# Process-wide embedder singleton. bge-m3 is ~1.3GB on a RAM-tight box: it must
# load AT MOST ONCE, hence the lock around first load (two concurrent first
# requests would otherwise both instantiate it).
_EMBEDDER: Any = None
_EMBEDDER_LOCK = asyncio.Lock()


async def get_shared_embedder():
    """Cached embedder shared by recall, ingest, consolidation and sweeps.

    Caches only a real LocalBgeM3; a FakeEmbedder fallback is returned fresh
    (not cached) so a later call can still pick up a real model once available.
    """
    global _EMBEDDER
    if _EMBEDDER is not None:
        return _EMBEDDER
    async with _EMBEDDER_LOCK:
        if _EMBEDDER is not None:
            return _EMBEDDER
        emb = await _make_embedder()
        from shared.embeddings.implementations import FakeEmbedder
        if not isinstance(emb, FakeEmbedder):
            _EMBEDDER = emb
        return emb


async def _embed_row(pool: Any, table: str, row_id: str, text: str, embedder: Any) -> bool:
    """Embed `text` and store it in {table}.embedding for `row_id`. Best-effort.

    `table` is a fixed internal literal ("memories"/"episodes"), never user input.
    """
    if not text or not text.strip():
        return False
    try:
        vec = (await embedder.embed([text]))[0]
        literal = "[" + ",".join(repr(float(x)) for x in vec) + "]"
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"UPDATE {table} SET embedding = %s::vector WHERE id = %s",
                    (literal, row_id),
                )
        return True
    except Exception:
        logger.exception("embed failed table=%s id=%s", table, row_id)
        return False


async def _process_episode_bg(
    scope: str, epi_id: str, mem_id: str | None, text: str, extract_knowledge: bool = True,
) -> None:
    """Background processing for an ingested episode. Embeds the episode + its
    memory (so both are dense-recallable immediately), then runs knowledge
    extraction (LLM → graph; regex fallback). Runs after the HTTP response so
    capture stays snappy. Best-effort with retries; failures are logged and any
    row still left unembedded is picked up by the startup sweep.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from knowledge.graph import extract_and_project

    try:
        pool = await _get_async_pool(row_factory=psycopg.rows.tuple_row)
        pub = _make_publisher_sync()
        embedder = await get_shared_embedder()

        # Embeddings first — these make the entry dense-recallable right away.
        done_epi = done_mem = False
        for attempt in range(3):
            done_epi = done_epi or await _embed_row(pool, "episodes", epi_id, text, embedder)
            done_mem = done_mem or not mem_id or await _embed_row(pool, "memories", mem_id, text[:4000], embedder)
            if done_epi and done_mem:
                break
            logger.warning("embedding attempt %d incomplete epi=%s", attempt + 1, epi_id)
            await asyncio.sleep(2 ** attempt * 2)

        # Classify the captured memory's epistemic status (canonical/belief/
        # hypothesis) instead of blanket canonical. Best-effort, background.
        if mem_id:
            try:
                llm_client, model = _make_llm_client()
                if llm_client:
                    from shared.epistemic import classify_statements
                    res = await classify_statements(llm_client, model, [{"id": mem_id, "statement": text[:2000]}])
                    status = res.get(mem_id, "canonical")
                    if status != "canonical":
                        async with pool.connection() as conn:
                            async with conn.cursor() as cur:
                                await cur.execute(
                                    "UPDATE memories SET epistemic_status = %s WHERE id = %s AND valid_to IS NULL",
                                    (status, mem_id),
                                )
            except Exception:
                logger.exception("epistemic classify failed epi=%s", epi_id)

        # Then knowledge-graph extraction (entities/edges).
        if extract_knowledge:
            llm_client, _ = _make_llm_client()
            for attempt in range(3):
                try:
                    await extract_and_project(
                        pool, pub, scope, epi_id, text,
                        embedder=embedder, llm_client=llm_client,
                    )
                    break
                except Exception:
                    logger.exception("knowledge extraction failed epi=%s attempt=%d", epi_id, attempt + 1)
                    if attempt < 2:
                        await asyncio.sleep(2 ** attempt * 2)
    except Exception:
        logger.exception("background processing failed epi=%s", epi_id)


async def _sweep_unembedded() -> None:
    """Startup self-heal: embed recent rows left embedding-less by a crash or
    restart that killed a background task mid-flight. Sequential and bounded
    by design — this box swaps on big encode batches.
    """
    try:
        pool = await _get_async_pool(row_factory=psycopg.rows.tuple_row)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT 'episodes' AS t, id, content AS txt FROM episodes "
                    "WHERE embedding IS NULL AND content IS NOT NULL AND content <> '' "
                    "AND created_at > now() - interval '7 days' "
                    "UNION ALL "
                    "SELECT 'memories' AS t, id, statement AS txt FROM memories "
                    "WHERE embedding IS NULL AND statement IS NOT NULL AND statement <> '' "
                    "AND created_at > now() - interval '7 days' "
                    "LIMIT 500"
                )
                rows = await cur.fetchall()
        if not rows:
            return
        logger.info("sweep: re-embedding %d rows left unembedded", len(rows))
        embedder = await get_shared_embedder()
        done = 0
        for table, row_id, txt in rows:
            text = txt[:4000] if table == "memories" else txt
            done += await _embed_row(pool, table, row_id, text, embedder)
        logger.info("sweep: embedded %d/%d rows", done, len(rows))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("unembedded sweep failed")


@app.post("/api/ingest")
async def ingest(body: IngestRequest, background_tasks: BackgroundTasks):
    """Record raw text as an episode in curlyos-memory and process it.

    Used by the web app (journal capture). Records an episode, appends a
    recallable memory, and schedules LLM knowledge extraction in the
    background so the caller returns immediately.

    Body: {"text": str, "source_ref"?: str, "scope"?: str,
           "add_memory"?: bool (default true),
           "extract_knowledge"?: bool (default true),
           "kind"?: "fact"|"procedure"|"preference" (default "fact"),
           "epistemic_status"?: "canonical"|"hypothesis"|"belief" (default "canonical")}
    """
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    source_ref = body.source_ref or "web:capture"
    scope = body.scope or SCOPE
    add_memory = body.add_memory
    extract_knowledge = body.extract_knowledge

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from memory.governance import record_episode, add

    pool = await _get_async_pool(row_factory=psycopg.rows.tuple_row)
    pub = _make_publisher_sync()
    epi_id = None
    result: dict = {}
    try:
        epi = await record_episode(
            pool, pub, scope, content=text, source_ref=source_ref,
        )
        epi_id = epi["epi_id"]
        result["epi_id"] = epi_id
        if add_memory:
            mem = await add(
                pool, pub, scope,
                statement=text[:4000],
                source_episode_id=epi_id,
                kind=body.kind,
                # canonical (default) so it's recallable: fast/deep recall
                # filter to epistemic_status='canonical' only ("belief" is in
                # no mode's filter, so belief memories never surface). Callers
                # that want non-recallable drafts pass "hypothesis".
                epistemic_status=body.epistemic_status,
            )
            result["mem_id"] = mem.get("mem_id")
    except Exception as e:
        logger.exception("ingest failed source_ref=%s", source_ref)
        return {"error": str(e)}

    if epi_id:
        background_tasks.add_task(
            _process_episode_bg, scope, epi_id, result.get("mem_id"), text, extract_knowledge,
        )
    result["processing"] = "scheduled" if epi_id else "skipped"
    return result


@app.post("/api/consolidation/run")
async def consolidation_run(body: ConsolidationRunRequest | None = None):
    """Run consolidation passes.

    Body: {"mode": "fast"|"deep", "scope": "user:usr_hiten"}
      fast  = dedup + conflict_resolve
      deep  = all passes (dedup, merge_promote, conflict_resolve, summarize, decay, recombine_incubate)
    """
    body = body or ConsolidationRunRequest()
    scope = body.scope
    deep = body.mode == "deep"

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from memory.consolidation import run_consolidation

    # Consolidation uses positional/tuple row access — force tuple_row.
    pool = await _get_async_pool(row_factory=psycopg.rows.tuple_row)
    redis = _make_redis()
    try:
        result = await run_consolidation(
            pool=pool,
            redis=redis,
            embedder=await get_shared_embedder(),
            publisher=_make_publisher_sync(),
            scope=scope,
            deep=deep,
        )
        return result
    except Exception as e:
        logger.exception("consolidation run failed scope=%s deep=%s", scope, deep)
        return {"error": str(e), "scope": scope, "deep": deep}
    finally:
        if redis:
            await redis.close()


async def _sync_identity_from_reflection(pool: Any, pub: Any, scope: str) -> dict:
    """Promote reflection reports' identity_candidates into identity_facts.

    Closes the reflection→identity loop so identity keeps building as memory
    grows. propose_identity_fact auto-promotes confidence >= 0.75 to canonical
    (lower → hypothesis) and supersedes lower-confidence conflicts. Idempotent:
    skips candidates whose (predicate, object) is already a current fact.
    Best-effort. Returns counts.
    """
    import json
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from identity import propose_identity_fact
    from memory.governance import record_episode

    cands: list[dict] = []
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT identity_candidates FROM reflection_reports "
                "WHERE scope = %s AND identity_candidates IS NOT NULL",
                [scope],
            )
            for (ic,) in await cur.fetchall():
                data = ic if isinstance(ic, list) else (json.loads(ic) if isinstance(ic, str) else [])
                for c in (data or []):
                    if not (isinstance(c, dict) and c.get("predicate") and c.get("object") is not None):
                        continue
                    obj = str(c["object"])
                    # Skip raw-transcript junk — reflection sometimes emits whole
                    # conversation turns as "identity". Real identity values are
                    # short and free of turn/speaker markers.
                    if "[turn" in obj or "User:" in obj or "Assistant:" in obj or len(obj) > 120:
                        continue
                    cands.append(c)

    # One best (highest-confidence) candidate per predicate: identity_facts holds
    # one value per predicate and propose_identity_fact supersedes conflicts, so
    # promoting competing objects for the same predicate would churn.
    best: dict[str, dict] = {}
    for c in cands:
        pred = str(c["predicate"])
        if pred not in best or float(c.get("confidence", 0.6)) > float(best[pred].get("confidence", 0.6)):
            best[pred] = c

    promoted = skipped = 0
    epi_id = None
    for pred, c in best.items():
        obj, conf = str(c["object"]), float(c.get("confidence", 0.6))
        # Idempotent + churn-free: skip predicates that already have a current
        # fact (don't re-propose/supersede on every reflection run).
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT 1 FROM identity_facts WHERE scope = %s AND predicate = %s "
                    "AND valid_to IS NULL",
                    [scope, pred],
                )
                if await cur.fetchone():
                    skipped += 1
                    continue
        if epi_id is None:
            epi = await record_episode(
                pool, pub, scope,
                content="Identity facts promoted from reflection reports.",
                source_ref="reflection:identity-promotion",
            )
            epi_id = epi["epi_id"]
        try:
            await propose_identity_fact(
                pool, pub, scope, predicate=pred, object=obj,
                confidence=conf, source_episode_id=epi_id,
            )
            promoted += 1
        except Exception:
            skipped += 1
    return {"identity_promoted": promoted, "identity_skipped": skipped}


async def _sync_goals_from_reflection(pool: Any, scope: str) -> dict:
    """Land the latest reflection's goal_deltas back on first-class goal rows:
    properties.last_reflection = the delta; a 'completed' delta drives
    progress to 1.0 (achievement itself stays the human's call). Closes the
    reflection→goals loop (Phase G exit criterion). Best-effort."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from goals import set_goal_reflection

    synced = 0
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT goal_deltas FROM reflection_reports WHERE scope = %s "
                    "ORDER BY created_at DESC LIMIT 1",
                    [scope],
                )
                row = await cur.fetchone()
        deltas = (row[0] if row else None) or []
        if isinstance(deltas, str):
            deltas = json.loads(deltas)
        for d in deltas:
            if isinstance(d, dict) and str(d.get("goal_id", "")).startswith("goal_"):
                if await set_goal_reflection(pool, d["goal_id"], scope, d):
                    synced += 1
    except Exception:
        logger.exception("goal sync from reflection failed scope=%s", scope)
    return {"goals_synced": synced}


async def _sync_principles_to_memory(pool: Any, pub: Any, embedder: Any, scope: str) -> dict:
    """Mirror distilled principles into recallable memories.

    Closes the meta→memory loop so principles surface in /api/search and
    /api/recall. Stored canonical (recall filters to canonical) with a
    provenance episode (source_ref='meta:principles-mirror') and embedded so
    they're dense-recallable. Idempotent: skips principles already mirrored.
    Best-effort. Returns counts.
    """
    import re
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from memory.governance import record_episode, add

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT statement FROM principles WHERE scope = %s AND valid_to IS NULL",
                [scope],
            )
            # Skip word-frequency "principles" (e.g. "... 'use' appears 4 times")
            # from the pattern-counting distiller — noise in recallable memory.
            _junk = re.compile(r"appears\s+\d+\s+times", re.I)
            principles = [
                r[0] for r in await cur.fetchall()
                if r[0] and r[0].strip() and not _junk.search(r[0])
            ]

    added = skipped = 0
    epi_id = None
    for statement in principles:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT 1 FROM memories WHERE scope = %s AND statement = %s AND valid_to IS NULL",
                    [scope, statement],
                )
                if await cur.fetchone():
                    skipped += 1
                    continue
        if epi_id is None:
            epi = await record_episode(
                pool, pub, scope,
                content="Principles distilled by meta-cognition.",
                source_ref="meta:principles-mirror",
            )
            epi_id = epi["epi_id"]
        try:
            mem = await add(
                pool, pub, scope, statement=statement,
                source_episode_id=epi_id, kind="fact", epistemic_status="canonical",
            )
            mid = mem.get("mem_id")
            if mid and embedder is not None:
                await _embed_row(pool, "memories", mid, statement, embedder)
            added += 1
        except Exception:
            skipped += 1
    return {"principles_mirrored": added, "principles_skipped": skipped}


@app.post("/api/reflection/weekly")
async def reflection_weekly(body: ReflectionRequest | None = None):
    """Run a weekly reflection over the past 7 days.

    Body: {"scope": "user:usr_hiten", "window_days": 7}
    """
    body = body or ReflectionRequest()
    scope, window_days = body.scope, body.window_days

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from cognition.reflection import run_weekly_reflection

    llm_client, llm_model = _make_llm_client()
    pool = await _get_async_pool()
    try:
        result = await run_weekly_reflection(
            pool=pool,
            publisher=_make_publisher_sync(),
            scope=scope,
            window_days=window_days,
            llm_client=llm_client,
            llm_model=llm_model,
        )
        result["llm"] = bool(llm_client)
        result.update(await _sync_identity_from_reflection(pool, _make_publisher_sync(), scope))
        result.update(await _sync_goals_from_reflection(pool, scope))
        return result
    except Exception as e:
        logger.exception("weekly reflection failed scope=%s", scope)
        return {"error": str(e), "scope": scope, "type": "weekly"}


@app.post("/api/reflection/monthly")
async def reflection_monthly(body: ReflectionRequest | None = None):
    """Run a monthly reflection over the past 30 days.

    Body: {"scope": "user:usr_hiten"}
    """
    body = body or ReflectionRequest()
    scope = body.scope

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from cognition.reflection import run_monthly_reflection

    llm_client, llm_model = _make_llm_client()
    pool = await _get_async_pool()
    try:
        result = await run_monthly_reflection(
            pool=pool,
            publisher=_make_publisher_sync(),
            scope=scope,
            llm_client=llm_client,
            llm_model=llm_model,
        )
        result["llm"] = bool(llm_client)
        result.update(await _sync_identity_from_reflection(pool, _make_publisher_sync(), scope))
        result.update(await _sync_goals_from_reflection(pool, scope))
        return result
    except Exception as e:
        logger.exception("monthly reflection failed scope=%s", scope)
        return {"error": str(e), "scope": scope, "type": "monthly"}


@app.post("/api/meta/audit")
async def meta_audit(body: MetaAuditRequest | None = None):
    """Run a decision audit + principle distillation over recent episodes.

    Body: {"scope": "user:usr_hiten", "window_days": 30}
    """
    body = body or MetaAuditRequest()
    scope, window_days = body.scope, body.window_days

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from cognition.meta import run_decision_audit, distill_principles

    llm_client, llm_model = _make_llm_client()
    pool = await _get_async_pool()
    try:
        audit_result = await run_decision_audit(
            pool=pool,
            publisher=_make_publisher_sync(),
            scope=scope,
            window_days=window_days,
            llm_client=llm_client,
            llm_model=llm_model,
        )
        principles = await distill_principles(
            pool=pool,
            publisher=_make_publisher_sync(),
            scope=scope,
            llm_client=llm_client,
            llm_model=llm_model,
        )
        sync = await _sync_principles_to_memory(
            pool, _make_publisher_sync(), await get_shared_embedder(), scope,
        )
        return {
            "audit": audit_result,
            "principles_distilled": len(principles),
            "principles": principles,
            **sync,
        }
    except Exception as e:
        logger.exception("meta audit failed scope=%s", scope)
        return {"error": str(e), "scope": scope}


@app.post("/api/meta/distill")
async def meta_distill(body: MetaDistillRequest | None = None):
    """Distill principles from decision audits.

    Body: {"scope": "user:usr_hiten", "min_confidence": 0.7}
    """
    body = body or MetaDistillRequest()
    scope, min_confidence = body.scope, body.min_confidence

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from cognition.meta import distill_principles

    llm_client, llm_model = _make_llm_client()
    pool = await _get_async_pool()
    try:
        principles = await distill_principles(
            pool=pool,
            publisher=_make_publisher_sync(),
            scope=scope,
            min_confidence=min_confidence,
            llm_client=llm_client,
            llm_model=llm_model,
        )
        sync = await _sync_principles_to_memory(
            pool, _make_publisher_sync(), await get_shared_embedder(), scope,
        )
        return {"principles_distilled": len(principles), "principles": principles, **sync}
    except Exception as e:
        logger.exception("meta distill failed scope=%s", scope)
        return {"error": str(e), "scope": scope}


@app.post("/api/narrative/generate")
async def narrative_generate(body: NarrativeGenerateRequest | None = None):
    """Surface themes + compose life chapters from episodes.

    Body: {"scope": "user:usr_hiten", "min_frequency": 3}
    Each run supersedes prior hypothesis themes/chapters (invalidate-not-delete)
    so the active set stays a fresh, bounded snapshot.
    """
    body = body or NarrativeGenerateRequest()
    scope, min_frequency = body.scope, body.min_frequency

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from cognition.narrative import surface_themes, compose_chapters

    pool = await _get_async_pool()
    llm_client, llm_model = _make_llm_client()
    try:
        themes = await surface_themes(
            pool=pool, publisher=_make_publisher_sync(),
            scope=scope, min_frequency=min_frequency,
        )
        chapters = await compose_chapters(
            pool=pool, publisher=_make_publisher_sync(), scope=scope,
            llm_client=llm_client, llm_model=llm_model,
        )
        return {
            "scope": scope,
            "themes_surfaced": len(themes),
            "chapters_composed": len(chapters),
            "top_themes": [t.get("name") for t in themes[:8]],
            "llm": bool(llm_client),
        }
    except Exception as e:
        logger.exception("narrative generate failed scope=%s", scope)
        return {"error": str(e), "scope": scope}


@app.post("/api/attention/scan")
async def attention_scan(body: AttentionScanRequest | None = None):
    """Detect value-action alignment gaps + snapshot allocation & cognitive load.

    Body: {"scope": "user:usr_hiten", "window_days": 14}
    Alignment gaps are written as hypothesis-status alignment_signals (each run
    supersedes prior hypothesis signals). Allocation/load are computed read-only.
    """
    body = body or AttentionScanRequest()
    scope, window_days = body.scope, body.window_days

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from cognition.attention import (
        detect_alignment_gaps, get_allocation, estimate_cognitive_load,
    )

    pool = await _get_async_pool()
    try:
        gaps = await detect_alignment_gaps(
            pool=pool, publisher=_make_publisher_sync(), scope=scope,
        )
        allocation = await get_allocation(pool=pool, scope=scope, window_days=7)
        load = await estimate_cognitive_load(
            pool=pool, scope=scope, window_days=window_days,
        )
        return {
            "scope": scope,
            "alignment_gaps_found": len(gaps),
            "alignment_gaps": gaps,
            "allocation": allocation,
            "cognitive_load": load,
        }
    except Exception as e:
        logger.exception("attention scan failed scope=%s", scope)
        return {"error": str(e), "scope": scope}


# ---------------------------------------------------------------------------
# Scheduler job table — the OS's complete background behavior, in one place
# (curlyos-final/06 §3). Cadences mirror the Hermes cron entries they replace,
# offset +20min so during the one-week overlap Hermes fires first and the
# output-based period guards make the scheduler's firing a no-op.
# ---------------------------------------------------------------------------

def _table_period_guard(table: str, trunc: str):
    """True when `table` already has a row for the current date_trunc period
    (catches Hermes-triggered and manual runs too — the guard is on OUTPUT)."""
    async def guard() -> bool:
        pool = await _get_async_pool(row_factory=psycopg.rows.tuple_row)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT 1 FROM {table} WHERE scope = %s "
                    "AND created_at >= date_trunc(%s, now()) LIMIT 1",
                    (SCOPE, trunc),
                )
                return bool(await cur.fetchone())
    guard.__name__ = f"guard_{table}_{trunc}"
    return guard


def _reflection_period_guard(report_type: str, trunc: str):
    async def guard() -> bool:
        pool = await _get_async_pool(row_factory=psycopg.rows.tuple_row)
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT 1 FROM reflection_reports WHERE scope = %s "
                    "AND report_type = %s AND created_at >= date_trunc(%s, now()) LIMIT 1",
                    (SCOPE, report_type, trunc),
                )
                return bool(await cur.fetchone())
    guard.__name__ = f"guard_reflection_{report_type}"
    return guard


async def _approval_silence_job() -> dict:
    """Approval housekeeping: expire overdue approvals (event each) and remind
    once (per approval) about pending ones older than 6h. The 72h default-action
    ladder arrives with the Phase-A runner (deny needs a parked run to degrade)."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from agent.pdp_gate import scope_parts
    from shared.events import build_event
    from shared.notify import get_notifier

    pool = await _get_async_pool(row_factory=psycopg.rows.tuple_row)
    pub = _make_publisher_sync()

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE approvals SET state = 'expired', decided_at = now() "
                "WHERE state = 'pending' AND expires_at <= now() "
                "RETURNING id, scope, action_class",
            )
            expired_rows = await cur.fetchall()
        for apv_id, scope_text, action_class in expired_rows:
            ev = build_event(
                short_type="safety.approval.expired", subject=apv_id,
                scope=scope_parts(scope_text),
                data={"apv_id": apv_id, "action_class": action_class},
                actor="system", source="curlyos-core/scheduler",
            )
            await pub.stage(ev, conn)

    reminded = 0
    redis = _make_redis()
    notifier = get_notifier()
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, action_class FROM approvals WHERE state = 'pending' "
                    "AND created_at <= now() - interval '6 hours'",
                )
                waiting = await cur.fetchall()
        for apv_id, action_class in waiting:
            fresh = True
            if redis is not None:
                try:  # remind once per approval (key outlives the 7d expiry)
                    fresh = bool(await redis.set(f"remind:apv:{apv_id}", "1",
                                                 nx=True, ex=7 * 24 * 3600))
                except Exception:
                    fresh = False  # no redis dedupe → stay quiet rather than spam hourly
            if fresh:
                await notifier.notify(
                    f"Approval {apv_id} ({action_class}) has been waiting 6h+ — "
                    "grant or deny it in Mission Control.",
                    approval_id=apv_id,
                )
                reminded += 1
    finally:
        if redis is not None:
            try:
                await redis.aclose()
            except Exception:
                pass
    return {"expired": len(expired_rows), "reminded": reminded}


async def _decision_review_nudge_job() -> dict:
    """Daily: surface decisions whose review_at has passed without an outcome.
    One notification naming up to 5; the /decisions page shows the full list."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from goals import list_decisions
    from shared.notify import get_notifier

    pool = await _get_async_pool(row_factory=psycopg.rows.tuple_row)
    due = await list_decisions(pool, SCOPE, due_for_review=True)
    if due:
        titles = "; ".join(d["title"][:60] for d in due[:5])
        await get_notifier().notify(
            f"{len(due)} decision(s) due for outcome review: {titles}"
            + (" …" if len(due) > 5 else ""),
        )
    return {"due_for_review": len(due)}


def _scheduler_jobs():
    from orchestration.scheduler import DailyAt, Every, Job, MonthlyAt, WeeklyAt

    async def _discovery_scan_job() -> dict:
        from orchestration.workflows import discovery_scan

        return await discovery_scan(
            pool=await _get_async_pool(row_factory=psycopg.rows.tuple_row),
            publisher=_make_publisher_sync(),
            embedder=await get_shared_embedder(),
            redis=_make_redis(),
            llm=_runner_llm(),
            scope=SCOPE,
        )

    return [
        Job("decision_review_nudge", DailyAt("09:00"), _decision_review_nudge_job),
        Job("discovery_scan", WeeklyAt((2,), "20:00"), _discovery_scan_job),
        # consolidation is internally locked per scope — overlap-safe at any cadence
        Job("consolidation_fast", Every(15),
            lambda: consolidation_run(ConsolidationRunRequest(mode="fast"))),
        Job("consolidation_deep", DailyAt("03:05"),
            lambda: consolidation_run(ConsolidationRunRequest(mode="deep"))),
        # LLM-bearing cognition: output-based period guards prevent double-spend
        Job("reflection_weekly", WeeklyAt((0,), "06:20"),     # Hermes: Mon 06:00
            lambda: reflection_weekly(None),
            period_guard=_reflection_period_guard("weekly", "week")),
        Job("reflection_monthly", MonthlyAt(1, "06:40"),      # no Hermes equivalent
            lambda: reflection_monthly(None),
            period_guard=_reflection_period_guard("monthly", "month")),
        Job("meta_audit", MonthlyAt(1, "07:20"),              # Hermes: 1st 07:00
            lambda: meta_audit(None),
            period_guard=_table_period_guard("decision_audits", "month")),
        Job("narrative_generate", WeeklyAt((6,), "05:20"),    # Hermes: Sun 05:00
            lambda: narrative_generate(None),
            period_guard=_table_period_guard("themes", "week")),
        # heuristic, no LLM — duplicates are harmless
        Job("attention_scan", WeeklyAt((0, 3), "08:10"),      # Hermes: Mon,Thu 08:00
            lambda: attention_scan(None)),
        Job("approval_silence", Every(60), _approval_silence_job),
    ]


@app.get("/api/scheduler")
async def scheduler_status(request: Request):
    """The heartbeat table: every background job, its cadence, last/next fire."""
    sched = getattr(request.app.state, "scheduler", None)
    if sched is None:
        return {"running": False, "reason": "CURLYOS_SCHEDULER disabled", "jobs": []}
    return sched.snapshot()
