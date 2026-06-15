"""The Executive's tool registry — typed, action-classed, thin (curlyos-final/05 §3).

Every tool: (a) declares its PDP action class — the gate decides per call;
(b) wraps an EXISTING core function — no domain logic lives here (nodes and
tools are adapters, P7); (c) returns a JSON-serializable dict the observation
row stores verbatim.

Phase-A set: read + memory_write + external_post only. file_edit/code_exec/
net_egress arrive in later phases with their own floors and sandboxes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

log = logging.getLogger("curlyos-core.orchestration.tools")


@dataclass
class ToolDeps:
    """Infra bundle the runner assembles once per run."""
    pool: Any
    publisher: Any
    redis: Any
    notifier: Any
    scope: str
    run_id: str
    embedder_factory: Callable[[], Awaitable[Any]]


@dataclass(frozen=True)
class Tool:
    name: str
    action_class: str
    description: str          # one line, planner-facing
    params: str               # compact signature, planner-facing
    fn: Callable[[ToolDeps, dict], Awaitable[dict]]
    egress_host: str | None = None   # for net_egress tools: the allowed host (PDP)


# ── read tools ────────────────────────────────────────────────────────────────

async def _recall(deps: ToolDeps, args: dict) -> dict:
    from memory.retrieval import retrieve
    from shared.types import RetrievalRequest

    req = RetrievalRequest(query=str(args.get("query", ""))[:2000], scope=deps.scope,
                           mode="fast", token_budget=2000)
    result = await retrieve(req, deps.pool, await deps.embedder_factory(), redis=deps.redis)
    items = [
        {"id": i.id, "content": i.text[:400], "tier": i.tier, "score": round(i.score, 4)}
        for i in result.items[:12]
    ]
    return {"items": items, "count": len(items)}


async def _search_graph(deps: ToolDeps, args: dict) -> dict:
    name = str(args.get("name", ""))[:200]
    async with deps.pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT e.id, e.name, e.label, "
                "(SELECT count(*) FROM knowledge_edges k WHERE (k.src_entity_id = e.id "
                " OR k.dst_entity_id = e.id) AND k.valid_to IS NULL) AS degree "
                "FROM knowledge_entities e WHERE e.scope = %s AND e.valid_to IS NULL "
                "AND e.name ILIKE %s ORDER BY degree DESC LIMIT 10",
                (deps.scope, f"%{name}%"),
            )
            rows = await cur.fetchall()
    return {"entities": [{"id": r[0], "name": r[1], "label": r[2], "degree": r[3]}
                         for r in rows]}


async def _list_goals(deps: ToolDeps, args: dict) -> dict:
    from goals import list_goals

    items = await list_goals(deps.pool, deps.scope, status=args.get("status") or "active")
    return {"goals": [{"id": g["id"], "title": g["title"], "status": g["status"],
                       "progress": g["progress"], "horizon": g["horizon"],
                       "success_criteria": g["success_criteria"]} for g in items]}


async def _get_identity(deps: ToolDeps, args: dict) -> dict:
    from identity import get_identity_context

    ctx = await get_identity_context(deps.pool, deps.scope)
    return {"identity": ctx}


# ── memory_write tools ────────────────────────────────────────────────────────

async def _remember(deps: ToolDeps, args: dict) -> dict:
    """Record an episode + a recallable memory (the agent's own provenance)."""
    from memory.governance import add, record_episode

    statement = str(args.get("statement", "")).strip()[:4000]
    if not statement:
        return {"error": "remember: empty statement"}
    epi = await record_episode(deps.pool, deps.publisher, deps.scope,
                               content=statement, source_ref=f"agent:{deps.run_id}")
    mem = await add(deps.pool, deps.publisher, deps.scope, statement=statement,
                    source_episode_id=epi["epi_id"],
                    kind=str(args.get("kind", "fact"))[:50],
                    epistemic_status="canonical")
    return {"epi_id": epi["epi_id"], "mem_id": mem.get("mem_id")}


async def _record_decision(deps: ToolDeps, args: dict) -> dict:
    from goals import record_decision

    conf = args.get("prediction_confidence")
    return await record_decision(
        deps.pool, deps.publisher, deps.scope,
        title=str(args.get("title", ""))[:300],
        chosen=str(args.get("chosen", ""))[:2000],
        rationale=str(args.get("rationale", ""))[:4000],
        context=(str(args["context"])[:4000] if args.get("context") else None),
        reversibility=args.get("reversibility"),
        goal_id=args.get("goal_id"),
        review_at=args.get("review_at"),
        predicted_outcome=(str(args["predicted_outcome"])[:2000]
                           if args.get("predicted_outcome") else None),
        prediction_confidence=(float(conf) if conf is not None else None),
    )


async def _review_decision(deps: ToolDeps, args: dict) -> dict:
    """Close the loop on a past decision: record the structured outcome and,
    if a distilled lesson is given, reinforce/create + mirror it to the KG."""
    from goals import review_decision

    matched = args.get("matched_prediction")
    return await review_decision(
        deps.pool, deps.publisher, deps.scope,
        str(args.get("dec_id", "")),
        outcome=str(args.get("outcome", ""))[:4000],
        valence=str(args.get("valence", "mixed")),
        matched_prediction=(bool(matched) if matched is not None else None),
        lesson=(str(args["lesson"])[:2000] if args.get("lesson") else None),
        applies_to_entities=args.get("applies_to_entities") or None,
        embedder=await deps.embedder_factory(),
    )


async def _recall_lessons(deps: ToolDeps, args: dict) -> dict:
    """Retrieve lessons relevant to a query — the feedback half of the
    decision → outcome → lesson loop, surfaced during hydration."""
    from cognition.decision_loop import retrieve_lessons_async

    query = str(args.get("query", ""))[:2000]
    if not query:
        return {"lessons": [], "count": 0}
    embedding = (await (await deps.embedder_factory()).embed([query]))[0]
    async with deps.pool.connection() as conn:
        lessons = await retrieve_lessons_async(
            conn, scope=deps.scope, query_embedding=embedding,
            domain=args.get("domain"), limit=int(args.get("limit", 5)),
        )
    return {"lessons": lessons, "count": len(lessons)}


async def _create_goal(deps: ToolDeps, args: dict) -> dict:
    from goals import create_goal

    return await create_goal(
        deps.pool, deps.publisher, deps.scope,
        title=str(args.get("title", ""))[:300],
        description=(str(args["description"])[:4000] if args.get("description") else None),
        horizon=args.get("horizon"),
        success_criteria=(str(args["success_criteria"])[:2000]
                          if args.get("success_criteria") else None),
    )


async def _create_sketch(deps: ToolDeps, args: dict) -> dict:
    """Write into the studio (epistemic seed) — the agent's scratchpad output."""
    import studio as studio_mod

    studio_id = args.get("studio_id")
    if not studio_id:
        s = await studio_mod.create_studio(deps.pool, deps.publisher, deps.scope,
                                           title=str(args.get("studio_title", "agent studio"))[:200])
        studio_id = s["id"]
    sk = await studio_mod.create_sketch(deps.pool, deps.publisher, studio_id,
                                        content=str(args.get("content", ""))[:8000])
    return {"studio_id": studio_id, "sketch_id": sk["id"]}


# ── external_post ─────────────────────────────────────────────────────────────

async def _notify(deps: ToolDeps, args: dict) -> dict:
    delivered = await deps.notifier.notify(str(args.get("text", ""))[:1500],
                                           run_id=deps.run_id)
    return {"delivered": bool(delivered)}


# ── real-world tools (file_edit / code_exec / external_post) ───────────────────
# These let a worker actually DO the goal in reality — write the case study,
# update the portfolio repo, run the build to check it — not just record notes.
# Every path/command is forced through orchestration.sandbox (home-confined +
# allowlisted) BELOW the PDP, so even a bypass run can't escape the boundary.

async def _read_file(deps: ToolDeps, args: dict) -> dict:
    from orchestration.sandbox import resolve_in_home
    try:
        p = resolve_in_home(str(args.get("path", "")))
    except ValueError as e:
        return {"error": str(e)}
    if not p.is_file():
        return {"error": f"not a file: {args.get('path')}"}
    try:
        text = p.read_text("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return {"error": f"read failed: {e}"}
    return {"path": str(p), "bytes": len(text), "content": text[:30_000],
            "truncated": len(text) > 30_000}


async def _list_dir(deps: ToolDeps, args: dict) -> dict:
    from orchestration.sandbox import resolve_in_home
    try:
        p = resolve_in_home(str(args.get("path", ".")))
    except ValueError as e:
        return {"error": str(e)}
    if not p.is_dir():
        return {"error": f"not a directory: {args.get('path')}"}
    entries = []
    for child in sorted(p.iterdir())[:200]:
        entries.append({"name": child.name, "dir": child.is_dir()})
    return {"path": str(p), "entries": entries, "count": len(entries)}


async def _write_file(deps: ToolDeps, args: dict) -> dict:
    """Create or overwrite a file (its parent dirs are created as needed)."""
    from orchestration.sandbox import resolve_in_home
    try:
        p = resolve_in_home(str(args.get("path", "")))
    except ValueError as e:
        return {"error": str(e)}
    content = str(args.get("content", ""))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        existed = p.exists()
        p.write_text(content, "utf-8")
    except Exception as e:  # noqa: BLE001
        return {"error": f"write failed: {e}"}
    return {"path": str(p), "bytes": len(content), "action": "overwrote" if existed else "created"}


async def _edit_file(deps: ToolDeps, args: dict) -> dict:
    """Replace an exact, UNIQUE substring in a file (surgical edit)."""
    from orchestration.sandbox import resolve_in_home
    try:
        p = resolve_in_home(str(args.get("path", "")))
    except ValueError as e:
        return {"error": str(e)}
    if not p.is_file():
        return {"error": f"not a file: {args.get('path')}"}
    find = str(args.get("find", ""))
    replace = str(args.get("replace", ""))
    if not find:
        return {"error": "edit_file: 'find' is required"}
    try:
        text = p.read_text("utf-8")
    except Exception as e:  # noqa: BLE001
        return {"error": f"read failed: {e}"}
    n = text.count(find)
    if n == 0:
        return {"error": "edit_file: 'find' string not present in file"}
    if n > 1:
        return {"error": f"edit_file: 'find' string is not unique ({n} matches) — add context"}
    p.write_text(text.replace(find, replace, 1), "utf-8")
    return {"path": str(p), "replacements": 1}


async def _run_command(deps: ToolDeps, args: dict) -> dict:
    """Run an allowlisted build/test/git-local command and return its output."""
    from orchestration.sandbox import run_command
    return await run_command(str(args.get("command", "")), cwd=args.get("cwd"))


async def _git_commit(deps: ToolDeps, args: dict) -> dict:
    """Stage everything and commit in a repo (local only — never pushes)."""
    from orchestration.sandbox import run_command, resolve_in_home
    cwd = args.get("cwd")
    if not cwd:
        return {"error": "git_commit: 'cwd' (repo path) is required"}
    try:
        resolve_in_home(str(cwd))
    except ValueError as e:
        return {"error": str(e)}
    message = str(args.get("message", "")).strip() or "curlyos: agent changes"
    add = await run_command("git add -A", cwd=cwd)
    if add.get("error") or add.get("exit_code") not in (0, None):
        return {"error": "git add failed", "detail": add}
    # shlex-safe commit message via -m with quoting handled by the allowlist runner
    import shlex as _shlex
    commit = await run_command(f"git commit -m {_shlex.quote(message)}", cwd=cwd)
    return {"committed": commit.get("exit_code") == 0, "commit": commit, "add": add}


async def _git_push(deps: ToolDeps, args: dict) -> dict:
    """Push to a remote — DEPLOYS LIVE. Classed external_post so the PDP forces
    human approval even under bypass; only runs after the user grants it."""
    from orchestration.sandbox import resolve_in_home
    cwd = args.get("cwd")
    if not cwd:
        return {"error": "git_push: 'cwd' (repo path) is required"}
    try:
        resolve_in_home(str(cwd))
    except ValueError as e:
        return {"error": str(e)}
    remote = str(args.get("remote", "origin")).strip() or "origin"
    branch = str(args.get("branch", "")).strip()
    # git_push is the ONE place push is permitted — call the binary directly,
    # bypassing the allowlist's push ban (the PDP approval is the gate here).
    import asyncio as _asyncio
    import os as _os
    argv = ["git", "push", remote] + ([branch] if branch else [])
    try:
        p = resolve_in_home(str(cwd))
        proc = await _asyncio.create_subprocess_exec(
            *argv, cwd=str(p),
            stdout=_asyncio.subprocess.PIPE, stderr=_asyncio.subprocess.PIPE,
            env={**_os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        out, err = await _asyncio.wait_for(proc.communicate(), timeout=120)
    except Exception as e:  # noqa: BLE001
        return {"error": f"push failed: {e}"}
    return {"pushed": proc.returncode == 0, "exit_code": proc.returncode,
            "stdout": out.decode("utf-8", "replace")[:4000],
            "stderr": err.decode("utf-8", "replace")[:4000]}


# ── Hermes delegation (net_egress) ─────────────────────────────────────────────
# CurlyOS workers have no native web/browser/image tools. Instead they DELEGATE
# those sub-tasks to the local Hermes agent (:8642), which autonomously uses its
# full toolset and returns the result. The actual internet egress happens inside
# Hermes; from CurlyOS this is a localhost call (egress_host = 127.0.0.1).

_HERMES_HOST = "127.0.0.1"


async def _web_research(deps: ToolDeps, args: dict) -> dict:
    """Delegate a web-research sub-task to Hermes (which searches + reads the web)."""
    from hermes_integration.hermes_client import complete
    query = str(args.get("query", "")).strip()
    if not query:
        return {"error": "web_research: 'query' is required"}
    r = await complete(
        f"Research this on the web and return a thorough, well-sourced summary "
        f"(include key facts, figures, and source URLs):\n\n{query}",
        system="You are a research assistant. Use your web tools to find current, "
               "accurate information and report concisely with sources.",
    )
    return {"query": query, **({"findings": r["text"]} if r.get("ok") else {"error": r.get("error")})}


async def _browse(deps: ToolDeps, args: dict) -> dict:
    """Delegate 'visit this URL and extract X' to Hermes' browser."""
    from hermes_integration.hermes_client import complete
    url = str(args.get("url", "")).strip()
    goal = str(args.get("goal", "extract the main content")).strip()
    if not url:
        return {"error": "browse: 'url' is required"}
    r = await complete(
        f"Visit {url} and {goal}. Return what you found.",
        system="You are a browsing assistant. Use your browser tools to load the "
               "page and extract exactly what was asked.",
    )
    return {"url": url, **({"result": r["text"]} if r.get("ok") else {"error": r.get("error")})}


async def _generate_image(deps: ToolDeps, args: dict) -> dict:
    """Delegate image generation to Hermes; it returns a path/URL to the image."""
    from hermes_integration.hermes_client import complete
    prompt = str(args.get("prompt", "")).strip()
    if not prompt:
        return {"error": "generate_image: 'prompt' is required"}
    r = await complete(
        f"Generate an image for this prompt and tell me the saved file path or URL:\n\n{prompt}",
        system="You are an image-generation assistant. Generate the image and "
               "report the resulting file path or URL.",
    )
    return {"prompt": prompt, **({"result": r["text"]} if r.get("ok") else {"error": r.get("error")})}


async def _delegate_to_hermes(deps: ToolDeps, args: dict) -> dict:
    """General escape hatch: hand any sub-task to the Hermes agent."""
    from hermes_integration.hermes_client import complete
    task = str(args.get("task", "")).strip()
    if not task:
        return {"error": "delegate_to_hermes: 'task' is required"}
    r = await complete(task)
    return {"task": task[:200], **({"result": r["text"]} if r.get("ok") else {"error": r.get("error")})}


REGISTRY: dict[str, Tool] = {
    t.name: t for t in [
        Tool("recall", "read", "Search the user's memory (hybrid semantic+keyword).",
             "query: str", _recall),
        Tool("search_graph", "read", "Find knowledge-graph entities by name.",
             "name: str", _search_graph),
        Tool("list_goals", "read", "List the user's goals with progress and criteria.",
             "status?: active|paused|achieved", _list_goals),
        Tool("get_identity", "read", "The user's current identity facts (who they are).",
             "(none)", _get_identity),
        Tool("remember", "memory_write", "Store a fact/insight into memory (with provenance).",
             "statement: str, kind?: str", _remember),
        Tool("record_decision", "memory_write", "Record a decision in the registry.",
             "title, chosen, rationale, reversibility?: reversible|costly|one_way, goal_id?, "
             "review_at?, predicted_outcome?, prediction_confidence?: 0..1",
             _record_decision),
        Tool("review_decision", "memory_write",
             "Close a decision: record its outcome (scored vs the prediction) and an optional lesson.",
             "dec_id, outcome, valence?: success|partial|failure|mixed|too_early, "
             "matched_prediction?: bool, lesson?, applies_to_entities?: [ent_id]",
             _review_decision),
        Tool("recall_lessons", "read",
             "Retrieve lessons learned from past decisions relevant to a query.",
             "query: str, domain?, limit?", _recall_lessons),
        Tool("create_goal", "memory_write", "Create a new goal.",
             "title, description?, horizon?: life|year|quarter|month, success_criteria?",
             _create_goal),
        Tool("create_sketch", "memory_write", "Write a sketch into the studio (speculative scratchpad).",
             "content: str, studio_id?: str, studio_title?: str", _create_sketch),
        Tool("notify", "external_post", "Send the user a notification message.",
             "text: str", _notify),
        # real-world tools — actually do the work, not just record it
        Tool("read_file", "read", "Read a UTF-8 file under your home directory.",
             "path: str", _read_file),
        Tool("list_dir", "read", "List the entries of a directory under home.",
             "path: str", _list_dir),
        Tool("write_file", "file_edit",
             "Create or overwrite a file (parent dirs auto-created). Use to write real "
             "output: articles, code, docs.",
             "path: str, content: str", _write_file),
        Tool("edit_file", "file_edit",
             "Replace one exact, unique substring in an existing file (surgical edit).",
             "path: str, find: str, replace: str", _edit_file),
        Tool("run_command", "code_exec",
             "Run an allowlisted build/test/git-local command (npm/pnpm/yarn run|build|test, "
             "tsc, git add|commit|status|diff, ls/cat/grep/find) and read its output. No "
             "network installs, no push.",
             "command: str, cwd?: str", _run_command),
        Tool("git_commit", "code_exec",
             "Stage all changes and commit in a repo (local only — does not push).",
             "cwd: str (repo path), message: str", _git_commit),
        Tool("git_push", "external_post",
             "Push commits to a remote — DEPLOYS LIVE. Requires explicit human approval.",
             "cwd: str (repo path), remote?: str, branch?: str", _git_push),
        # Hermes delegation — gives workers web/browser/image via the Hermes agent
        Tool("web_research", "net_egress",
             "Research a topic on the WEB (delegated to the Hermes agent, which searches "
             "and reads pages). Use for any current/external information.",
             "query: str", _web_research, egress_host=_HERMES_HOST),
        Tool("browse", "net_egress",
             "Visit a URL and extract information (delegated to Hermes' browser).",
             "url: str, goal?: str", _browse, egress_host=_HERMES_HOST),
        Tool("generate_image", "net_egress",
             "Generate an image from a prompt (delegated to Hermes); returns a file path/URL.",
             "prompt: str", _generate_image, egress_host=_HERMES_HOST),
        Tool("delegate_to_hermes", "net_egress",
             "Hand an arbitrary sub-task to the Hermes agent (its full toolset). Escape hatch.",
             "task: str", _delegate_to_hermes, egress_host=_HERMES_HOST),
    ]
}


def planner_tool_block() -> str:
    """The tool list as the planner prompt sees it."""
    return "\n".join(f"- {t.name}({t.params}) [{t.action_class}]: {t.description}"
                     for t in REGISTRY.values())


async def execute_tool(name: str, deps: ToolDeps, args: dict) -> dict:
    tool = REGISTRY.get(name)
    if tool is None:
        return {"error": f"unknown tool {name!r}"}
    try:
        return await tool.fn(deps, args or {})
    except Exception as exc:  # noqa: BLE001 — tool failure is an observation, not a crash
        log.exception("tool %s failed", name)
        return {"error": f"{type(exc).__name__}: {exc}"}
