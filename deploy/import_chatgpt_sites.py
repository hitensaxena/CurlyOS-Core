#!/usr/bin/env python3
"""Import the ChatGPT export's Sites projects into CurlyOS (phase 6).

The export carries four ChatGPT Sites under sites/projects/project-NNNN/. They
are metadata only — project name, description, live URL, access mode, deployed
commit SHA, version — with no source code. So each becomes:

  * a brief at ~/mind/projects/chatgpt-sites/<slug>.md
  * one episode with that brief as its body (source_ref chatgpt-site:<slug>)
  * one memory holding the same facts (already distilled — no LLM summary)
  * knowledge-graph entities/edges from the brief

Idempotent: a project whose episode already exists is skipped unless --refresh,
in which case the existing episode body and memory are rewritten in place.

Usage (from the repo root):
    set -a; . ./.env; set +a
    .venv/bin/python -I deploy/import_chatgpt_sites.py \
        --export-dir ~/Downloads/chatgpt-extract-20261110 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import import_chatgpt_export as base  # noqa: E402  (load_env, dsn, make_llm, slugify)

SCOPE_DEFAULT = "user:usr_hiten"
log = logging.getLogger("chatgpt-sites")


def _meta(path: Path) -> dict:
    try:
        return (json.loads(path.read_text()).get("metadata") or {})
    except Exception:  # noqa: BLE001
        return {}


def discover(export_dir: Path) -> list[dict]:
    projects = []
    for proj in sorted(glob.glob(str(export_dir / "sites/projects/project-*"))):
        p = Path(proj)
        m = _meta(p / "project.json")
        if not m.get("slug"):
            continue
        dep = _meta(p / "deployments/app-deployment-0001.json")
        ver = _meta(p / "versions/app-version-0001.json")
        acc = _meta(p / "access_policies/access-policy-0001.json")
        ws = _meta(p / "workspaces/app-workspace-0001.json")
        projects.append({
            "project_dir": p.name,
            "slug": m["slug"],
            "title": m.get("title") or m["slug"],
            "description": m.get("description") or "",
            "status": m.get("status") or "unknown",
            "url": m.get("current_live_url") or dep.get("url") or "",
            "access": acc.get("access_mode") or "unknown",
            "deploy_status": dep.get("status") or "unknown",
            "commit": ver.get("source_commit_sha") or "",
            "version": ver.get("version_number") or "",
            "workspace": ws.get("slug") or "",
        })
    return projects


def brief(pr: dict) -> str:
    lines = [
        f"ChatGPT Site: {pr['title']}",
        f"Live URL: {pr['url']}",
        f"Slug: {pr['slug']} · workspace: {pr['workspace'] or 'unknown'} · access: {pr['access']}",
        f"Status: {pr['status']} · last deployment: {pr['deploy_status']}"
        + (f" · version {pr['version']}" if pr["version"] else ""),
    ]
    if pr["description"]:
        lines.append(f"What it is: {pr['description']}")
    if pr["commit"]:
        lines.append(f"Deployed commit: {pr['commit'][:12]}")
    lines.append(
        "Facts: this is one of Hiten's four ChatGPT Sites projects published under "
        "the hitensaxena workspace; the export contains site metadata only, not the "
        "site source."
    )
    return "\n".join(lines)


def memory_statement(pr: dict) -> str:
    return (
        f"ChatGPT Site \"{pr['title']}\" ({pr['slug']}) is live at {pr['url']}; "
        f"{pr['description'].rstrip('.')}. Access mode {pr['access']}, "
        f"deployment {pr['deploy_status']}"
        + (f", version {pr['version']}, commit {pr['commit'][:12]}." if pr["version"] else ".")
    )


def mirror_path(mirror_dir: Path, pr: dict) -> Path:
    return mirror_dir / f"{base.slugify(pr['slug'], limit=80)}.md"


def render(pr: dict) -> str:
    body = brief(pr)
    return (
        "---\n"
        f'title: "{pr["title"].replace(chr(34), chr(39))}"\n'
        f'slug: "{pr["slug"]}"\n'
        f'url: "{pr["url"]}"\n'
        f'access: "{pr["access"]}"\n'
        f'deployment: "{pr["deploy_status"]}"\n'
        f'commit: "{pr["commit"]}"\n'
        "source: chatgpt-site\n"
        "---\n\n"
        f"# {pr['title']}\n\n{body}\n"
    )


async def run(args: argparse.Namespace) -> dict:
    import psycopg
    from psycopg_pool import AsyncConnectionPool

    base.load_env()
    export_dir = Path(args.export_dir).expanduser().resolve()
    mirror_dir = Path(args.mirror_dir).expanduser().resolve()
    projects = discover(export_dir)
    if not projects:
        raise SystemExit(f"no sites found under {export_dir}/sites/projects")

    sync = psycopg.connect(base.dsn())
    existing = {
        r[0]: r[1] for r in sync.execute(
            "SELECT source_ref, id FROM episodes WHERE source_ref LIKE 'chatgpt-site:%'"
        ).fetchall()
    }
    print()
    for pr in projects:
        ref = f"chatgpt-site:{pr['slug']}"
        print(f"  {'skip ' if ref in existing and not args.refresh else 'import'}"
              f"  {pr['title'][:44]:<46} {pr['url']}")
    print(f"\n  {len(projects)} project(s), {len(existing)} already in the graph")
    if args.dry_run:
        print("\n[dry-run] nothing written")
        sync.close()
        return {"dry_run": True}

    pool = AsyncConnectionPool(base.dsn(), min_size=1, max_size=4, open=False)
    await pool.open()
    from shared.events.implementations import PgOnlyPublisher
    from memory.governance import add, record_episode, statement_key
    from knowledge.graph import extract_and_project
    from shared.embeddings.implementations import LocalBgeM3, CachingEmbedder

    publisher = PgOnlyPublisher()
    embedder = CachingEmbedder(LocalBgeM3())
    await embedder.embed(["warmup"])
    llm = base.make_llm()
    stats = {"projects": 0, "skipped": 0, "episodes": 0, "memories": 0,
             "entities": 0, "edges": 0, "errors": 0}
    t0 = time.time()

    for pr in projects:
        ref = f"chatgpt-site:{pr['slug']}"
        if ref in existing and not args.refresh:
            stats["skipped"] += 1
            continue
        try:
            text = brief(pr)
            md = mirror_path(mirror_dir, pr)
            md.parent.mkdir(parents=True, exist_ok=True)
            md.write_text(render(pr), encoding="utf-8")

            if ref in existing:
                epi_id = existing[ref]
                async with pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute("UPDATE episodes SET content = %s WHERE id = %s",
                                          (text, epi_id))
                stats["episodes"] += 1
            else:
                epi = await record_episode(pool, publisher, args.scope, content=text,
                                           source_ref=ref)
                epi_id = epi["epi_id"]
                stats["episodes"] += 1

            try:
                vec = (await embedder.embed([text]))[0]
                literal = "[" + ",".join(repr(float(x)) for x in vec) + "]"
                async with pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "UPDATE episodes SET embedding = %s::vector WHERE id = %s",
                            (literal, epi_id))
            except Exception:  # noqa: BLE001
                log.warning("embed failed for %s", ref)

            statement = memory_statement(pr)
            if ref in existing:
                async with pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "UPDATE memories SET statement = %s, statement_key = %s "
                            "WHERE source_episode_id = %s AND statement LIKE 'ChatGPT Site %'",
                            (statement, statement_key(statement), epi_id))
                        stats["memories"] += cur.rowcount
            else:
                mem = await add(pool, publisher, args.scope, statement=statement,
                                source_episode_id=epi_id, kind="fact",
                                epistemic_status="canonical")
                stats["memories"] += 1
                try:
                    vec = (await embedder.embed([statement]))[0]
                    literal = "[" + ",".join(repr(float(x)) for x in vec) + "]"
                    async with pool.connection() as conn:
                        async with conn.cursor() as cur:
                            await cur.execute(
                                "UPDATE memories SET embedding = %s::vector WHERE id = %s",
                                (literal, mem["mem_id"]))
                except Exception:  # noqa: BLE001
                    pass

            try:
                res = await extract_and_project(pool, publisher, args.scope, epi_id,
                                                text, embedder, llm)
                stats["entities"] += int(res.get("entities_created", 0))
                stats["edges"] += int(res.get("edges_created", 0))
            except Exception as exc:  # noqa: BLE001
                log.warning("extract failed %s: %s", ref, exc)
            stats["projects"] += 1
            log.info("[%s] %s", pr["slug"], md)
        except Exception:  # noqa: BLE001
            stats["errors"] += 1
            log.exception("site import failed %s", ref)

    sync.close()
    await pool.close()
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(os.environ.get("CURLYOS_REDIS_URL", "redis://127.0.0.1:6379"))
        await r.incr(f"recall:gen:{args.scope}")
        await r.aclose()
    except Exception:  # noqa: BLE001
        log.warning("could not bump recall cache generation")

    print("\n" + "=" * 60)
    for k, v in stats.items():
        print(f"  {k:<12} {v}")
    print(f"  elapsed      {time.time() - t0:.1f}s")
    print("=" * 60)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description="Import ChatGPT Sites metadata")
    ap.add_argument("--export-dir", required=True)
    ap.add_argument("--mirror-dir", default="~/mind/projects/chatgpt-sites")
    ap.add_argument("--scope", default=SCOPE_DEFAULT)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
