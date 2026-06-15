"""Hierarchy engine — workspace → project → goal placement + physical homes.

This is the spine that turns abstract goals into work that happens somewhere real.

  workspace (life-area)  ──<  project (north-star goal)  ──<  goal  ──<  plan/tasks

Each project gets a directory tree on disk:

  ~/curlyos/workspaces/<workspace-slug>/<project-slug>/
      studio/      ← agent deliverables land here (the "studio view")
      src/         ← working source / scratch
      .curly.json  ← project manifest (ids, north-star, created_at)

All write paths stay under $HOME and never touch credential dirs — the same
boundary the worker sandbox enforces (orchestration/sandbox.py).

Functions here are idempotent: ensure_workspace / ensure_project look up by
(scope, slug) and create-if-missing, so re-running placement is safe.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from shared.types.ulid import mint

# Physical root for all workspace trees. Under $HOME, sandbox-legal.
WORKSPACES_ROOT = Path.home() / "curlyos" / "workspaces"


# ── slug / path helpers ───────────────────────────────────────────────────────

def slugify(name: str, *, fallback: str = "untitled") -> str:
    """A filesystem- and url-safe slug: lowercase, hyphenated, ascii."""
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    return s[:48] or fallback


def project_dir(workspace_slug: str, project_slug: str) -> Path:
    return WORKSPACES_ROOT / workspace_slug / project_slug


def _ensure_project_tree(path: Path, manifest: dict) -> None:
    """Create studio/ + src/ + .curly.json for a project (idempotent)."""
    (path / "studio").mkdir(parents=True, exist_ok=True)
    (path / "src").mkdir(parents=True, exist_ok=True)
    man = path / ".curly.json"
    if not man.exists():
        man.write_text(json.dumps(manifest, indent=2, default=str), "utf-8")


# ── workspaces ────────────────────────────────────────────────────────────────

async def ensure_workspace(
    pool: Any, *, scope: str, name: str, slug: str | None = None,
    summary: str | None = None, kind: str = "life_area",
) -> dict:
    """Look up a workspace by (scope, slug) or create it. Returns the row dict."""
    slug = slug or slugify(name)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, scope, name, slug, path, summary, kind, status "
                "FROM workspaces WHERE scope=%s AND slug=%s", (scope, slug),
            )
            row = await cur.fetchone()
            if row:
                return _ws_row(row)
            ws_id = mint("ws")
            path = str(WORKSPACES_ROOT / slug)
            await cur.execute(
                "INSERT INTO workspaces (id, scope, name, slug, path, summary, kind, status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,'active')",
                (ws_id, scope, name, slug, path, summary, kind),
            )
    (WORKSPACES_ROOT / slug).mkdir(parents=True, exist_ok=True)
    return {"id": ws_id, "scope": scope, "name": name, "slug": slug,
            "path": path, "summary": summary, "kind": kind, "status": "active"}


def _ws_row(r) -> dict:
    return {"id": r[0], "scope": r[1], "name": r[2], "slug": r[3], "path": r[4],
            "summary": r[5], "kind": r[6], "status": r[7]}


# ── projects ──────────────────────────────────────────────────────────────────

async def ensure_project(
    pool: Any, *, scope: str, workspace_id: str, workspace_slug: str, name: str,
    slug: str | None = None, summary: str | None = None,
    north_star_goal_id: str | None = None,
) -> dict:
    """Look up a project by (workspace_id, slug) or create it + its dir tree."""
    slug = slug or slugify(name)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, workspace_id, scope, name, slug, path, summary, "
                "north_star_goal_id, status FROM projects "
                "WHERE workspace_id=%s AND slug=%s", (workspace_id, slug),
            )
            row = await cur.fetchone()
            if row:
                return _prj_row(row)
            prj_id = mint("prj")
            path = str(project_dir(workspace_slug, slug))
            await cur.execute(
                "INSERT INTO projects (id, workspace_id, scope, name, slug, path, "
                "summary, north_star_goal_id, status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'active')",
                (prj_id, workspace_id, scope, name, slug, path, summary, north_star_goal_id),
            )
    _ensure_project_tree(Path(path), {
        "project_id": prj_id, "workspace_id": workspace_id, "scope": scope,
        "name": name, "slug": slug, "north_star_goal_id": north_star_goal_id,
    })
    return {"id": prj_id, "workspace_id": workspace_id, "scope": scope, "name": name,
            "slug": slug, "path": path, "summary": summary,
            "north_star_goal_id": north_star_goal_id, "status": "active"}


def _prj_row(r) -> dict:
    return {"id": r[0], "workspace_id": r[1], "scope": r[2], "name": r[3], "slug": r[4],
            "path": r[5], "summary": r[6], "north_star_goal_id": r[7], "status": r[8]}


# ── life-area routing ─────────────────────────────────────────────────────────
# A goal is placed into a life-area workspace (not one undifferentiated bucket).
# Routing is keyword-based: deterministic, free, and predictable. The taxonomy is
# ordered — the first area whose keywords match wins — so more specific areas
# (career, content) are checked before broad ones (building). Falls back to
# "Personal & Ops". Phase 2 may later refine borderline cases with the LLM.

_LIFE_AREAS: list[tuple[str, str, tuple[str, ...]]] = [
    ("Career & Job Search", "career",
     ("apply", "job", "role", "resume", "cv", "interview", "recruiter",
      "hiring", "hire", "application", "linkedin", "salary", "offer")),
    ("Content & Brand", "content",
     ("case study", "case-study", "narrative", "article", "blog", "post",
      "portfolio", "write-up", "writeup", "content", "brand", "story",
      "storytelling", "essay", "newsletter")),
    ("Product & Engineering", "building",
     ("build", "ship", "app", "feature", "code", "deploy", "bug", "api",
      "system", "refactor", "migrate", "curlyos", "jobpilot", "agent",
      "webapp", "backend", "frontend", "database", "integration")),
    ("Learning & Research", "learning",
     ("learn", "study", "research", "course", "read", "explore", "understand",
      "investigate", "evaluate")),
    ("Health & Wellbeing", "health",
     ("health", "fitness", "sleep", "exercise", "workout", "diet", "nutrition",
      "meditat", "wellbeing", "wellness")),
    ("Finance", "finance",
     ("finance", "money", "budget", "invest", "expense", "cost", "revenue",
      "pricing", "income", "savings")),
]
_DEFAULT_AREA = ("Personal & Ops", "personal")


def _kw_match(haystack: str, keyword: str) -> bool:
    """Match a keyword on word boundaries (so 'job' doesn't hit 'jobpilot' and
    'post' doesn't hit 'postgres'). Multi-word phrases match as substrings."""
    if " " in keyword or "-" in keyword:
        return keyword in haystack
    return re.search(rf"\b{re.escape(keyword)}\b", haystack) is not None


def route_life_area(title: str, description: str | None = None) -> tuple[str, str]:
    """Return (workspace_name, slug) for a goal, by keyword match over the taxonomy."""
    hay = f"{title or ''} {description or ''}".lower()
    for name, slug, keywords in _LIFE_AREAS:
        if any(_kw_match(hay, k) for k in keywords):
            return name, slug
    return _DEFAULT_AREA


_AREA_SUMMARY = {
    "career": "Job search, applications, and positioning.",
    "content": "Case studies, writing, and brand narrative.",
    "building": "Products, code, and systems you're shipping.",
    "learning": "Research, study, and exploration.",
    "health": "Health, fitness, and wellbeing.",
    "finance": "Money, budgeting, and investing.",
    "personal": "Everything else — personal projects and ops.",
}


# ── goal placement ────────────────────────────────────────────────────────────

async def place_goal(pool: Any, *, scope: str, goal_id: str) -> dict | None:
    """Ensure a goal has a workspace + project + working dir, and return placement.

    Idempotent: if the goal already has project_id, returns the existing project's
    home. Otherwise derives a workspace (from the goal's horizon/parent) and a
    project (from the goal title), creates both + the dir tree, and links the goal.

    Returns {"workspace": {...}, "project": {...}, "studio_dir": str} or None if
    the goal doesn't exist.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT g.id, g.title, g.horizon, g.project_id, g.parent_id "
                "FROM goals g WHERE g.id=%s AND g.scope=%s", (goal_id, scope),
            )
            g = await cur.fetchone()
            if g is None:
                return None
            existing_project_id = g[3]
            if existing_project_id:
                await cur.execute(
                    "SELECT p.id, p.workspace_id, p.scope, p.name, p.slug, p.path, "
                    "p.summary, p.north_star_goal_id, p.status, w.name, w.slug, w.path "
                    "FROM projects p JOIN workspaces w ON w.id=p.workspace_id "
                    "WHERE p.id=%s", (existing_project_id,),
                )
                pr = await cur.fetchone()
                if pr:
                    project = _prj_row(pr[:9])
                    workspace = {"id": pr[1], "name": pr[9], "slug": pr[10], "path": pr[11]}
                    return {"workspace": workspace, "project": project,
                            "studio_dir": str(Path(project["path"]) / "studio")}

    title = g[1] or "Untitled goal"
    # Workspace = the goal's life area (career / content / building / …), routed
    # by keyword so related goals cluster into the same workspace.
    ws_name, ws_slug = route_life_area(title, g[1])
    workspace = await ensure_workspace(
        pool, scope=scope, name=ws_name, slug=ws_slug, kind="life_area",
        summary=_AREA_SUMMARY.get(ws_slug, "Goal-driven work."))
    project = await ensure_project(
        pool, scope=scope, workspace_id=workspace["id"],
        workspace_slug=workspace["slug"], name=title,
        summary=(g[1] or "")[:280], north_star_goal_id=goal_id)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE goals SET project_id=%s WHERE id=%s AND project_id IS NULL",
                (project["id"], goal_id),
            )
    return {"workspace": workspace, "project": project,
            "studio_dir": str(Path(project["path"]) / "studio")}


# ── artifacts ─────────────────────────────────────────────────────────────────

async def save_artifact(
    pool: Any, *, scope: str, title: str, kind: str = "file",
    project_id: str | None = None, goal_id: str | None = None,
    run_id: str | None = None, task_id: str | None = None,
    path: str | None = None, url: str | None = None,
    bytes_: int | None = None, summary: str | None = None,
    meta: dict | None = None,
) -> dict:
    """Record a deliverable. Upserts on (project_id, path) for file-backed kinds so
    re-writing the same file bumps it to 'updated' instead of duplicating."""
    if bytes_ is None and path:
        try:
            bytes_ = Path(path).stat().st_size
        except OSError:
            bytes_ = None
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            if path and project_id:
                await cur.execute(
                    "SELECT id FROM artifacts WHERE project_id=%s AND path=%s LIMIT 1",
                    (project_id, path),
                )
                hit = await cur.fetchone()
                if hit:
                    await cur.execute(
                        "UPDATE artifacts SET title=%s, kind=%s, bytes=%s, summary=%s, "
                        "run_id=%s, task_id=%s, status='updated', updated_at=now() "
                        "WHERE id=%s",
                        (title, kind, bytes_, summary, run_id, task_id, hit[0]),
                    )
                    return {"id": hit[0], "status": "updated", "path": path}
            art_id = mint("art")
            await cur.execute(
                "INSERT INTO artifacts (id, scope, project_id, goal_id, run_id, task_id, "
                "kind, title, path, url, bytes, summary, meta) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (art_id, scope, project_id, goal_id, run_id, task_id, kind, title,
                 path, url, bytes_, summary, json.dumps(meta or {})),
            )
    return {"id": art_id, "status": "created", "path": path, "url": url}


def _art_row(r) -> dict:
    return {"id": r[0], "scope": r[1], "project_id": r[2], "goal_id": r[3],
            "run_id": r[4], "task_id": r[5], "kind": r[6], "title": r[7],
            "path": r[8], "url": r[9], "bytes": r[10], "status": r[11],
            "summary": r[12], "created_at": r[13], "updated_at": r[14]}


_ART_COLS = ("id, scope, project_id, goal_id, run_id, task_id, kind, title, path, "
             "url, bytes, status, summary, created_at, updated_at")


async def list_artifacts(
    pool: Any, *, scope: str, project_id: str | None = None,
    goal_id: str | None = None, limit: int = 200,
) -> list[dict]:
    where = ["scope=%s"]
    params: list[Any] = [scope]
    if project_id:
        where.append("project_id=%s"); params.append(project_id)
    if goal_id:
        where.append("goal_id=%s"); params.append(goal_id)
    params.append(limit)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT {_ART_COLS} FROM artifacts WHERE {' AND '.join(where)} "
                f"ORDER BY created_at DESC LIMIT %s", params,
            )
            rows = await cur.fetchall()
    return [_art_row(r) for r in rows]


# ── read views ────────────────────────────────────────────────────────────────

async def list_workspaces_full(pool: Any, scope: str) -> list[dict]:
    """Workspaces with project counts, for the top-level view."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT w.id, w.scope, w.name, w.slug, w.path, w.summary, w.kind, w.status, "
                "(SELECT count(*) FROM projects p WHERE p.workspace_id=w.id) "
                "FROM workspaces w WHERE w.scope=%s AND w.status='active' "
                "ORDER BY w.created_at DESC", (scope,),
            )
            rows = await cur.fetchall()
    return [{**_ws_row(r), "project_count": r[8]} for r in rows]


async def get_workspace_detail(pool: Any, workspace_id: str) -> dict | None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, scope, name, slug, path, summary, kind, status "
                "FROM workspaces WHERE id=%s", (workspace_id,),
            )
            w = await cur.fetchone()
            if w is None:
                return None
            await cur.execute(
                "SELECT p.id, p.workspace_id, p.scope, p.name, p.slug, p.path, p.summary, "
                "p.north_star_goal_id, p.status, "
                "(SELECT count(*) FROM goals g WHERE g.project_id=p.id), "
                "(SELECT count(*) FROM artifacts a WHERE a.project_id=p.id) "
                "FROM projects p WHERE p.workspace_id=%s ORDER BY p.created_at DESC",
                (workspace_id,),
            )
            prows = await cur.fetchall()
    projects = [{**_prj_row(r[:9]), "goal_count": r[9], "artifact_count": r[10]}
                for r in prows]
    return {"workspace": _ws_row(w), "projects": projects}


async def get_project_detail(pool: Any, project_id: str) -> dict | None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT p.id, p.workspace_id, p.scope, p.name, p.slug, p.path, p.summary, "
                "p.north_star_goal_id, p.status, w.name, w.slug "
                "FROM projects p JOIN workspaces w ON w.id=p.workspace_id WHERE p.id=%s",
                (project_id,),
            )
            p = await cur.fetchone()
            if p is None:
                return None
            await cur.execute(
                "SELECT id, title, status, progress, horizon, success_criteria "
                "FROM goals WHERE project_id=%s AND valid_to IS NULL "
                "ORDER BY priority DESC, valid_from DESC", (project_id,),
            )
            grows = await cur.fetchall()
            scope = p[2]
    project = {**_prj_row(p[:9]), "workspace_name": p[9], "workspace_slug": p[10]}
    goals = [{"id": r[0], "title": r[1], "status": r[2], "progress": r[3],
              "horizon": r[4], "success_criteria": r[5]} for r in grows]
    artifacts = await list_artifacts(pool, scope=scope, project_id=project_id)
    return {"project": project, "goals": goals, "artifacts": artifacts}
