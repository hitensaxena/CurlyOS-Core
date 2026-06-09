"""CurlyOS API server — serves memory, knowledge graph, identity, cognition data.

Runs as a FastAPI app on port 8642. Called by Next.js API routes or directly.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

import psycopg
import psycopg.rows
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DSN = os.environ.get("CURLYOS_DATABASE_URL", "postgresql://curlyos:***@localhost:54321/curlyos")
REDIS_URL = os.environ.get("CURLYOS_REDIS_URL", "")
SCOPE = os.environ.get("CURLYOS_SCOPE", "user:usr_hiten")

app = FastAPI(title="CurlyOS API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_conn():
    return psycopg.connect(DSN, row_factory=psycopg.rows.dict_row, autocommit=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Health + Stats
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health():
    result = {"timestamp": now_iso(), "postgres": {}, "redis": {}, "embedder": {}}

    # Postgres
    try:
        conn = get_conn()
        ver = conn.execute("SELECT version() AS v").fetchone()["v"]
        has_vec = conn.execute("SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone()
        conn.close()
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


@app.get("/api/stats")
async def stats():
    conn = get_conn()
    counts = {}
    for t in ["episodes", "memories", "identity_facts", "knowledge_entities", "knowledge_edges"]:
        try:
            counts[t] = conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"]
        except Exception:
            counts[t] = 0
    conn.close()
    return counts


# ---------------------------------------------------------------------------
# Memories
# ---------------------------------------------------------------------------

@app.get("/api/memories")
async def list_memories(
    scope: str = SCOPE,
    kind: str | None = None,
    epistemic_status: str | None = None,
    valid: bool | None = True,
    limit: int = Query(default=50, le=200),
    offset: int = 0,
    q: str | None = None,
):
    conn = get_conn()
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
    rows = conn.execute(
        f"SELECT * FROM memories WHERE {where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
        params + [limit, offset],
    ).fetchall()
    conn.close()
    return {"items": rows, "count": len(rows)}


@app.get("/api/memories/{mem_id}")
async def get_memory(mem_id: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM memories WHERE id = %s", [mem_id]).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Memory not found")
    # Get source episode
    epi = conn.execute("SELECT * FROM episodes WHERE id = %s", [row["source_episode_id"]]).fetchone()
    # Get superseded_by memory if any
    sup = None
    if row.get("superseded_by"):
        sup = conn.execute("SELECT id, statement FROM memories WHERE id = %s", [row["superseded_by"]]).fetchone()
    conn.close()
    return {"memory": row, "source_episode": epi, "superseded_by": sup}


@app.post("/api/memories")
async def add_memory(request: Request):
    body = await request.json()
    statement = body.get("statement", "")
    source_episode_id = body.get("source_episode_id", "")
    kind = body.get("kind", "fact")
    epistemic_status = body.get("epistemic_status", "canonical")

    if not statement:
        raise HTTPException(400, "statement is required")
    if not source_episode_id:
        raise HTTPException(400, "source_episode_id is required")

    conn = get_conn()
    try:
        row = conn.execute(
            "INSERT INTO memories (id, scope, statement, statement_key, kind, tier, "
            "epistemic_status, valid_from, ingested_at, source_episode_id) "
            "VALUES (gen_random_uuid()::text, %s, %s, %s, %s, 'semantic', %s, now(), now(), %s) "
            "RETURNING id, created_at",
            [SCOPE, statement, statement.lower().strip(), kind,
             epistemic_status, source_episode_id],
        ).fetchone()
        conn.close()
        return {"id": row["id"], "created_at": row["created_at"].isoformat() if row else None}
    except Exception as e:
        conn.close()
        if "23503" in str(e):
            raise HTTPException(400, f"source_episode_id not found: {source_episode_id}")
        raise HTTPException(500, str(e))


@app.post("/api/memories/{mem_id}/invalidate")
async def invalidate_memory(mem_id: str, request: Request):
    body = await request.json() if await request.body() else {}
    reason = body.get("reason", "")

    conn = get_conn()
    row = conn.execute("SELECT valid_to FROM memories WHERE id = %s", [mem_id]).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Memory not found")
    if row["valid_to"] is not None:
        conn.close()
        raise HTTPException(409, "Already invalidated")
    conn.execute("UPDATE memories SET valid_to = now() WHERE id = %s", [mem_id])
    conn.close()
    return {"id": mem_id, "valid_to": now_iso(), "deleted": False}


# ---------------------------------------------------------------------------
# Episodes
# ---------------------------------------------------------------------------

@app.get("/api/episodes")
async def list_episodes(
    scope: str = SCOPE,
    limit: int = Query(default=50, le=200),
    offset: int = 0,
):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM episodes WHERE scope = %s ORDER BY created_at DESC LIMIT %s OFFSET %s",
        [scope, limit, offset],
    ).fetchall()
    conn.close()
    return {"items": rows, "count": len(rows)}


@app.get("/api/episodes/{epi_id}")
async def get_episode(epi_id: str):
    conn = get_conn()
    epi = conn.execute("SELECT * FROM episodes WHERE id = %s", [epi_id]).fetchone()
    if not epi:
        conn.close()
        raise HTTPException(404, "Episode not found")
    mems = conn.execute(
        "SELECT id, statement, epistemic_status, valid_from, valid_to FROM memories WHERE source_episode_id = %s",
        [epi_id],
    ).fetchall()
    conn.close()
    return {"episode": epi, "memories": mems}


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

@app.get("/api/identity")
async def list_identity(scope: str = SCOPE, predicates: str | None = None):
    conn = get_conn()
    if predicates:
        preds = [p.strip() for p in predicates.split(",")]
        rows = conn.execute(
            "SELECT * FROM identity_facts WHERE scope = %s AND predicate = ANY(%s) AND valid_to IS NULL ORDER BY confidence DESC",
            [scope, preds],
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM identity_facts WHERE scope = %s AND valid_to IS NULL ORDER BY confidence DESC",
            [scope],
        ).fetchall()
    conn.close()
    return {"items": rows, "count": len(rows)}


@app.post("/api/identity")
async def propose_identity(request: Request):
    body = await request.json()
    predicate = body.get("predicate", "")
    obj = body.get("object", "")
    confidence = float(body.get("confidence", 0.5))
    source_episode_id = body.get("source_episode_id", "")

    if not predicate:
        raise HTTPException(400, "predicate is required")
    if not obj:
        raise HTTPException(400, "object is required")

    conn = get_conn()
    try:
        ep_status = "canonical" if confidence >= 0.75 else "hypothesis"
        row = conn.execute(
            "INSERT INTO identity_facts (id, scope, predicate, object, confidence, "
            "epistemic_status, valid_from, ingested_at, source_episode_id) "
            "VALUES (gen_random_uuid()::text, %s, %s, %s, %s, %s, now(), now(), %s) "
            "RETURNING id",
            [SCOPE, predicate, obj, confidence, ep_status, source_episode_id],
        ).fetchone()
        conn.close()
        return {"id": row["id"], "predicate": predicate, "object": obj,
                "confidence": confidence, "epistemic_status": ep_status}
    except Exception as e:
        conn.close()
        raise HTTPException(500, str(e))


# ---------------------------------------------------------------------------
# Knowledge Graph
# ---------------------------------------------------------------------------

@app.get("/api/graph")
async def get_graph(scope: str = SCOPE, limit: int = Query(default=200, le=500)):
    conn = get_conn()
    entities = conn.execute(
        "SELECT id, name, label, properties, epistemic_status FROM knowledge_entities WHERE scope = %s AND valid_to IS NULL ORDER BY created_at DESC LIMIT %s",
        [scope, limit],
    ).fetchall()
    entity_ids = [e["id"] for e in entities]
    edges = []
    if entity_ids:
        edges = conn.execute(
            "SELECT id, src_entity_id, dst_entity_id, rel_type, properties FROM knowledge_edges WHERE src_entity_id = ANY(%s) AND valid_to IS NULL",
            [entity_ids],
        ).fetchall()
    conn.close()

    # Build degree map
    degree: dict[str, int] = {}
    for e in edges:
        degree[e["src_entity_id"]] = degree.get(e["src_entity_id"], 0) + 1
        degree[e["dst_entity_id"]] = degree.get(e["dst_entity_id"], 0) + 1

    nodes = [
        {"id": e["id"], "name": e["name"], "label": e["label"], "degree": degree.get(e["id"], 0)}
        for e in entities
    ]
    links = [
        {"source": e["src_entity_id"], "target": e["dst_entity_id"], "rel_type": e["rel_type"]}
        for e in edges
    ]
    return {"nodes": nodes, "links": links}


@app.get("/api/graph/{entity_id}/expand")
async def expand_graph(entity_id: str, k: int = Query(default=1, le=3)):
    conn = get_conn()
    visited = {entity_id}
    frontier = [entity_id]
    all_edges = []

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
    conn.close()
    return {"entities": entities, "edges": all_edges}


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@app.get("/api/search")
async def search(q: str, scope: str = SCOPE, limit: int = Query(default=20, le=50)):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, statement, kind, valid_from, valid_to, source_episode_id, epistemic_status, "
        "ts_rank(to_tsvector('english', statement), plainto_tsquery('english', %s)) AS score "
        "FROM memories WHERE scope = %s AND valid_to IS NULL "
        "AND to_tsvector('english', statement) @@ plainto_tsquery('english', %s) "
        "ORDER BY score DESC LIMIT %s",
        [q, scope, q, limit],
    ).fetchall()
    conn.close()
    return {"query": q, "items": rows, "count": len(rows)}


# Cached warm embedder for semantic recall (avoids reloading bge-m3 per request).
_RECALL_EMBEDDER = None


async def _get_recall_embedder():
    """Return a process-cached LocalBgeM3 (warm). Requires sentence_transformers,
    which is present in the curlyos-core venv this API runs in."""
    global _RECALL_EMBEDDER
    if _RECALL_EMBEDDER is None:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from shared.embeddings.implementations import LocalBgeM3
        emb = LocalBgeM3()
        await emb.embed(["warmup"])
        _RECALL_EMBEDDER = emb
    return _RECALL_EMBEDDER


@app.post("/api/recall")
async def recall(request: Request):
    """Semantic + graph retrieval (dense pgvector + sparse + entity + graph + rerank).

    This is the authoritative recall path for the Hermes `curlyos` plugin, which
    cannot embed in-process (its venv lacks sentence_transformers). The plugin
    HTTP-calls this endpoint so embedding runs here, in the ST-capable venv.

    Body: {"query": "...", "scope": "user:usr_hiten", "mode": "fast"|"deep"|"divergent", "k": 6}
    """
    body = await request.json()
    query = body.get("query", "")
    scope = body.get("scope", SCOPE)
    mode = body.get("mode", "fast")
    k = min(int(body.get("k", 6)), 20)
    if not query:
        raise HTTPException(400, "query is required")

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from memory.retrieval import retrieve
    from shared.types import RetrievalRequest
    from shared.embeddings.implementations import FakeReranker

    # memory.retrieval uses positional/tuple row access → tuple_row (see consolidation).
    pool = await _get_async_pool(row_factory=psycopg.rows.tuple_row)
    try:
        emb = await _get_recall_embedder()
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
        import traceback
        return {"error": str(e), "traceback": traceback.format_exc(), "results": [], "count": 0}
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# Cognition
# ---------------------------------------------------------------------------

@app.get("/api/cognition/meta")
async def cognition_meta(scope: str = SCOPE):
    conn = get_conn()
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
    conn.close()
    return {
        "principles": principles,
        "assumptions": assumptions,
        "decision_audits": decision_audits,
    }


@app.get("/api/cognition/reflection")
async def cognition_reflection(scope: str = SCOPE):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM reflection_reports WHERE scope = %s ORDER BY created_at DESC LIMIT 10",
        [scope],
    ).fetchall()
    conn.close()
    return {"reports": rows}


@app.get("/api/cognition/attention")
async def cognition_attention(scope: str = SCOPE, window_days: int = 7):
    conn = get_conn()
    gaps = conn.execute(
        "SELECT id, signal_type, description, severity, epistemic_status FROM alignment_signals WHERE scope = %s AND valid_to IS NULL ORDER BY severity DESC",
        [scope],
    ).fetchall()
    conn.close()

    # Allocation (time-by-category + trend) and cognitive load are computed
    # read-only — no LLM, no writes — so the GET can surface them live for the
    # dashboard. POST /api/attention/scan additionally writes fresh
    # alignment_signals; that mutation isn't needed just to display the numbers.
    allocation = None
    cognitive_load = None
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from cognition.attention import get_allocation, estimate_cognitive_load
        pool = await _get_async_pool()
        try:
            allocation = await get_allocation(pool=pool, scope=scope, window_days=window_days)
            cognitive_load = await estimate_cognitive_load(pool=pool, scope=scope, window_days=14)
        finally:
            await pool.close()
    except Exception:
        # Allocation/load are best-effort enrichment; gaps still render without them.
        pass

    return {"alignment_gaps": gaps, "allocation": allocation, "cognitive_load": cognitive_load}


@app.get("/api/cognition/narrative")
async def cognition_narrative(scope: str = SCOPE):
    conn = get_conn()
    chapters = conn.execute(
        "SELECT id, title, summary, start_date, end_date, epistemic_status FROM life_chapters WHERE scope = %s ORDER BY start_date DESC",
        [scope],
    ).fetchall()
    themes = conn.execute(
        "SELECT id, name, description, frequency, epistemic_status FROM themes WHERE scope = %s ORDER BY frequency DESC",
        [scope],
    ).fetchall()
    conn.close()
    return {"chapters": chapters, "themes": themes}


@app.post("/api/cognition/narrative/compose")
async def compose_narrative(request: Request):
    body = await request.json()
    query = body.get("query", "")
    since = body.get("since")
    domain = body.get("domain")

    if not query:
        raise HTTPException(400, "query is required")

    like = f"%{query}%"
    conn = get_conn()
    # Material for the narrative: episodes/memories matching the query first,
    # then recent context so there's always something to weave from.
    if since:
        rel_eps = conn.execute(
            "SELECT id, content, created_at FROM episodes "
            "WHERE scope = %s AND content ILIKE %s AND created_at >= %s "
            "ORDER BY created_at DESC LIMIT 20",
            [SCOPE, like, since],
        ).fetchall()
    else:
        rel_eps = conn.execute(
            "SELECT id, content, created_at FROM episodes "
            "WHERE scope = %s AND content ILIKE %s ORDER BY created_at DESC LIMIT 20",
            [SCOPE, like],
        ).fetchall()
    recent_eps = conn.execute(
        "SELECT id, content, created_at FROM episodes WHERE scope = %s ORDER BY created_at DESC LIMIT 12",
        [SCOPE],
    ).fetchall()
    memories = conn.execute(
        "SELECT id, statement, created_at FROM memories "
        "WHERE scope = %s AND valid_to IS NULL AND statement ILIKE %s "
        "ORDER BY created_at DESC LIMIT 20",
        [SCOPE, like],
    ).fetchall()
    conn.close()

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
async def list_events(
    scope: str = SCOPE,
    limit: int = Query(default=50, le=100),
    offset: int = 0,
):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, type, subject, scope, data, seq, created_at FROM events WHERE scope = %s ORDER BY seq DESC LIMIT %s OFFSET %s",
        [scope, limit, offset],
    ).fetchall()
    conn.close()
    return {"items": rows, "count": len(rows)}


# ---------------------------------------------------------------------------
# Studio
# ---------------------------------------------------------------------------

@app.get("/api/studio")
async def list_studios(scope: str = SCOPE):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, scope, title, status, properties, created_at, updated_at FROM studios WHERE scope = %s ORDER BY updated_at DESC",
        [scope],
    ).fetchall()
    conn.close()
    return {"items": rows, "count": len(rows)}


@app.post("/api/studio")
async def create_studio(request: Request):
    body = await request.json()
    title = body.get("title", "")
    properties = body.get("properties", {})

    if not title:
        raise HTTPException(400, "title is required")

    conn = get_conn()
    row = conn.execute(
        "INSERT INTO studios (id, scope, title, status, properties) "
        "VALUES (gen_random_uuid()::text, %s, %s, 'active', %s) "
        "RETURNING id, created_at",
        [SCOPE, title, json.dumps(properties)],
    ).fetchone()
    conn.close()
    return {"id": row["id"], "title": title, "status": "active",
            "created_at": row["created_at"].isoformat() if row else None}


@app.get("/api/studio/{studio_id}")
async def get_studio(studio_id: str):
    conn = get_conn()
    studio = conn.execute("SELECT * FROM studios WHERE id = %s", [studio_id]).fetchone()
    if not studio:
        conn.close()
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
    conn.close()
    return {"studio": studio, "sketches": sketches, "links": links}


@app.post("/api/studio/{studio_id}/sketch")
async def create_sketch(studio_id: str, request: Request):
    body = await request.json()
    content = body.get("content", "")
    kind = body.get("kind", "text")
    properties = body.get("properties", {})

    if not content:
        raise HTTPException(400, "content is required")

    conn = get_conn()
    # Verify studio exists
    studio = conn.execute("SELECT id FROM studios WHERE id = %s", [studio_id]).fetchone()
    if not studio:
        conn.close()
        raise HTTPException(404, "Studio not found")
    row = conn.execute(
        "INSERT INTO studio_sketches (id, studio_id, content, kind, epistemic_status, properties) "
        "VALUES (gen_random_uuid()::text, %s, %s, %s, 'seed', %s) "
        "RETURNING id, created_at",
        [studio_id, content, kind, json.dumps(properties)],
    ).fetchone()
    conn.close()
    return {"id": row["id"], "studio_id": studio_id, "content": content,
            "kind": kind, "epistemic_status": "seed",
            "created_at": row["created_at"].isoformat() if row else None}


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

@app.get("/api/simulation/runs")
async def list_simulation_runs(scope: str = SCOPE):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, scope, question, world_model_id, status, epistemic_status, "
        "outcome_distribution, parameters, created_at, completed_at "
        "FROM simulation_runs WHERE scope = %s ORDER BY created_at DESC",
        [scope],
    ).fetchall()
    conn.close()
    return {"items": rows, "count": len(rows)}


@app.post("/api/simulation/runs")
async def create_simulation_run(request: Request):
    body = await request.json()
    question = body.get("question", "")
    world_model_id = body.get("world_model_id")
    parameters = body.get("parameters", {})

    if not question:
        raise HTTPException(400, "question is required")

    conn = get_conn()
    row = conn.execute(
        "INSERT INTO simulation_runs (id, scope, question, world_model_id, status, epistemic_status, parameters) "
        "VALUES (gen_random_uuid()::text, %s, %s, %s, 'created', 'possible_world', %s) "
        "RETURNING id, created_at",
        [SCOPE, question, world_model_id, json.dumps(parameters)],
    ).fetchone()
    conn.close()
    return {"id": row["id"], "question": question, "status": "created",
            "epistemic_status": "possible_world",
            "created_at": row["created_at"].isoformat() if row else None}


# ---------------------------------------------------------------------------
# Workspaces + Projects
# ---------------------------------------------------------------------------

@app.get("/api/workspaces")
async def list_workspaces(scope: str = SCOPE):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, scope, name, kind, properties, created_at, updated_at "
        "FROM workspaces WHERE scope = %s ORDER BY updated_at DESC",
        [scope],
    ).fetchall()
    conn.close()
    return {"items": rows, "count": len(rows)}


@app.post("/api/workspaces")
async def create_workspace(request: Request):
    body = await request.json()
    name = body.get("name", "")
    kind = body.get("kind", "project")
    properties = body.get("properties", {})

    if not name:
        raise HTTPException(400, "name is required")

    conn = get_conn()
    row = conn.execute(
        "INSERT INTO workspaces (id, scope, name, kind, properties) "
        "VALUES (gen_random_uuid()::text, %s, %s, %s, %s) "
        "RETURNING id, created_at",
        [SCOPE, name, kind, json.dumps(properties)],
    ).fetchone()
    conn.close()
    return {"id": row["id"], "name": name, "kind": kind,
            "created_at": row["created_at"].isoformat() if row else None}


@app.get("/api/projects")
async def list_projects(workspace_id: str | None = None, scope: str = SCOPE):
    conn = get_conn()
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
    conn.close()
    return {"items": rows, "count": len(rows)}


@app.post("/api/projects")
async def create_project(request: Request):
    body = await request.json()
    workspace_id = body.get("workspace_id", "")
    name = body.get("name", "")
    properties = body.get("properties", {})

    if not workspace_id:
        raise HTTPException(400, "workspace_id is required")
    if not name:
        raise HTTPException(400, "name is required")

    conn = get_conn()
    # Verify workspace exists
    ws = conn.execute("SELECT id FROM workspaces WHERE id = %s", [workspace_id]).fetchone()
    if not ws:
        conn.close()
        raise HTTPException(404, f"Workspace not found: {workspace_id}")
    row = conn.execute(
        "INSERT INTO projects (id, workspace_id, name, status, properties) "
        "VALUES (gen_random_uuid()::text, %s, %s, 'active', %s) "
        "RETURNING id, created_at",
        [workspace_id, name, json.dumps(properties)],
    ).fetchone()
    conn.close()
    return {"id": row["id"], "workspace_id": workspace_id, "name": name,
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
async def log_sources():
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
async def get_logs(
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


@app.get("/api/systems")
async def systems():
    """Aggregate a single per-system status payload for the dashboard."""
    # Reuse the existing health() + stats() endpoint logic.
    health_data = await health()
    stats_data = await stats()

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
        }

    try:
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
    finally:
        conn.close()

    return {
        "timestamp": now_iso(),
        "infrastructure": infrastructure,
        "stats": stats_data,
        "engines": engines,
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

async def _get_async_pool(row_factory=None):
    """Create an AsyncConnectionPool for the cognition endpoints.

    Defaults to tuple_row: the entire curlyos-core stack (governance,
    retrieval, identity, consolidation, meta, reflection) uses positional
    row access (r[0]/r[1] and tuple unpacking). Endpoints that genuinely
    need column-by-name access can pass row_factory=psycopg.rows.dict_row.
    """
    import psycopg_pool
    if row_factory is None:
        row_factory = psycopg.rows.tuple_row
    pool = psycopg_pool.AsyncConnectionPool(
        DSN,
        min_size=1,
        max_size=3,
        kwargs={"row_factory": row_factory},
        open=False,
    )
    await pool.open()
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
    """Resolve an API key from the process env, falling back to ~/.hermes/.env.

    The API server is started as a subprocess that does not inherit the
    Hermes .env, so credentials like OPENROUTER_API_KEY live only in the file.
    """
    for n in names:
        v = os.environ.get(n, "")
        if v:
            return v
    env_path = os.path.join(os.path.expanduser("~"), ".hermes", ".env")
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


def _make_llm_client():
    """Build an OpenRouter-backed AsyncOpenAI client + model, or (None, "").

    Returns (client, model). Returns (None, "") when no key is available or
    the openai SDK is missing — callers must fall back to heuristics.
    """
    key = _load_env_key("OPENROUTER_API_KEY")
    if not key:
        return None, ""
    try:
        from openai import AsyncOpenAI
    except Exception:
        return None, ""
    model = os.environ.get("CURLYOS_LLM_MODEL", "openai/gpt-4o-mini")
    client = AsyncOpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=key,
        timeout=60.0,
        max_retries=2,
    )
    return client, model


_embedder_singleton: Any = None


async def _get_embedder_cached():
    """Cached embedder so repeated ingests don't reload bge-m3 each time.

    Caches only a real LocalBgeM3; a FakeEmbedder fallback is returned fresh
    (not cached) so a later call can still pick up a real model once available.
    """
    global _embedder_singleton
    if _embedder_singleton is not None:
        return _embedder_singleton
    emb = await _make_embedder()
    from shared.embeddings.implementations import FakeEmbedder
    if not isinstance(emb, FakeEmbedder):
        _embedder_singleton = emb
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
        import traceback
        traceback.print_exc()
        return False


async def _process_episode_bg(
    scope: str, epi_id: str, mem_id: str | None, text: str, extract_knowledge: bool = True,
) -> None:
    """Background processing for an ingested episode. Embeds the episode + its
    memory (so both are dense-recallable immediately), then runs knowledge
    extraction (LLM → graph; regex fallback). Runs after the HTTP response so
    capture stays snappy. Manages its own pool/embedder/LLM; best-effort.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from knowledge.graph import extract_and_project

    pool = await _get_async_pool(row_factory=psycopg.rows.tuple_row)
    pub = _make_publisher_sync()
    try:
        embedder = await _get_embedder_cached()
        # Embeddings first — these make the entry dense-recallable right away.
        await _embed_row(pool, "episodes", epi_id, text, embedder)
        if mem_id:
            await _embed_row(pool, "memories", mem_id, text[:4000], embedder)
        # Then knowledge-graph extraction (entities/edges).
        if extract_knowledge:
            llm_client, _ = _make_llm_client()
            await extract_and_project(
                pool, pub, scope, epi_id, text,
                embedder=embedder, llm_client=llm_client,
            )
    except Exception:
        import traceback
        traceback.print_exc()
    finally:
        await pool.close()


@app.post("/api/ingest")
async def ingest(request: Request, background_tasks: BackgroundTasks):
    """Record raw text as an episode in curlyos-memory and process it.

    Used by the web app (journal capture). Records an episode, appends a
    recallable memory, and schedules LLM knowledge extraction in the
    background so the caller returns immediately.

    Body: {"text": str, "source_ref"?: str, "scope"?: str,
           "add_memory"?: bool (default true),
           "extract_knowledge"?: bool (default true)}
    """
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    source_ref = body.get("source_ref") or "web:capture"
    scope = body.get("scope") or SCOPE
    add_memory = body.get("add_memory", True)
    extract_knowledge = body.get("extract_knowledge", True)

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
                kind="fact",
                # canonical so it's recallable: fast/deep recall filter to
                # epistemic_status='canonical' only ("belief" is in no mode's
                # filter, so belief memories never surface). A journal/voice
                # capture is a first-person record, so canonical is apt.
                epistemic_status="canonical",
            )
            result["mem_id"] = mem.get("mem_id")
    except Exception as e:
        import traceback
        return {"error": str(e), "traceback": traceback.format_exc()}
    finally:
        await pool.close()

    if epi_id:
        background_tasks.add_task(
            _process_episode_bg, scope, epi_id, result.get("mem_id"), text, extract_knowledge,
        )
    result["processing"] = "scheduled" if epi_id else "skipped"
    return result


@app.post("/api/consolidation/run")
async def consolidation_run(request: Request):
    """Run consolidation passes.

    Body: {"mode": "fast"|"deep", "scope": "user:usr_hiten"}
      fast  = dedup + conflict_resolve
      deep  = all passes (dedup, merge_promote, conflict_resolve, summarize, decay, recombine_incubate)
    """
    body = await request.json()
    mode = body.get("mode", "fast")
    scope = body.get("scope", SCOPE)
    deep = mode == "deep"

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from memory.consolidation import run_consolidation

    # Consolidation uses positional/tuple row access — force tuple_row.
    pool = await _get_async_pool(row_factory=psycopg.rows.tuple_row)
    redis = _make_redis()
    try:
        result = await run_consolidation(
            pool=pool,
            redis=redis,
            embedder=await _make_embedder(),
            publisher=_make_publisher_sync(),
            scope=scope,
            deep=deep,
        )
        return result
    except Exception as e:
        import traceback
        return {"error": str(e), "traceback": traceback.format_exc(), "scope": scope, "deep": deep}
    finally:
        await pool.close()
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
async def reflection_weekly(request: Request):
    """Run a weekly reflection over the past 7 days.

    Body: {"scope": "user:usr_hiten", "window_days": 7}
    """
    body = await request.json()
    scope = body.get("scope", SCOPE)
    window_days = int(body.get("window_days", 7))

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
        return result
    except Exception as e:
        import traceback
        return {"error": str(e), "traceback": traceback.format_exc(), "scope": scope, "type": "weekly"}
    finally:
        await pool.close()


@app.post("/api/reflection/monthly")
async def reflection_monthly(request: Request):
    """Run a monthly reflection over the past 30 days.

    Body: {"scope": "user:usr_hiten"}
    """
    body = await request.json()
    scope = body.get("scope", SCOPE)

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
        return result
    except Exception as e:
        import traceback
        return {"error": str(e), "traceback": traceback.format_exc(), "scope": scope, "type": "monthly"}
    finally:
        await pool.close()


@app.post("/api/meta/audit")
async def meta_audit(request: Request):
    """Run a decision audit + principle distillation over recent episodes.

    Body: {"scope": "user:usr_hiten", "window_days": 30}
    """
    body = await request.json()
    scope = body.get("scope", SCOPE)
    window_days = int(body.get("window_days", 30))

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
            pool, _make_publisher_sync(), await _get_embedder_cached(), scope,
        )
        return {
            "audit": audit_result,
            "principles_distilled": len(principles),
            "principles": principles,
            **sync,
        }
    except Exception as e:
        import traceback
        return {"error": str(e), "traceback": traceback.format_exc(), "scope": scope}
    finally:
        await pool.close()


@app.post("/api/meta/distill")
async def meta_distill(request: Request):
    """Distill principles from decision audits.

    Body: {"scope": "user:usr_hiten", "min_confidence": 0.7}
    """
    body = await request.json()
    scope = body.get("scope", SCOPE)
    min_confidence = float(body.get("min_confidence", 0.7))

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
            pool, _make_publisher_sync(), await _get_embedder_cached(), scope,
        )
        return {"principles_distilled": len(principles), "principles": principles, **sync}
    except Exception as e:
        import traceback
        return {"error": str(e), "traceback": traceback.format_exc(), "scope": scope}
    finally:
        await pool.close()


@app.post("/api/narrative/generate")
async def narrative_generate(request: Request):
    """Surface themes + compose life chapters from episodes.

    Body: {"scope": "user:usr_hiten", "min_frequency": 3}
    Each run supersedes prior hypothesis themes/chapters (invalidate-not-delete)
    so the active set stays a fresh, bounded snapshot.
    """
    body = await request.json()
    scope = body.get("scope", SCOPE)
    min_frequency = int(body.get("min_frequency", 3))

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from cognition.narrative import surface_themes, compose_chapters

    pool = await _get_async_pool()
    try:
        themes = await surface_themes(
            pool=pool, publisher=_make_publisher_sync(),
            scope=scope, min_frequency=min_frequency,
        )
        chapters = await compose_chapters(
            pool=pool, publisher=_make_publisher_sync(), scope=scope,
        )
        return {
            "scope": scope,
            "themes_surfaced": len(themes),
            "chapters_composed": len(chapters),
            "top_themes": [t.get("name") for t in themes[:8]],
        }
    except Exception as e:
        import traceback
        return {"error": str(e), "traceback": traceback.format_exc(), "scope": scope}
    finally:
        await pool.close()


@app.post("/api/attention/scan")
async def attention_scan(request: Request):
    """Detect value-action alignment gaps + snapshot allocation & cognitive load.

    Body: {"scope": "user:usr_hiten", "window_days": 14}
    Alignment gaps are written as hypothesis-status alignment_signals (each run
    supersedes prior hypothesis signals). Allocation/load are computed read-only.
    """
    body = await request.json()
    scope = body.get("scope", SCOPE)
    window_days = int(body.get("window_days", 14))

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
        import traceback
        return {"error": str(e), "traceback": traceback.format_exc(), "scope": scope}
    finally:
        await pool.close()
