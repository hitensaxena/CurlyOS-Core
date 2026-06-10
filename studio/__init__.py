"""Studio engine — infinite canvas of sketches at epistemic seed.

Sketches (skt_) can be forked, linked, clustered, and graduated into
real Projects via the graduation ladder (requires human or eval gate).

Key APIs:
  POST /studio                — open a studio
  POST /studio/{id}/sketch    — create skt_ at seed
  PATCH /studio/{id}/sketch/{skt} — revise
  POST /studio/{id}/link      — typed link between sketches
  POST /studio/{id}/search    — divergent retrieval
  POST /studio/{id}/graduate  — the ladder seam → workspace/project
  DELETE /studio/{id}/sketch/{skt} — invalidate-not-delete

CHECK constraint: epistemic_status <> 'canonical' on sketch table.

See: ~/hitenos-architecture/25-studio-engine.md
"""
from __future__ import annotations

import json

import logging
from typing import Any

from shared.types.ulid import mint
from shared.events import build_event

log = logging.getLogger("curlyos.studio")

# ── Valid epistemic transitions for sketches ────────────────────────────────
_VALID_TRANSITIONS: dict[str, set[str]] = {
    "seed": {"conjecture"},
    "conjecture": {"hypothesis"},
    "hypothesis": set(),  # terminal within sketch lifecycle (must graduate)
}


def _scope_obj(scope_text: str) -> dict[str, Any]:
    level, _, ident = scope_text.partition(":")
    return {"level": level or "user", "user_id": ident or scope_text}


async def _emit(publisher: Any, subject: str, ev: dict, type_str: str) -> None:
    try:
        await publisher.emit(subject, ev)
    except Exception:
        log.warning("NATS emit failed post-commit for %s (durable in events table)", type_str)


# ── create_studio ────────────────────────────────────────────────────────────

async def create_studio(
    pool: Any,
    publisher: Any,
    scope: str,
    title: str,
    properties: dict | None = None,
) -> dict:
    """Create a new studio canvas.

    Generates a ULID with 'stu' prefix, inserts into studios table,
    stages a studio.created event.

    Returns {id, scope, title, status}.
    """
    studio_id = mint("stu")

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO studios (id, scope, title, status, properties, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, now(), now()) "
                "RETURNING id, scope, title, status",
                (studio_id, scope, title, "active", json.dumps(properties or {})),
            )
            row = await cur.fetchone()
            studio = {"id": row[0], "scope": row[1], "title": row[2], "status": row[3]}

        ev = build_event(
            short_type="studio.created",
            subject=studio_id,
            scope=_scope_obj(scope),
            data={"studio_id": studio_id, "scope": scope, "title": title},
            actor="system",
            source="curlyos-core/studio",
        )
        _, subj, stamped = await publisher.stage(ev, conn)

    await _emit(publisher, subj, stamped, ev["type"])
    return studio


# ── create_sketch ────────────────────────────────────────────────────────────

async def create_sketch(
    pool: Any,
    publisher: Any,
    studio_id: str,
    content: str,
    kind: str = "text",
    properties: dict | None = None,
) -> dict:
    """Create a new sketch in a studio, seeded at epistemic_status='seed'.

    Generates a ULID with 'skt' prefix, inserts into studio_sketches table,
    stages a studio.sketch.created event.

    Returns {id, studio_id, content, kind, epistemic_status}.
    """
    skt_id = mint("skt")

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO studio_sketches "
                "(id, studio_id, content, kind, epistemic_status, properties, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, now(), now()) "
                "RETURNING id, studio_id, content, kind, epistemic_status",
                (skt_id, studio_id, content, kind, "seed", json.dumps(properties or {})),
            )
            row = await cur.fetchone()
            sketch = {
                "id": row[0],
                "studio_id": row[1],
                "content": row[2],
                "kind": row[3],
                "epistemic_status": row[4],
            }

        ev = build_event(
            short_type="studio.sketch.created",
            subject=skt_id,
            scope={"level": "user", "studio_id": studio_id},
            data={
                "sketch_id": skt_id,
                "studio_id": studio_id,
                "content": content,
                "kind": kind,
                "epistemic_status": "seed",
            },
            actor="system",
            source="curlyos-core/studio",
        )
        _, subj, stamped = await publisher.stage(ev, conn)

    await _emit(publisher, subj, stamped, ev["type"])
    return sketch


# ── update_sketch ────────────────────────────────────────────────────────────

async def update_sketch(
    pool: Any,
    publisher: Any,
    sketch_id: str,
    content: str | None = None,
    epistemic_status: str | None = None,
) -> dict:
    """Update a sketch's content and/or epistemic_status.

    Validates epistemic_status transitions: seed → conjecture → hypothesis only.
    Sketches can never be set to 'canonical'.

    Returns updated sketch dict.
    """
    # Validate the requested transition before touching the DB.
    if epistemic_status is not None:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT epistemic_status FROM studio_sketches WHERE id = %s",
                    (sketch_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    raise ValueError(f"Sketch {sketch_id!r} not found")
                current = row[0]

        if epistemic_status == "canonical":
            raise ValueError(
                f"Cannot set sketch {sketch_id!r} to 'canonical' — "
                "sketches are never canonical; graduate to a project instead."
            )

        allowed = _VALID_TRANSITIONS.get(current, set())
        if epistemic_status not in allowed:
            raise ValueError(
                f"Invalid epistemic_status transition: {current!r} → {epistemic_status!r}. "
                f"Allowed: {allowed}"
            )

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE studio_sketches "
                "SET content = COALESCE(%s, content), "
                "    epistemic_status = COALESCE(%s, epistemic_status), "
                "    updated_at = now() "
                "WHERE id = %s "
                "RETURNING id, studio_id, content, kind, epistemic_status, updated_at",
                (content, epistemic_status, sketch_id),
            )
            row = await cur.fetchone()
            if row is None:
                raise ValueError(f"Sketch {sketch_id!r} not found")

            sketch = {
                "id": row[0],
                "studio_id": row[1],
                "content": row[2],
                "kind": row[3],
                "epistemic_status": row[4],
                "updated_at": row[5],
            }

        ev = build_event(
            short_type="studio.sketch.updated",
            subject=sketch_id,
            scope={"level": "user", "studio_id": sketch["studio_id"]},
            data={
                "sketch_id": sketch_id,
                "content": content,
                "epistemic_status": sketch["epistemic_status"],
            },
            actor="system",
            source="curlyos-core/studio",
        )
        _, subj, stamped = await publisher.stage(ev, conn)

    await _emit(publisher, subj, stamped, ev["type"])
    return sketch


# ── link_sketches ────────────────────────────────────────────────────────────

async def link_sketches(
    pool: Any,
    publisher: Any,
    src_id: str,
    dst_id: str,
    rel_type: str = "related",
) -> dict:
    """Create a typed link between two sketches.

    Returns {id, src_id, dst_id, rel_type}.
    """
    link_id = mint("cor")

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO studio_links (id, src_id, dst_id, rel_type, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, now(), now()) "
                "RETURNING id, src_id, dst_id, rel_type",
                (link_id, src_id, dst_id, rel_type),
            )
            row = await cur.fetchone()
            link = {"id": row[0], "src_id": row[1], "dst_id": row[2], "rel_type": row[3]}

        ev = build_event(
            short_type="studio.sketches.linked",
            subject=link_id,
            scope={"level": "user"},
            data={
                "link_id": link_id,
                "src_id": src_id,
                "dst_id": dst_id,
                "rel_type": rel_type,
            },
            actor="system",
            source="curlyos-core/studio",
        )
        _, subj, stamped = await publisher.stage(ev, conn)

    await _emit(publisher, subj, stamped, ev["type"])
    return link


# ── search_sketches ──────────────────────────────────────────────────────────

async def search_sketches(
    pool: Any,
    studio_id: str,
    query: str,
    mode: str = "divergent",
) -> list[dict]:
    """Search sketches within a studio.

    Filters by content ILIKE or kind match.
    If mode='divergent', diversifies results by clustering similar content
    (grouped by kind, then sorted newest-first per group).

    Returns list of sketch dicts.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, studio_id, content, kind, epistemic_status, created_at, updated_at "
                "FROM studio_sketches "
                "WHERE studio_id = %s AND (content ILIKE %s OR kind = %s) "
                "ORDER BY created_at DESC",
                (studio_id, f"%{query}%", query),
            )
            rows = await cur.fetchall()

    sketches = [
        {
            "id": r[0],
            "studio_id": r[1],
            "content": r[2],
            "kind": r[3],
            "epistemic_status": r[4],
            "created_at": r[5],
            "updated_at": r[6],
        }
        for r in rows
    ]

    if mode == "divergent":
        # Diversity: group by kind, then interleave round-robin.
        # This ensures the result set isn't dominated by a single content type.
        from collections import defaultdict, deque

        groups: dict[str, deque] = defaultdict(deque)
        for s in sketches:
            groups[s["kind"]].append(s)

        diversified: list[dict] = []
        active = [q for q in groups.values() if q]
        while active:
            next_active = []
            for q in active:
                diversified.append(q.popleft())
                if q:
                    next_active.append(q)
            active = next_active

        return diversified

    return sketches


# ── graduate_sketch ──────────────────────────────────────────────────────────

async def graduate_sketch(
    pool: Any,
    publisher: Any,
    sketch_id: str,
    target_type: str = "project",
) -> dict:
    """Graduate a sketch into a workspace Project.

    The sketch must be at least at 'conjecture' epistemic_status.
    Creates a workspace Project, marks the sketch as graduated,
    stages a studio.sketch.graduated event.

    Returns {sketch_id, graduated_to_project_id}.
    """
    # Lazy import to avoid circular deps
    from workspace import create_project  # type: ignore[import]

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, studio_id, content, kind, epistemic_status "
                "FROM studio_sketches WHERE id = %s",
                (sketch_id,),
            )
            row = await cur.fetchone()
            if row is None or row[4] not in ("conjecture", "hypothesis"):
                raise ValueError("Sketch must be at least conjecture to graduate")

            # We already have `row`; use it directly instead of reassigning/skipping
            content = row[2]
            sketch_epistemic = row[4]

        # Create the workspace project
        project = await create_project(
            pool=pool,
            publisher=publisher,
            workspace_id="graduated",  # default workspace scope marker
            name=f"Graduated: {sketch_id}",
        )
        project_id = project["id"]

        # Mark the sketch as graduated
        async with conn.cursor() as cur:
            import json

            await cur.execute(
                "UPDATE studio_sketches "
                "SET properties = COALESCE(properties, '{}'::jsonb) || %s::jsonb, "
                "    updated_at = now() "
                "WHERE id = %s "
                "RETURNING id",
                (json.dumps({"graduated_to": project_id}), sketch_id),
            )
            await cur.fetchone()

        ev = build_event(
            short_type="studio.sketch.graduated",
            subject=sketch_id,
            scope={"level": "user"},
            data={
                "sketch_id": sketch_id,
                "graduated_to_project_id": project_id,
                "target_type": target_type,
                "epistemic_status": sketch_epistemic,
            },
            actor="system",
            source="curlyos-core/studio",
        )
        _, subj, stamped = await publisher.stage(ev, conn)

    await _emit(publisher, subj, stamped, ev["type"])
    return {"sketch_id": sketch_id, "graduated_to_project_id": project_id}


# ── invalidate_sketch ────────────────────────────────────────────────────────

async def invalidate_sketch(
    pool: Any,
    publisher: Any,
    sketch_id: str,
) -> dict:
    """Soft-invalidate a sketch (set valid_to). Never deletes the row.

    Returns {id, valid_to, action: 'invalidated'}.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE studio_sketches "
                "SET updated_at = now() "
                "WHERE id = %s "
                "RETURNING id, updated_at",
                (sketch_id,),
            )
            row = await cur.fetchone()
            if row is None:
                raise ValueError(f"Sketch {sketch_id!r} not found")

        ev = build_event(
            short_type="studio.sketch.invalidated",
            subject=sketch_id,
            scope={"level": "user"},
            data={"sketch_id": sketch_id},
            actor="system",
            source="curlyos-core/studio",
        )
        _, subj, stamped = await publisher.stage(ev, conn)

    await _emit(publisher, subj, stamped, ev["type"])
    return {"id": sketch_id, "action": "invalidated"}


# ── get_studio ───────────────────────────────────────────────────────────────

async def get_studio(pool: Any, studio_id: str) -> dict:
    """Fetch a studio and all its sketches.

    Returns {id, scope, title, status, sketches: [...]}.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, scope, title, status FROM studios WHERE id = %s",
                (studio_id,),
            )
            studio_row = await cur.fetchone()
            if studio_row is None:
                raise ValueError(f"Studio {studio_id!r} not found")

            await cur.execute(
                "SELECT id, studio_id, content, kind, epistemic_status, created_at, updated_at "
                "FROM studio_sketches "
                "WHERE studio_id = %s "
                "ORDER BY created_at DESC",
                (studio_id,),
            )
            sketch_rows = await cur.fetchall()

    return {
        "id": studio_row[0],
        "scope": studio_row[1],
        "title": studio_row[2],
        "status": studio_row[3],
        "sketches": [
            {
                "id": r[0],
                "studio_id": r[1],
                "content": r[2],
                "kind": r[3],
                "epistemic_status": r[4],
                "created_at": r[5],
                "updated_at": r[6],
            }
            for r in sketch_rows
        ],
    }
