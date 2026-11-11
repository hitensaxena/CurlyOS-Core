#!/usr/bin/env python3
"""Import the ChatGPT export's documents into CurlyOS (phase 5).

Phase 4 repaired the conversation transcripts. This handles the files that were
uploaded *into* those conversations: the export ships them as opaque
`file-XXXX.dat` blobs, named through conversation_asset_file_names.json.

What it does per document:
  1. Pulls the blob out of the export zip into --files-dir under its real name.
  2. Extracts text (PDF via pypdf; .md/.txt read directly) and writes a
     markdown mirror to ~/mind/archives/chatgpt/files/YYYY-MM/.
  3. Writes one episode per ≤--max-chars chunk with the REAL document text,
     embeds it, extracts into the knowledge graph.
  4. Writes one distilled summary memory for the whole document.
  5. Records everything in the 0019 ledger (migrations/0019_chatgpt_files.sql)
     so re-runs skip finished files and resume partial ones.

Documents listed in library_files.json with no blob in the export get a ledger
row with status='missing-binary' — the gap stays visible, nothing is invented.

PDF text needs pypdf, which is not a CurlyOS dependency. Point CURLYOS_PDF_PYTHON
(or --pdf-python) at any interpreter that has it; default:
    uv venv ~/.local/share/curlyos-import-tools
    uv pip install --python ~/.local/share/curlyos-import-tools/bin/python pypdf

Usage (from the repo root):
    set -a; . ./.env; set +a
    .venv/bin/python -I deploy/import_chatgpt_files.py \
        --export-dir ~/Downloads/chatgpt-extract-20261110 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import import_chatgpt_export as base  # noqa: E402  (load_env, dsn, make_llm, slugify)

REPO = Path(__file__).resolve().parent.parent
SCOPE_DEFAULT = "user:usr_hiten"
SOURCE_TAG = "chatgpt-file"
MAX_CHARS = 6000
DOC_EXTS = {".pdf", ".md", ".txt"}
PDF_PYTHON_DEFAULT = "~/.local/share/curlyos-import-tools/bin/python"

log = logging.getLogger("chatgpt-files")


# ── text ────────────────────────────────────────────────────────────────────
_PUA_RE = re.compile(r"[-]")
_BLANK_RUN_RE = re.compile(r"\n{3,}")
# Postgres text columns reject NUL outright, and pypdf emits stray control
# bytes from some PDFs' font programs.
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def doc_text(text: str) -> str:
    """Normalise extracted text. Deliberately NOT base.clean_text: that one
    strips chat citation tokens like `turn0product0`, which can be legitimate
    words inside a document."""
    if not text:
        return ""
    text = text.replace(" ", " ").replace(" ", " ").replace("\r\n", "\n")
    text = _PUA_RE.sub("", text)
    text = _CTRL_RE.sub("", text)
    text = _BLANK_RUN_RE.sub("\n\n", text)
    return text.strip()


def pdf_text(path: Path, pdf_python: str) -> tuple[str, int]:
    """(text, page_count) via pypdf in a separate interpreter."""
    code = (
        "import json,sys,pypdf,logging\n"
        "logging.disable(logging.WARNING)\n"
        "r=pypdf.PdfReader(sys.argv[1])\n"
        "if r.is_encrypted:\n"                       # owner-password PDFs: user pw is empty
        "    try: r.decrypt('')\n"
        "    except Exception: pass\n"
        "pages=[]\n"
        "for p in r.pages:\n"
        "    try: pages.append(p.extract_text() or '')\n"
        "    except Exception: pages.append('')\n"
        "json.dump({'pages':len(pages),'text':'\\n\\n'.join(pages)},sys.stdout)\n"
    )
    out = subprocess.run(
        [pdf_python, "-I", "-c", code, str(path)],
        capture_output=True, text=True, timeout=600,
    )
    if out.returncode != 0:
        raise RuntimeError(f"pypdf failed on {path.name}: {out.stderr.strip()[:400]}")
    d = json.loads(out.stdout)
    return d["text"], int(d["pages"])


def read_doc(path: Path, pdf_python: str) -> tuple[str, int]:
    if path.suffix.lower() == ".pdf":
        return pdf_text(path, pdf_python)
    return path.read_text(encoding="utf-8", errors="replace"), 0


# ── discovery ───────────────────────────────────────────────────────────────
def load_maps(export_dir: Path) -> tuple[dict[str, str], list[dict]]:
    """(dat_member → real filename, library_files rows)."""
    mapped: dict[str, str] = {}
    assets = export_dir / "conversation_asset_file_names.json"
    if assets.exists():
        mapped = json.loads(assets.read_text())
    lib: list[dict] = []
    lp = export_dir / "library_files.json"
    if lp.exists():
        lib = json.loads(lp.read_text())
    return mapped, lib


def discover(export_dir: Path) -> list[dict]:
    mapped, lib = load_maps(export_dir)
    docs: list[dict] = []
    seen_ids: set[str] = set()

    for member, name in mapped.items():
        if Path(name).suffix.lower() not in DOC_EXTS:
            continue
        fid = member.rsplit(".", 1)[0]
        seen_ids.add(fid)
        row = next((f for f in lib if f.get("file_name") == name), {})
        docs.append({
            "file_id": fid, "member": member, "file_name": name,
            "ext": Path(name).suffix.lower().lstrip("."),
            "created_at": row.get("created_at"), "has_blob": True,
        })

    # library rows whose name never appears in the asset map: no blob shipped.
    mapped_names = {d["file_name"] for d in docs}
    for f in lib:
        ext = "." + (f.get("file_extension") or "").lower()
        if ext not in DOC_EXTS or f.get("file_name") in mapped_names:
            continue
        fid = (f.get("file_id") or f"lib_{len(docs)}").replace("/", "_")
        if fid in seen_ids:
            continue
        seen_ids.add(fid)
        docs.append({
            "file_id": fid, "member": None, "file_name": f.get("file_name"),
            "ext": ext.lstrip("."), "created_at": f.get("created_at"),
            "has_blob": False,
        })
    return docs


def mirror_path(mirror_dir: Path, doc: dict, taken: dict[str, str]) -> Path:
    created = doc.get("created_at")
    try:
        d = datetime.fromisoformat(created.replace("Z", "+00:00")) if created else None
    except ValueError:
        d = None
    month = d.strftime("%Y-%m") if d else "undated"
    slug = base.slugify(doc["file_name"] or "", limit=80)
    name = f"{slug}.md"
    # Three uploads are all called "December 2024.pdf"; disambiguate by id.
    if name in taken and taken[name] != doc["file_id"]:
        name = f"{slug}_{doc['file_id'][-8:]}.md"
    taken[name] = doc["file_id"]
    return mirror_dir / month / name


def extract_blob(zip_path: Path, doc: dict, files_dir: Path,
                 taken: dict[str, str] | None = None) -> Path:
    """Write the blob under its real filename; suffix the id on a name clash.

    Different uploads share filenames ("December 2024.pdf" three times), so a
    bare name would silently hand the second document the first one's bytes.
    """
    files_dir.mkdir(parents=True, exist_ok=True)
    name = doc["file_name"]
    if taken is not None:
        if name in taken and taken[name] != doc["file_id"]:
            p = Path(name)
            name = f"{p.stem}_{doc['file_id'][-8:]}{p.suffix}"
        taken[name] = doc["file_id"]
    target = files_dir / name
    if target.exists() and target.stat().st_size > 0:
        return target
    with zipfile.ZipFile(zip_path) as zf:
        data = zf.read(doc["member"])
    target.write_bytes(data)
    return target


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ── chunking / mirror ───────────────────────────────────────────────────────
def chunk_text(doc: dict, text: str, max_chars: int) -> list[str]:
    created = doc.get("created_at")
    day = created[:10] if created else "undated"
    header = f"ChatGPT document: {doc['file_name']} (uploaded {day})\n\n"
    chunks: list[str] = []
    buf: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal buf, size
        if buf:
            chunks.append(header + "\n\n".join(buf))
            buf, size = [], 0

    for para in re.split(r"\n{2,}", text):
        para = para.strip()
        if not para:
            continue
        room = max_chars - len(header)
        if len(para) > room:
            flush()
            while len(para) > room:
                chunks.append(header + para[:room])
                para = para[room:]
            if para:
                buf.append(para)
                size += len(para)
            continue
        if size + len(para) + 2 > room and buf:
            flush()
        buf.append(para)
        size += len(para) + 2
    flush()
    return chunks


def render_markdown(doc: dict, text: str, sha: str, pages: int) -> str:
    title = (doc["file_name"] or "Untitled").replace('"', "'")
    created = doc.get("created_at") or "unknown"
    return (
        "---\n"
        f'title: "{title}"\n'
        f'file_id: "{doc["file_id"]}"\n'
        f'ext: "{doc["ext"]}"\n'
        f"uploaded: {created}\n"
        f'sha256: "{sha}"\n'
        f"pages: {pages}\n"
        f"chars: {len(text)}\n"
        f"source: {SOURCE_TAG}\n"
        "---\n\n"
        f"# {doc['file_name']}\n\n{text}\n"
    )


# ── llm ─────────────────────────────────────────────────────────────────────
SUMMARY_PROMPT = """You maintain a long-term memory about one person from the documents they uploaded.

Write 3-8 bullet lines of durable knowledge from this document: what it is, what they
decided or proposed in it, named people/companies/products, numbers, dates, and any
constraint or preference it states.

Rules:
- Third person, tense as stated, one fact per bullet.
- Concrete beats abstract: name the product, model, amount, date.
- Never invent, never add advice, never describe the file format.
- Reply NO_DURABLE_CONTENT only if the document is a blank form or pure boilerplate.

Document:
{text}
"""


async def summarize_doc(llm, model: str, text: str) -> str:
    resp = await llm.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": SUMMARY_PROMPT.replace("{text}", text)}],
        temperature=0.2,
        max_tokens=1200,
    )
    return (resp.choices[0].message.content or "").strip()


# ── main ────────────────────────────────────────────────────────────────────
async def run(args: argparse.Namespace) -> dict:
    import psycopg
    from psycopg_pool import AsyncConnectionPool

    base.load_env()
    export_dir = Path(args.export_dir).expanduser().resolve()
    zip_path = Path(args.zip).expanduser().resolve()
    files_dir = Path(args.files_dir).expanduser().resolve()
    mirror_dir = Path(args.mirror_dir).expanduser().resolve()
    pdf_python = os.path.expanduser(
        args.pdf_python or os.environ.get("CURLYOS_PDF_PYTHON") or PDF_PYTHON_DEFAULT
    )
    scope = args.scope
    if not zip_path.exists():
        raise SystemExit(f"export zip not found: {zip_path}")

    docs = discover(export_dir)
    have_blob = [d for d in docs if d["has_blob"]]
    missing = [d for d in docs if not d["has_blob"]]
    print()
    print(f"documents in export      : {len(docs)}")
    print(f"  blob available         : {len(have_blob)}")
    print(f"  name only (no binary)  : {len(missing)}")
    if have_blob and not Path(pdf_python).exists() and any(d["ext"] == "pdf" for d in have_blob):
        raise SystemExit(
            f"PDF text needs pypdf at {pdf_python}\n"
            f"  uv venv {Path(PDF_PYTHON_DEFAULT).expanduser().parent}\n"
            f"  uv pip install --python {Path(PDF_PYTHON_DEFAULT).expanduser().parent}/bin/python pypdf"
        )

    sync = psycopg.connect(base.dsn())
    done = {r[0] for r in sync.execute(
        "SELECT file_id FROM chatgpt_files WHERE status = 'ingested'").fetchall()}
    work = [d for d in have_blob if d["file_id"] not in done]
    if args.limit:
        work = work[: args.limit]

    print(f"  already ingested       : {len(done & {d['file_id'] for d in have_blob})}")
    print(f"TO IMPORT                : {len(work)}")
    print(f"LLM calls                : {len(work)} summaries + 1 extraction per chunk")
    print()

    if args.dry_run:
        for d in work[:20]:
            print(f"  {d['ext']:<4} {d['file_name']}")
        print("\n[dry-run] nothing written")
        sync.close()
        return {"dry_run": True}

    run_id = sync.execute(
        "INSERT INTO chatgpt_import_runs (export_dir) VALUES (%s) RETURNING id",
        (f"{export_dir} [documents]",),
    ).fetchone()[0]
    for d in missing:
        sync.execute(
            """
            INSERT INTO chatgpt_files (file_id, file_name, ext, status, note)
            VALUES (%s,%s,%s,'missing-binary','listed in library_files.json; no blob in export')
            ON CONFLICT (file_id) DO NOTHING
            """,
            (d["file_id"], d["file_name"], d["ext"]),
        )
    sync.commit()

    pool = AsyncConnectionPool(base.dsn(), min_size=1, max_size=4, open=False)
    await pool.open()
    from shared.events.implementations import PgOnlyPublisher
    from memory.governance import add, record_episode
    from knowledge.graph import extract_and_project
    from shared.embeddings.implementations import LocalBgeM3, CachingEmbedder

    publisher = PgOnlyPublisher()
    embedder = CachingEmbedder(LocalBgeM3())
    await embedder.embed(["warmup"])
    llm = base.make_llm()
    model = os.environ.get("CURLYOS_LLM_MODEL", "claude-haiku-5-5")
    chain = [m.strip() for m in os.environ.get("CURLYOS_MODEL_CHAIN", "").split(",") if m.strip()]

    stats = {"files": 0, "duplicates": 0, "missing_binary": len(missing),
             "episodes": 0, "summaries": 0, "entities": 0, "edges": 0,
             "triples": 0, "errors": 0}
    taken: dict[str, str] = {}
    sem = asyncio.Semaphore(max(1, args.concurrency))
    t0 = time.time()

    # The export ships the same bytes many times over — 67 blobs hold 46 distinct
    # documents ("October 2024.pdf" four times). Hash everything up front so a
    # duplicate is skipped before it writes a second copy of the same episode.
    sha_seen: dict[str, str] = {
        r[0]: r[1] for r in sync.execute(
            "SELECT sha256, file_id FROM chatgpt_files WHERE status='ingested' AND sha256 IS NOT NULL"
        ).fetchall()
    }
    blob_of: dict[str, Path] = {}
    canonical: list[dict] = []
    for doc in work:
        try:
            blob = extract_blob(zip_path, doc, files_dir, taken)
            sha = sha256_of(blob)
            blob_of[doc["file_id"]] = blob
            first = sha_seen.get(sha)
            if first:
                sync.execute(
                    "INSERT INTO chatgpt_files (file_id,file_name,ext,sha256,bytes,status,note,imported_at) "
                    "VALUES (%s,%s,%s,%s,%s,'duplicate',%s,now()) "
                    "ON CONFLICT (file_id) DO UPDATE SET status='duplicate', sha256=EXCLUDED.sha256, "
                    "note=EXCLUDED.note, imported_at=now()",
                    (doc["file_id"], doc["file_name"], doc["ext"], sha, blob.stat().st_size,
                     f"same bytes as {first}"),
                )
                stats["duplicates"] += 1
                continue
            sha_seen[sha] = doc["file_id"]
            doc["sha256"] = sha
            canonical.append(doc)
        except Exception:  # noqa: BLE001
            stats["errors"] += 1
            log.exception("blob extraction failed %s", doc["file_id"])
    sync.commit()
    log.info("%d blob(s) → %d distinct document(s), %d duplicate(s)",
             len(work), len(canonical), stats["duplicates"])

    for n, doc in enumerate(canonical, 1):
        fid = doc["file_id"]
        try:
            blob = blob_of[fid]
            sha = doc["sha256"]
            raw, pages = await asyncio.to_thread(read_doc, blob, pdf_python)
            text = doc_text(raw)
            if not text:
                sync.execute(
                    "INSERT INTO chatgpt_files (file_id,file_name,ext,sha256,bytes,status,note,imported_at) "
                    "VALUES (%s,%s,%s,%s,%s,'unsupported','no extractable text',now()) "
                    "ON CONFLICT (file_id) DO UPDATE SET status='unsupported', note='no extractable text'",
                    (fid, doc["file_name"], doc["ext"], sha, blob.stat().st_size),
                )
                sync.commit()
                stats["errors"] += 1
                continue

            md_file = mirror_path(mirror_dir, doc, taken)
            md_file.parent.mkdir(parents=True, exist_ok=True)
            md_file.write_text(render_markdown(doc, text, sha, pages), encoding="utf-8")

            chunks = chunk_text(doc, text, args.max_chars)
            sync.execute(
                """
                INSERT INTO chatgpt_files
                  (file_id,file_name,ext,sha256,bytes,chars,page_count,md_path,chunk_count,status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending')
                ON CONFLICT (file_id) DO UPDATE SET
                  sha256=EXCLUDED.sha256, chars=EXCLUDED.chars, page_count=EXCLUDED.page_count,
                  md_path=EXCLUDED.md_path, chunk_count=EXCLUDED.chunk_count
                """,
                (fid, doc["file_name"], doc["ext"], sha, blob.stat().st_size,
                 len(text), pages, str(md_file), len(chunks)),
            )
            sync.commit()

            first_epi = None
            ent = edg = tri = 0
            for i, chunk in enumerate(chunks):
                async with sem:
                    epi = await record_episode(
                        pool, publisher, scope, content=chunk,
                        source_ref=f"{SOURCE_TAG}:{fid}:{i}",
                    )
                epi_id = epi["epi_id"]
                first_epi = first_epi or epi_id
                stats["episodes"] += 1
                try:
                    vec = (await embedder.embed([chunk]))[0]
                    literal = "[" + ",".join(repr(float(x)) for x in vec) + "]"
                    async with pool.connection() as conn:
                        async with conn.cursor() as cur:
                            await cur.execute(
                                "UPDATE episodes SET embedding = %s::vector WHERE id = %s",
                                (literal, epi_id),
                            )
                except Exception:  # noqa: BLE001
                    log.warning("embed failed for episode %s", epi_id)
                res = {}
                try:
                    res = await extract_and_project(pool, publisher, scope, epi_id, chunk,
                                                    embedder, llm)
                    ent += int(res.get("entities_created", 0))
                    edg += int(res.get("edges_created", 0))
                    tri += int(res.get("triples_extracted", 0))
                except Exception as exc:  # noqa: BLE001
                    log.warning("extract failed %s chunk %d: %s", fid, i, exc)
                async with pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            """
                            INSERT INTO chatgpt_file_chunks (file_id,chunk_idx,epi_id,chars,extracted,triples,embedded)
                            VALUES (%s,%s,%s,%s,true,%s,true)
                            ON CONFLICT (file_id,chunk_idx) DO UPDATE SET
                              epi_id=EXCLUDED.epi_id, extracted=true,
                              triples=EXCLUDED.triples, embedded=true
                            """,
                            (fid, i, epi_id, len(chunk), int(res.get("triples_extracted", 0))),
                        )

            summary, mem_id = "", None
            if llm is not None:
                view = "\n\n".join(chunks)[: args.summary_chars]
                for cand in [model] + chain:
                    try:
                        summary = await summarize_doc(llm, cand, view)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("summary failed %s (%s): %s", fid, cand, exc)
                        summary = ""
                    if summary:
                        break
            if summary and "NO_DURABLE_CONTENT" not in summary:
                mem = await add(
                    pool, publisher, scope,
                    statement=f"ChatGPT document \"{doc['file_name']}\" "
                              f"({(doc.get('created_at') or 'undated')[:10]}):\n{summary}",
                    source_episode_id=first_epi, kind="fact", epistemic_status="canonical",
                )
                mem_id = mem.get("mem_id")
                stats["summaries"] += 1
                try:
                    vec = (await embedder.embed([summary]))[0]
                    literal = "[" + ",".join(repr(float(x)) for x in vec) + "]"
                    async with pool.connection() as conn:
                        async with conn.cursor() as cur:
                            await cur.execute(
                                "UPDATE memories SET embedding = %s::vector WHERE id = %s",
                                (literal, mem_id),
                            )
                except Exception:  # noqa: BLE001
                    pass

            sync.execute(
                """
                UPDATE chatgpt_files SET status='ingested', epi_id=%s, summary_mem_id=%s,
                  extracted=true, imported_at=now(), note=%s WHERE file_id=%s
                """,
                (first_epi, mem_id, f"{len(chunks)} chunk(s)", fid),
            )
            sync.commit()
            stats["files"] += 1
            stats["entities"] += ent
            stats["edges"] += edg
            stats["triples"] += tri
            log.info("[%d/%d] %s | %s chars, %s chunk(s), +%dE +%dR (%.0fs)",
                     n, len(canonical), (doc["file_name"] or "")[:48], len(text),
                     len(chunks), ent, edg, time.time() - t0)
        except Exception:  # noqa: BLE001
            stats["errors"] += 1
            log.exception("document import failed %s", fid)
            sync.execute(
                "INSERT INTO chatgpt_files (file_id,file_name,ext,status,note) "
                "VALUES (%s,%s,%s,'failed','see log') ON CONFLICT (file_id) DO UPDATE SET status='failed'",
                (fid, doc.get("file_name"), doc.get("ext")),
            )
            sync.commit()

    sync.execute("UPDATE chatgpt_import_runs SET finished_at=now(), stats=%s WHERE id=%s",
                 (json.dumps(stats), run_id))
    sync.commit()
    sync.close()
    await pool.close()
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(os.environ.get("CURLYOS_REDIS_URL", "redis://127.0.0.1:6379"))
        await r.incr(f"recall:gen:{scope}")
        await r.aclose()
    except Exception:  # noqa: BLE001
        log.warning("could not bump recall cache generation")

    print("\n" + "=" * 60)
    for k, v in stats.items():
        print(f"  {k:<16} {v}")
    print(f"  elapsed         {time.time() - t0:.0f}s")
    print("=" * 60)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description="Import ChatGPT export documents")
    ap.add_argument("--export-dir", required=True)
    ap.add_argument("--zip", default="~/Downloads/Chatgpt-backup.zip")
    ap.add_argument("--files-dir", default="~/Downloads/chatgpt-files-20261110")
    ap.add_argument("--mirror-dir", default="~/mind/archives/chatgpt/files")
    ap.add_argument("--scope", default=SCOPE_DEFAULT)
    ap.add_argument("--max-chars", type=int, default=MAX_CHARS)
    ap.add_argument("--summary-chars", type=int, default=40000)
    ap.add_argument("--pdf-python", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
