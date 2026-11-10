#!/usr/bin/env python3
"""Import a ChatGPT data export into CurlyOS (episodes → graph → summaries).

Phases 1–3 of the ChatGPT merge plan:
  1. Ledger rows (migrations/0018_chatgpt_import.sql) so runs are idempotent
     and resumable.
  2. Markdown mirror to ~/mind/archives/chatgpt/YYYY-MM/ (same shape as the
     2026-06 import) + chunking of net-new conversations.
  3. One episode per chunk with the REAL conversation text, embedded and
     LLM-extracted into the knowledge graph, plus one distilled summary
     memory per conversation.

Why not reuse import_mind.py: it wrote episodes whose body was
"Knowledge import from <file>: N facts", so extraction had nothing to read
(1,137 conversations produced ~1.2k entities). This importer puts actual
dialogue in the episode.

Usage (from the repo root, env sourced):
    set -a; . ./.env; set +a
    .venv/bin/python -I deploy/import_chatgpt_export.py \
        --export-dir ~/Downloads/chatgpt-extract-20261110 --dry-run

    # then, for real:
    .venv/bin/python -I deploy/import_chatgpt_export.py \
        --export-dir ~/Downloads/chatgpt-extract-20261110

Re-running is safe: finished conversations are skipped, partial ones resume
at the first chunk row that isn't marked extracted.
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = Path(__file__).resolve().parent.parent
SCOPE_DEFAULT = "user:usr_hiten"
SOURCE_TAG = "chatgpt-export"
MAX_CHARS = 6000

# Injected-profile / system scaffolding that appeared in older exports. This
# export is clean, but stripping is cheap insurance against re-export drift.
_SCAFFOLD_RE = re.compile(
    r"_\[custom instructions[^\]]*\]_.*?(?=(\n\n|$))", re.S | re.I
)

# ChatGPT inlines its rich-UI citations into the message text, e.g.
#   product[["turn0product0","AILKIN …",{…}]]
# 4.7% of messages carry them; left in, they are pure noise for extraction and
# for anyone reading the markdown mirror.
_CITATION_RE = re.compile(r"product\s*\[\s*\[.*?\]\s*\]", re.S)
_REF_TOKEN_RE = re.compile(r'"?turn\d+[a-z]*\d+"?')
_PRIVATE_USE_RE = re.compile(r"[\ue000-\uf8ff]")
_BLANK_RUN_RE = re.compile(r"\n{3,}")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("chatgpt-import")


# ── env ─────────────────────────────────────────────────────────────────────
def load_env() -> None:
    """Load .env into os.environ without overriding what's already set."""
    for candidate in (Path.cwd() / ".env", REPO / ".env"):
        if not candidate.exists():
            continue
        for raw in candidate.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip("'").strip('"')
            os.environ.setdefault(k, v)
        return


def dsn() -> str:
    v = os.environ.get("CURLYOS_DATABASE_URL")
    if not v:
        raise SystemExit("CURLYOS_DATABASE_URL is not set (source .env first)")
    return v


# ── export parsing ──────────────────────────────────────────────────────────
def _text_of(parts: Any, asset_names: dict[str, str]) -> str:
    """Flatten message parts to text, naming binary attachments in place."""
    out: list[str] = []
    for p in parts or []:
        if isinstance(p, str):
            out.append(p)
            continue
        if not isinstance(p, dict):
            continue
        if p.get("text"):
            out.append(p["text"])
        pointer = p.get("asset_pointer") or ""
        if pointer:
            fid = pointer.rsplit("/", 1)[-1]
            name = asset_names.get(fid + ".dat") or asset_names.get(fid) or fid[:14]
            kind = (p.get("content_type") or "asset").replace("_asset_pointer", "")
            out.append(f"_[{kind}: {name}]_")
    return clean_text("\n".join(x for x in out if x and x.strip()))


def clean_text(text: str) -> str:
    """Drop ChatGPT rich-UI citation artifacts and private-use glyphs."""
    if not text:
        return ""
    text = _CITATION_RE.sub("", text)
    text = _REF_TOKEN_RE.sub("", text)
    text = _PRIVATE_USE_RE.sub("", text)
    return _BLANK_RUN_RE.sub("\n\n", text).strip()


def _ts(epoch: float | None) -> datetime | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def iter_conversations(export_dir: Path) -> Iterable[dict]:
    shards = sorted(glob.glob(str(export_dir / "conversations-*.json")))
    if not shards:
        raise SystemExit(f"no conversations-*.json in {export_dir}")
    for path in shards:
        with open(path, "r", encoding="utf-8") as f:
            for conv in json.load(f):
                yield conv


def load_asset_names(export_dir: Path) -> dict[str, str]:
    names: dict[str, str] = {}
    lib = export_dir / "library_files.json"
    if lib.exists():
        for f in json.loads(lib.read_text()):
            fid, name = f.get("file_id"), f.get("file_name")
            if fid and name:
                names[fid + ".dat"] = name
    assets = export_dir / "conversation_asset_file_names.json"
    if assets.exists():
        names.update(json.loads(assets.read_text()))
    return names


def conversation_turns(conv: dict, asset_names: dict[str, str]) -> list[tuple[str, str]]:
    """Ordered (role, text) turns, current branch only, CoT stripped."""
    mapping = conv.get("mapping") or {}
    node_id = conv.get("current_node")
    chain: list[dict] = []
    seen: set[str] = set()
    while node_id and node_id in mapping and node_id not in seen:
        seen.add(node_id)
        node = mapping[node_id]
        chain.append(node)
        node_id = node.get("parent")
    chain.reverse()

    turns: list[tuple[str, str]] = []
    for node in chain:
        msg = node.get("message")
        if not msg:
            continue
        role = (msg.get("author") or {}).get("role")
        if role not in ("user", "assistant"):
            continue
        content = msg.get("content") or {}
        if content.get("content_type") not in ("text", "multimodal_text"):
            continue
        text = _text_of(content.get("parts"), asset_names)
        text = _SCAFFOLD_RE.sub("", text).strip()
        if not text:
            continue
        turns.append((role, text))
    return turns


def frontmatter(conv: dict, turns: list[tuple[str, str]]) -> str:
    created = _ts(conv.get("create_time"))
    updated = _ts(conv.get("update_time"))
    title = (conv.get("title") or "Untitled").replace('"', "'")
    return (
        "---\n"
        f'title: "{title}"\n'
        f'conversation_id: "{conv["conversation_id"]}"\n'
        f"date: {created.date().isoformat() if created else 'unknown'}\n"
        f"created: {created.strftime('%Y-%m-%dT%H:%M:%S.%f') if created else 'unknown'}\n"
        f"updated: {updated.strftime('%Y-%m-%dT%H:%M:%S.%f') if updated else 'unknown'}\n"
        f"message_count: {len(turns)}\n"
        f'model: "{conv.get("default_model_slug") or "unknown"}"\n'
        f"source: {SOURCE_TAG}\n"
        "---\n"
    )


def render_markdown(conv: dict, turns: list[tuple[str, str]]) -> str:
    title = conv.get("title") or "Untitled"
    body = [frontmatter(conv, turns), f"# {title}"]
    for role, text in turns:
        body.append(f"### {role}\n\n{text}")
    return "\n\n".join(body) + "\n"


def slugify(text: str, limit: int = 70) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "untitled").lower()).strip("-")
    return (s[:limit].rstrip("-")) or "untitled"


def mirror_path(mirror_dir: Path, conv: dict) -> Path:
    created = _ts(conv.get("create_time")) or datetime.now(tz=timezone.utc)
    month = created.strftime("%Y-%m")
    day = created.strftime("%Y-%m-%d")
    return mirror_dir / month / f"{day}_{slugify(conv.get('title') or '')}.md"


def chunk_turns(conv: dict, turns: list[tuple[str, str]], max_chars: int) -> list[str]:
    """Pack turns into chunks ≤ max_chars, never splitting a turn mid-sentence.

    Each chunk carries a one-line context header so extraction knows which
    conversation it came from.
    """
    created = _ts(conv.get("create_time"))
    header = (
        f"ChatGPT conversation: {conv.get('title') or 'Untitled'}"
        f" ({created.date().isoformat() if created else 'undated'})\n\n"
    )
    chunks: list[str] = []
    buf: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal buf, size
        if buf:
            chunks.append(header + "\n\n".join(buf))
            buf, size = [], 0

    for role, text in turns:
        segment = f"### {role}\n\n{text}"
        if len(segment) > max_chars:
            # A single huge turn: split on paragraph boundaries.
            for para in re.split(r"\n{2,}", text):
                piece = f"### {role}\n\n{para}"
                if size + len(piece) > max_chars and buf:
                    flush()
                while len(piece) > max_chars:
                    if buf:
                        flush()
                    chunks.append(header + piece[:max_chars])
                    piece = f"### {role} (cont.)\n\n" + piece[max_chars:]
                buf.append(piece)
                size += len(piece)
            continue
        if size + len(segment) > max_chars and buf:
            flush()
        buf.append(segment)
        size += len(segment)
    flush()
    return chunks


# ── llm ─────────────────────────────────────────────────────────────────────
def make_llm():
    from openai import AsyncOpenAI

    base = os.environ.get("CURLYOS_LLM_BASE_URL")
    key = os.environ.get("CURLYOS_LLM_API_KEY") or "local"
    if not base:
        return None
    return AsyncOpenAI(base_url=base, api_key=key, timeout=180.0, max_retries=1)


SUMMARY_PROMPT = """You maintain a long-term memory about one person from their chat history.

Write 3-8 bullet lines of durable knowledge from this conversation: what they decided,
what they are building, preferences they stated, constraints, named people, numbers,
and questions they left open.

Rules:
- Third person, tense as stated, one fact per bullet.
- Concrete beats abstract: name the product, model, amount, date.
- Never invent, never add advice, never restate the question.
- Reply NO_DURABLE_CONTENT only if every message is a greeting, an error, or a
  one-word reply. Any named product, plan, tool, person, amount, or date is durable.

Conversation:
{text}
"""


async def summarize(llm, model: str, text: str) -> str:
    # 1200 tokens, not 400: the gateway's models bill reasoning tokens against
    # max_tokens, and 400 came back finish_reason=length with empty content.
    resp = await llm.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": SUMMARY_PROMPT.replace("{text}", text)}],
        temperature=0.2,
        max_tokens=1200,
    )
    return (resp.choices[0].message.content or "").strip()


# ── db ──────────────────────────────────────────────────────────────────────
def prior_conversation_ids(conn) -> set[str]:
    """Conversation ids already in the graph from the 2026-06 import."""
    rows = conn.execute(
        "SELECT DISTINCT statement FROM memories "
        "WHERE statement LIKE '# archives/chatgpt/%'"
    ).fetchall()
    ids: set[str] = set()
    rx = re.compile(r'conversation_id: "?([0-9a-f-]+)"?')
    for (statement,) in rows:
        m = rx.search(statement or "")
        if m:
            ids.add(m.group(1))
    return ids


def already_imported(conn) -> set[str]:
    rows = conn.execute(
        "SELECT conversation_id FROM chatgpt_conversations WHERE status IN ('ingested','skipped')"
    ).fetchall()
    return {r[0] for r in rows}


def summaryless_ingested(conn) -> set[str]:
    """Ingested conversations whose summary memory never landed."""
    rows = conn.execute(
        "SELECT conversation_id FROM chatgpt_conversations "
        "WHERE status = 'ingested' AND summary_mem_id IS NULL"
    ).fetchall()
    return {r[0] for r in rows}


def upsert_conversation(conn, conv: dict, md_path: Path, export_dir: Path,
                        chars: int, chunks: int, status: str, note: str | None = None) -> None:
    created = _ts(conv.get("create_time"))
    updated = _ts(conv.get("update_time"))
    conn.execute(
        """
        INSERT INTO chatgpt_conversations
          (conversation_id, title, created_at, updated_at, message_count, model,
           char_count, chunk_count, md_path, export_dir, status, note)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (conversation_id) DO UPDATE SET
          title = EXCLUDED.title,
          char_count = EXCLUDED.char_count,
          chunk_count = EXCLUDED.chunk_count,
          md_path = EXCLUDED.md_path,
          export_dir = EXCLUDED.export_dir,
          status = EXCLUDED.status,
          note = COALESCE(EXCLUDED.note, chatgpt_conversations.note)
        """,
        (conv["conversation_id"], conv.get("title"), created, updated,
         int(conv.get("message_count") or 0), conv.get("default_model_slug"),
         chars, chunks, str(md_path), str(export_dir), status, note),
    )


def done_chunk_indexes(conn, conv_id: str) -> set[int]:
    rows = conn.execute(
        "SELECT chunk_idx FROM chatgpt_chunks WHERE conversation_id = %s AND extracted = true",
        (conv_id,),
    ).fetchall()
    return {r[0] for r in rows}


# ── main ────────────────────────────────────────────────────────────────────
async def run(args: argparse.Namespace) -> dict:
    import psycopg
    from psycopg_pool import AsyncConnectionPool

    load_env()
    export_dir = Path(args.export_dir).expanduser().resolve()
    mirror_dir = Path(args.mirror_dir).expanduser().resolve()
    scope = args.scope or os.environ.get("CURLYOS_SCOPE") or SCOPE_DEFAULT
    max_chars = args.max_chars

    asset_names = load_asset_names(export_dir)
    log.info("export: %s (%d known asset names)", export_dir, len(asset_names))

    # ── discovery + diff (read-only) ────────────────────────────────────────
    sync_conn = psycopg.connect(dsn())
    prior = prior_conversation_ids(sync_conn)
    done = already_imported(sync_conn)
    resummarize = summaryless_ingested(sync_conn) if args.resummarize else set()
    log.info("ledger: %d conversation(s) already ingested/skipped", len(done))
    log.info("prior import (2026-06 markdown archives): %d conversation id(s)", len(prior))

    plan: list[dict] = []
    skipped = 0
    for conv in iter_conversations(export_dir):
        cid = conv["conversation_id"]
        turns = conversation_turns(conv, asset_names)
        if not turns:
            skipped += 1
            continue
        chars = sum(len(t) for _, t in turns)
        chunks = chunk_turns(conv, turns, max_chars)
        plan.append({
            "conv": conv,
            "conv_id": cid,
            "turns": turns,
            "chars": chars,
            "chunks": chunks,
            "skip_reason": (
                None if cid in resummarize          # summary pass: chunks already done
                else "ledger" if cid in done
                else "prior-import" if cid in prior
                else None
            ),
        })

    new = [p for p in plan if not p["skip_reason"]]
    known = [p for p in plan if p["skip_reason"]]
    total_chunks = sum(len(p["chunks"]) for p in new)
    total_chars = sum(p["chars"] for p in new)

    print()
    print(f"conversations in export : {len(plan)}")
    print(f"  already in ledger     : {sum(1 for p in known if p['skip_reason'] == 'ledger')}")
    print(f"  from 2026-06 import   : {sum(1 for p in known if p['skip_reason'] == 'prior-import')}")
    print(f"  empty / no text turns : {skipped}")
    print(f"NET-NEW conversations   : {len(new)}")
    print(f"NET-NEW chars           : {total_chars:,} (~{total_chars // 4:,} tokens)")
    print(f"NET-NEW chunks @{max_chars}    : {total_chunks}")
    print(f"LLM calls               : {total_chunks} extraction + {len(new)} summaries")
    print()

    if args.dry_run:
        for p in sorted(new, key=lambda x: -x["chars"])[:15]:
            conv = p["conv"]
            created = _ts(conv.get("create_time"))
            print(f"  {p['chars']:>7,} chars  {len(p['chunks']):>3} chunks  "
                  f"{created.date() if created else '????-??-??'}  {(conv.get('title') or '')[:58]}")
        print("\n[dry-run] nothing written")
        sync_conn.close()
        return {"dry_run": True}

    # ── write ───────────────────────────────────────────────────────────────
    mirror_dir.mkdir(parents=True, exist_ok=True)
    run_id = sync_conn.execute(
        "INSERT INTO chatgpt_import_runs (export_dir) VALUES (%s) RETURNING id",
        (str(export_dir),),
    ).fetchone()[0]
    sync_conn.commit()
    for p in known:
        if p["skip_reason"] == "prior-import":
            upsert_conversation(sync_conn, p["conv"], mirror_path(mirror_dir, p["conv"]),
                                export_dir, p["chars"], len(p["chunks"]), "skipped",
                                "ingested by 2026-06 markdown import")
    sync_conn.commit()
    del done, prior

    pool = AsyncConnectionPool(dsn(), min_size=1, max_size=4, open=False)
    await pool.open()
    from shared.events.implementations import PgOnlyPublisher
    from memory.governance import add, record_episode
    from knowledge.graph import extract_and_project
    from shared.embeddings.implementations import LocalBgeM3, CachingEmbedder
    from knowledge.resolution import normalize_mention

    publisher = PgOnlyPublisher()
    embedder = CachingEmbedder(LocalBgeM3())
    await embedder.embed(["warmup"])
    llm = make_llm()
    model = os.environ.get("CURLYOS_LLM_MODEL", "claude-haiku-5-5")
    chain = [m.strip() for m in os.environ.get("CURLYOS_MODEL_CHAIN", "").split(",") if m.strip()]

    stats = {"conversations": 0, "episodes": 0, "chunks": 0, "summaries": 0,
             "entities": 0, "edges": 0, "triples": 0, "errors": 0, "skipped_chunks": 0}
    selected = new[: args.limit] if args.limit else new
    t0 = time.time()

    sem = asyncio.Semaphore(max(1, args.concurrency))

    async def import_conversation(p: dict, idx: int) -> None:
        conv, cid = p["conv"], p["conv_id"]
        md_file = mirror_path(mirror_dir, conv)
        try:
            md_file.parent.mkdir(parents=True, exist_ok=True)
            md_file.write_text(render_markdown(conv, p["turns"]), encoding="utf-8")
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO chatgpt_conversations
                          (conversation_id, title, created_at, updated_at, message_count, model,
                           char_count, chunk_count, md_path, export_dir, status)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending')
                        ON CONFLICT (conversation_id) DO UPDATE SET
                          md_path = EXCLUDED.md_path, chunk_count = EXCLUDED.chunk_count,
                          char_count = EXCLUDED.char_count
                        """,
                        (cid, conv.get("title"), _ts(conv.get("create_time")),
                         _ts(conv.get("update_time")), len(p["turns"]),
                         conv.get("default_model_slug"), p["chars"], len(p["chunks"]),
                         str(md_file), str(export_dir)),
                    )
        except Exception as exc:  # noqa: BLE001
            log.exception("mirror/ledger failed for %s", cid)
            stats["errors"] += 1
            return

        done_idx = await asyncio.to_thread(lambda: _done_idx_sync(cid))
        conv_entities = conv_edges = 0
        first_epi: str | None = None

        for i, chunk in enumerate(p["chunks"]):
            stats["chunks"] += 1
            if i in done_idx:
                stats["skipped_chunks"] += 1
                continue
            try:
                async with sem:
                    epi = await record_episode(
                        pool, publisher, scope, content=chunk,
                        source_ref=f"{SOURCE_TAG}:{cid}:{i}",
                    )
                epi_id = epi["epi_id"]
                first_epi = first_epi or epi_id
                stats["episodes"] += 1

                embedded = False
                try:
                    vec = (await embedder.embed([chunk]))[0]
                    literal = "[" + ",".join(repr(float(x)) for x in vec) + "]"
                    async with pool.connection() as conn:
                        async with conn.cursor() as cur:
                            await cur.execute(
                                "UPDATE episodes SET embedding = %s::vector WHERE id = %s",
                                (literal, epi_id),
                            )
                    embedded = True
                except Exception:  # noqa: BLE001
                    log.warning("embed failed epi=%s", epi_id)

                triples = entities = edges = 0
                extracted = False
                err = None
                if not args.no_extract and llm is not None:
                    for attempt, candidate in enumerate([model] + chain):
                        try:
                            os.environ["CURLYOS_LLM_MODEL"] = candidate
                            res = await extract_and_project(
                                pool, publisher, scope, epi_id, chunk,
                                embedder=embedder, llm_client=llm,
                            )
                            triples = int(res.get("triples_extracted", 0))
                            entities = int(res.get("entities_created", 0))
                            edges = int(res.get("edges_created", 0))
                            extracted = True
                            break
                        except Exception as exc:  # noqa: BLE001
                            err = f"{candidate}: {exc}"[:400]
                            log.warning("extraction attempt %d failed epi=%s (%s)",
                                        attempt + 1, epi_id, err)
                    os.environ["CURLYOS_LLM_MODEL"] = model
                else:
                    # --no-extract: chunk stored + embedded, extraction left for
                    # a later normal run (conversation stays 'partial').
                    extracted = False

                conv_entities += entities
                conv_edges += edges
                stats["triples"] += triples
                stats["entities"] += entities
                stats["edges"] += edges

                async with pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            INSERT INTO chatgpt_chunks
                              (conversation_id, chunk_idx, epi_id, chars, extracted,
                               triples, entities_created, edges_created, embedded, error)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (conversation_id, chunk_idx) DO UPDATE SET
                              epi_id = EXCLUDED.epi_id, extracted = EXCLUDED.extracted,
                              triples = EXCLUDED.triples,
                              entities_created = EXCLUDED.entities_created,
                              edges_created = EXCLUDED.edges_created,
                              embedded = EXCLUDED.embedded, error = EXCLUDED.error
                            """,
                            (cid, i, epi_id, len(chunk), extracted, triples,
                             entities, edges, embedded, err),
                        )
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += 1
                log.exception("chunk %s#%d failed", cid, i)
                async with pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "INSERT INTO chatgpt_chunks (conversation_id, chunk_idx, error) "
                            "VALUES (%s,%s,%s) ON CONFLICT (conversation_id, chunk_idx) "
                            "DO UPDATE SET error = EXCLUDED.error",
                            (cid, i, str(exc)[:400]),
                        )

        # One distilled memory per conversation (not one per chunk).
        mem_id = None
        if first_epi and llm is not None and not args.no_summary:
            try:
                view = "\n\n".join(p["chunks"])[: args.summary_chars]
                text = ""
                for candidate in [model] + chain:
                    try:
                        text = await summarize(llm, candidate, view)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("summary model %s failed for %s: %s", candidate, cid, exc)
                        text = ""
                    if text:
                        break
                if text and "NO_DURABLE_CONTENT" not in text:
                    mem = await add(
                        pool, publisher, scope,
                        statement=f"ChatGPT conversation \"{conv.get('title') or 'Untitled'}\" "
                                  f"({(_ts(conv.get('create_time')) or datetime.now(tz=timezone.utc)).date()}):\n{text}",
                        source_episode_id=first_epi, kind="fact",
                        epistemic_status="canonical",
                    )
                    mem_id = mem.get("mem_id")
                    stats["summaries"] += 1
                    try:
                        vec = (await embedder.embed([text]))[0]
                        literal = "[" + ",".join(repr(float(x)) for x in vec) + "]"
                        async with pool.connection() as conn:
                            async with conn.cursor() as cur:
                                await cur.execute(
                                    "UPDATE memories SET embedding = %s::vector WHERE id = %s",
                                    (literal, mem_id),
                                )
                    except Exception:  # noqa: BLE001
                        pass
            except Exception:  # noqa: BLE001
                log.exception("summary failed for %s", cid)

        remaining = await asyncio.to_thread(lambda: _open_chunks_sync(cid))
        status = "ingested" if remaining == 0 else "partial"
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE chatgpt_conversations SET status=%s, summary_mem_id=%s, "
                    "imported_at=now() WHERE conversation_id=%s",
                    (status, mem_id, cid),
                )
        stats["conversations"] += 1
        elapsed = time.time() - t0
        log.info("[%d/%d] %s — %d chunks, +%d entities, +%d edges (%s) %.0fs",
                 idx + 1, len(selected), cid[:8], len(p["chunks"]),
                 conv_entities, conv_edges, status, elapsed)

    def _done_idx_sync(cid: str) -> set[int]:
        with psycopg.connect(dsn()) as c:
            return done_chunk_indexes(c, cid)

    def _open_chunks_sync(cid: str) -> int:
        with psycopg.connect(dsn()) as c:
            return c.execute(
                "SELECT count(*) FROM chatgpt_chunks "
                "WHERE conversation_id=%s AND extracted=false", (cid,),
            ).fetchone()[0]

    try:
        for i, p in enumerate(selected):
            await import_conversation(p, i)
    finally:
        await pool.close()

    # Recall is cached per scope in Redis by generation counter; bulk writes
    # bypass the API, so advance it or stale recall survives the import.
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(os.environ.get("CURLYOS_REDIS_URL", "redis://127.0.0.1:6379"))
        await r.incr(f"recall:gen:{scope}")
        await r.aclose()
    except Exception:  # noqa: BLE001
        log.warning("could not bump recall cache generation")

    sync_conn.execute(
        "UPDATE chatgpt_import_runs SET finished_at=now(), stats=%s WHERE id=%s",
        (json.dumps(stats), run_id),
    )
    sync_conn.commit()
    sync_conn.close()

    print("\n" + "=" * 60)
    for k, v in stats.items():
        print(f"  {k:<14} {v}")
    print(f"  elapsed        {time.time() - t0:.0f}s")
    print("=" * 60)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--export-dir", required=True,
                    help="directory holding conversations-*.json (extracted export)")
    ap.add_argument("--mirror-dir", default="~/mind/archives/chatgpt",
                    help="where to write per-conversation markdown")
    ap.add_argument("--scope", default=None)
    ap.add_argument("--max-chars", type=int, default=MAX_CHARS)
    ap.add_argument("--summary-chars", type=int, default=40000,
                    help="how much of a conversation the summary prompt sees")
    ap.add_argument("--limit", type=int, default=0, help="only N conversations (testing)")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-extract", action="store_true", help="episodes only, skip graph extraction")
    ap.add_argument("--no-summary", action="store_true")
    ap.add_argument("--resummarize", action="store_true",
                    help="re-visit ingested conversations that never got a summary memory")
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
