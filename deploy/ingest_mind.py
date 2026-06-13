"""Ingest the full ~/mind markdown vault into curlyos-core as episodes +
chunked, locally-embedded memories, then (optionally) mine the knowledge graph
via the Claude Max hermes-bridge.

Why this exists: import_mind.py only ever read knowledge_triples.json (distilled
triples) -> 20 ai-context summary episodes. The RAW vault (memoirs, journals,
relationships, archives, ...) was never chunked or embedded, so semantic recall
had nothing to find on e.g. "life in Delhi". Recall (/api/recall) is dense
search over `memories` embeddings, so the fix is: one episode per file + many
chunk memories, each embedded with the local bge-m3.

Two resumable, idempotent phases (env PHASE=embed|extract|both, default both):

  embed    per .md file -> 1 episode (source_ref 'mind:<relpath>') + N chunk
           memories. Embeds with the LOCAL bge-m3 (free, no rate limit, no
           third-party exposure). Writes via DIRECT SQL so it does NOT publish
           memory.episode.recorded -> the live consolidation worker won't
           re-extract these via OpenRouter. Idempotent two ways: skips files
           whose episode already exists, AND embeds any mind row still NULL
           (so an interrupted run resumes cleanly).

  extract  per 'mind:' episode that has no real (non-inferred) edges yet ->
           run extract_and_project over its content in segments. LLM = Claude
           Max via the hermes-bridge (:8787), model CURLYOS extract model.
           Idempotent on the presence of extracted edges.

Run from repo root with env sourced (BRIDGE_API_KEY from the hermes-bridge env):

    set -a; . ./.env; set +a
    export BRIDGE_API_KEY=$(grep -E '^BRIDGE_API_KEY=' ~/hermes-bridge/.env | cut -d= -f2-)

    PHASE=embed   .venv/bin/python deploy/ingest_mind.py     # fast, free, local
    PHASE=extract .venv/bin/python deploy/ingest_mind.py     # Claude Max graph mining
"""
import asyncio
import logging
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from psycopg_pool import AsyncConnectionPool
from shared.embeddings.implementations import LocalBgeM3
from shared.types.ulid import mint

logging.basicConfig(level=logging.WARNING)

DSN = os.environ["CURLYOS_DATABASE_URL"]
SCOPE = os.environ.get("CURLYOS_SCOPE", "user:usr_hiten")
MIND_DIR = Path(os.environ.get("MIND_DIR", str(Path.home() / "mind")))
PHASE = os.environ.get("PHASE", "both").lower()

CHUNK = int(os.environ.get("CHUNK", "2000"))          # target chars per memory chunk
EPI_EMBED_CAP = int(os.environ.get("EPI_EMBED_CAP", "8000"))
EMBED_BATCH = int(os.environ.get("EMBED_BATCH", "32")) # texts per bge-m3 encode

EXTRACT_MODEL = os.environ.get("EXTRACT_MODEL", "claude-sonnet-4-6")
EXTRACT_SEG = int(os.environ.get("EXTRACT_SEG", "4500"))   # chars per extraction call
EXTRACT_MAX_SEG = int(os.environ.get("EXTRACT_MAX_SEG", "3"))  # segments per file cap
EXTRACT_CONC = int(os.environ.get("EXTRACT_CONC", "3"))    # concurrent extractions
BRIDGE_URL = os.environ.get("BRIDGE_URL", "http://localhost:8787/v1")


def log(m: str) -> None:
    print(m, flush=True)


def statement_key(statement: str) -> str:
    # replicated from memory.governance (avoid importing the event-bus heavy module)
    return re.sub(r"\s+", " ", statement.strip().lower()).rstrip(" .!?,;:")


def vec_lit(v) -> str:
    return "[" + ",".join(repr(float(x)) for x in v) + "]"


EXCLUDE_TOP = {x for x in os.environ.get("EXCLUDE_TOP", "").split(",") if x}
ONLY_TOP = {x for x in os.environ.get("ONLY_TOP", "").split(",") if x}


def iter_files() -> list[Path]:
    files = []
    for p in MIND_DIR.rglob("*.md"):
        if "/.git/" in str(p) or not p.is_file():
            continue
        top = p.relative_to(MIND_DIR).parts[0]
        if EXCLUDE_TOP and top in EXCLUDE_TOP:
            continue
        if ONLY_TOP and top not in ONLY_TOP:
            continue
        files.append(p)
    return sorted(files)


def chunk_text(text: str) -> list[str]:
    """Paragraph-aware chunks of ~CHUNK chars. A single oversized paragraph is
    hard-split so no chunk blows past the embedder limit."""
    paras = re.split(r"\n\s*\n", text)
    chunks: list[str] = []
    buf = ""
    for para in paras:
        para = para.strip()
        if not para:
            continue
        if len(para) > CHUNK:
            if buf:
                chunks.append(buf)
                buf = ""
            for i in range(0, len(para), CHUNK):
                chunks.append(para[i:i + CHUNK])
            continue
        if buf and len(buf) + len(para) + 2 > CHUNK:
            chunks.append(buf)
            buf = para
        else:
            buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        chunks.append(buf)
    return [c for c in chunks if c.strip()]


# ── embed phase ───────────────────────────────────────────────────────────────

async def ingest_files(pool) -> tuple[int, int, int]:
    """Insert episode + chunk memories for any file not already ingested.
    Embeddings are left NULL here and filled by embed_nulls()."""
    files = iter_files()
    log(f"vault files: {len(files)}")
    new_files = new_mems = skipped = 0
    for n, path in enumerate(files, 1):
        rel = path.relative_to(MIND_DIR).as_posix()
        source_ref = f"mind:{rel}"
        try:
            raw = path.read_text(encoding="utf-8", errors="replace").strip()
        except Exception as e:  # noqa: BLE001
            log(f"  read fail {rel}: {e}")
            continue
        if not raw:
            continue
        async with pool.connection() as c:
            cur = await c.execute(
                "SELECT 1 FROM episodes WHERE source_ref = %s LIMIT 1", (source_ref,)
            )
            if await cur.fetchone():
                skipped += 1
                continue

            epi_id = mint("epi")
            await c.execute(
                "INSERT INTO episodes (id, scope, content, source_ref) VALUES (%s, %s, %s, %s)",
                (epi_id, SCOPE, raw, source_ref),
            )
            header = f"# {rel}"
            for ch in chunk_text(raw):
                stmt = f"{header}\n\n{ch}"
                mem_id = mint("mem")
                await c.execute(
                    "INSERT INTO memories (id, scope, statement, statement_key, kind, tier, "
                    " embedding, epistemic_status, valid_from, valid_to, ingested_at, source_episode_id) "
                    "VALUES (%s, %s, %s, %s, 'fact', 'semantic', NULL, 'canonical', now(), NULL, now(), %s)",
                    (mem_id, SCOPE, stmt, statement_key(stmt), epi_id),
                )
                new_mems += 1
            new_files += 1
        if n % 100 == 0:
            log(f"  {n}/{len(files)} files; +{new_files} new, +{new_mems} mems, {skipped} skipped")
    log(f"ingest: +{new_files} episodes, +{new_mems} memories, {skipped} files already present")
    return new_files, new_mems, skipped


async def embed_nulls(pool, embedder) -> None:
    """Embed every mind episode/memory still missing an embedding. Resumable."""
    async def fetch(table, text_col, content_cap):
        join = "JOIN episodes e ON m.source_episode_id = e.id" if table == "memories" else ""
        alias = "m" if table == "memories" else "e"
        src = "e.source_ref" if table == "episodes" else "e.source_ref"
        q = (
            f"SELECT {alias}.id, {alias}.{text_col} FROM {table} {alias} {join} "
            f"WHERE {src} LIKE 'mind:%%' AND {alias}.embedding IS NULL"
        )
        async with pool.connection() as c:
            rows = await (await c.execute(q)).fetchall()
        return [(rid, (txt or "")[:content_cap]) for rid, txt in rows if (txt or "").strip()]

    for table, cap in (("episodes", EPI_EMBED_CAP), ("memories", CHUNK + 200)):
        rows = await fetch(table, "content" if table == "episodes" else "statement", cap)
        log(f"embed {table}: {len(rows)} rows need embedding")
        done = 0
        for i in range(0, len(rows), EMBED_BATCH):
            batch = rows[i:i + EMBED_BATCH]
            vecs = await embedder.embed([t for _, t in batch])
            payload = [(vec_lit(v), rid) for (rid, _), v in zip(batch, vecs)]
            async with pool.connection() as c:
                async with c.cursor() as cur:
                    await cur.executemany(
                        f"UPDATE {table} SET embedding = %s::vector WHERE id = %s", payload
                    )
            done += len(batch)
            if done % (EMBED_BATCH * 10) == 0 or done == len(rows):
                log(f"  {table}: {done}/{len(rows)} embedded")


async def embed_phase(pool, embedder) -> None:
    await ingest_files(pool)
    await embed_nulls(pool, embedder)


# ── extract phase ───────────────────────────────────────────────────────────────

async def make_llm():
    key = os.environ.get("BRIDGE_API_KEY")
    if not key:
        raise SystemExit("BRIDGE_API_KEY not set (needed for Claude Max extraction). "
                         "export BRIDGE_API_KEY=$(grep -E '^BRIDGE_API_KEY=' ~/hermes-bridge/.env | cut -d= -f2-)")
    from openai import AsyncOpenAI
    return AsyncOpenAI(base_url=BRIDGE_URL, api_key=key, timeout=120.0, max_retries=3)


async def extract_phase(pool, embedder) -> None:
    # extract_with_llm reads CURLYOS_LLM_MODEL; point it at Claude via the bridge.
    os.environ["CURLYOS_LLM_MODEL"] = EXTRACT_MODEL
    from shared.events.implementations import PgOnlyPublisher
    from knowledge.graph import extract_and_project

    llm = await make_llm()
    pub = PgOnlyPublisher()
    log(f"extract model={EXTRACT_MODEL} via {BRIDGE_URL} seg={EXTRACT_SEG} max_seg={EXTRACT_MAX_SEG} conc={EXTRACT_CONC}")

    async with pool.connection() as c:
        episodes = await (await c.execute(
            "SELECT e.id, e.content FROM episodes e "
            "WHERE e.source_ref LIKE 'mind:%%' AND e.scope = %s "
            "ORDER BY e.ingested_at", (SCOPE,)
        )).fetchall()

    # Skip episodes that already have real (non-inferred) extracted edges.
    todo = []
    async with pool.connection() as c:
        for epi_id, content in episodes:
            has = await (await c.execute(
                "SELECT 1 FROM knowledge_edges WHERE source_episode_id = %s "
                "AND valid_to IS NULL AND (properties->>'inferred') IS DISTINCT FROM 'true' LIMIT 1",
                (epi_id,),
            )).fetchone()
            if not has and (content or "").strip():
                todo.append((epi_id, content))
    log(f"episodes needing extraction: {len(todo)} / {len(episodes)}")

    sem = asyncio.Semaphore(EXTRACT_CONC)
    totals = {"ent": 0, "edge": 0, "fail": 0, "done": 0}

    async def work(epi_id, content):
        segs = [content[i:i + EXTRACT_SEG]
                for i in range(0, len(content), EXTRACT_SEG)][:EXTRACT_MAX_SEG]
        async with sem:
            for seg in segs:
                if not seg.strip():
                    continue
                try:
                    r = await extract_and_project(
                        pool, pub, SCOPE, epi_id, seg, embedder=embedder, llm_client=llm,
                    )
                    totals["ent"] += r.get("entities_created", 0)
                    totals["edge"] += r.get("edges_created", 0)
                except Exception as e:  # noqa: BLE001
                    totals["fail"] += 1
                    log(f"  extract fail {epi_id}: {e}")
        totals["done"] += 1
        if totals["done"] % 25 == 0:
            log(f"  {totals['done']}/{len(todo)} episodes; +{totals['ent']} ent +{totals['edge']} edge ({totals['fail']} fails)")

    # bounded fan-out
    tasks = [work(e, c) for e, c in todo]
    for i in range(0, len(tasks), EXTRACT_CONC * 4):
        await asyncio.gather(*tasks[i:i + EXTRACT_CONC * 4])
    log(f"extract DONE: +{totals['ent']} entities +{totals['edge']} edges, {totals['fail']} fails")


async def main() -> None:
    embedder = LocalBgeM3()
    await embedder.embed(["warmup"])
    log(f"embedder loaded; phase={PHASE}")

    pool = AsyncConnectionPool(DSN, min_size=1, max_size=5, open=False)
    await pool.open()
    try:
        if PHASE in ("embed", "both"):
            await embed_phase(pool, embedder)
        if PHASE in ("extract", "both"):
            await extract_phase(pool, embedder)
    finally:
        await pool.close()
    log("ALL DONE")


if __name__ == "__main__":
    asyncio.run(main())
