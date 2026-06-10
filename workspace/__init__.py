"""Workspace engine — projects as scoped cognitive containers.

Key APIs:
  POST/GET/PATCH/DELETE /workspaces
  CRUD /projects, /tasks, /goals, /timelines

Each workspace has its own memory scope, agent defaults, and knowledge surface.

See: ~/hitenos-architecture/06-workspace-engine.md
"""
from __future__ import annotations

import json
from typing import Any

from shared.types.ulid import mint

# Try importing events from shared; fallback if module is not yet created.
try:
    from shared import events  # noqa: F401
except Exception:
    events = None  # type: ignore[assignment]


# ── Workspaces ──────────────────────────────────────────────────────────────


async def create_workspace(
    pool: Any,
    publisher: Any,
    scope: str,
    name: str,
    kind: str = "project",
) -> dict:
    """Create a workspace. Returns {id, scope, name, kind}."""
    ws_id = mint("ws")
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO workspaces (id, scope, name, kind) VALUES (%s, %s, %s, %s)",
                (ws_id, scope, name, kind),
            )
    return {"id": ws_id, "scope": scope, "name": name, "kind": kind}


async def get_workspace(pool: Any, workspace_id: str) -> dict | None:
    """Fetch a single workspace by id."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, scope, name, kind, properties, created_at, updated_at "
                "FROM workspaces WHERE id = %s",
                (workspace_id,),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "scope": row[1],
        "name": row[2],
        "kind": row[3],
        "properties": row[4],
        "created_at": row[5],
        "updated_at": row[6],
    }


async def list_workspaces(pool: Any, scope: str) -> list[dict]:
    """List all workspaces for a given scope."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, scope, name, kind, created_at FROM workspaces WHERE scope = %s ORDER BY created_at DESC",
                (scope,),
            )
            rows = await cur.fetchall()
    return [
        {"id": r[0], "scope": r[1], "name": r[2], "kind": r[3], "created_at": r[4]}
        for r in rows
    ]


# ── Projects ────────────────────────────────────────────────────────────────


async def create_project(
    pool: Any,
    publisher: Any,
    workspace_id: str,
    name: str,
    status: str = "active",
) -> dict:
    """Create a project inside a workspace. Returns {id, workspace_id, name, status}."""
    prj_id = mint("prj")
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO projects (id, workspace_id, name, status) VALUES (%s, %s, %s, %s)",
                (prj_id, workspace_id, name, status),
            )
    return {"id": prj_id, "workspace_id": workspace_id, "name": name, "status": status}


# ── Tasks ───────────────────────────────────────────────────────────────────


async def create_task(
    pool: Any,
    publisher: Any,
    project_id: str,
    title: str,
    priority: str = "medium",
    depends_on: list[str] | None = None,
) -> dict:
    """Create a task inside a project. Returns {id, project_id, title, priority, status}."""
    tsk_id = mint("tsk")
    deps = json.dumps(depends_on if depends_on is not None else [])
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO tasks (id, project_id, title, priority, depends_on) VALUES (%s, %s, %s, %s, %s)",
                (tsk_id, project_id, title, priority, deps),
            )
    return {
        "id": tsk_id,
        "project_id": project_id,
        "title": title,
        "priority": priority,
        "status": "pending",
    }


async def update_task_status(
    pool: Any,
    publisher: Any,
    task_id: str,
    status: str,
) -> dict:
    """Update a task's status. Returns {id, status}."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE tasks SET status = %s WHERE id = %s",
                (status, task_id),
            )
    return {"id": task_id, "status": status}


async def get_project_tasks(pool: Any, project_id: str) -> list[dict]:
    """List all tasks for a given project."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, project_id, title, priority, status, depends_on, created_at, completed_at "
                "FROM tasks WHERE project_id = %s ORDER BY created_at ASC",
                (project_id,),
            )
            rows = await cur.fetchall()
    return [
        {
            "id": r[0],
            "project_id": r[1],
            "title": r[2],
            "priority": r[3],
            "status": r[4],
            "depends_on": r[5],
            "created_at": r[6],
            "completed_at": r[7],
        }
        for r in rows
    ]
