# Lean refactor — 2026-11-10

CurlyOS-Core was reduced to what the product actually is: a memory system with a
knowledge graph, identity, and the surfaces that feed and read them. The
autonomous-agent layer was removed, cognition was kept.

Rollback point: branch `legacy/pre-lean` (commit `f4d82af`) holds the complete
pre-cut tree, including the orchestration runner, agent gate, studio, simulation,
workspace and evaluation packages.

## Size

| | Before | After | Δ |
|---|---|---|---|
| Packages | 19 | 10 | −9 |
| Python (packages + api_server, excl. deploy/tests) | 22,992 | 15,384 | −7,608 (−33%) |
| API routes | 128 | 51 | −77 |
| Scheduler jobs | 15 | 12 | −3 |
| MCP tools | 17 | 7 | −10 |
| Webapp pages | 31 | 20 | −11 |

## Removed

| Package | Lines | What it was |
|---|---|---|
| `orchestration/` | 4,897 | LangGraph Executive runner, planner, 42 routes, autoplan sweep, sandbox, inbox, prompt evolution |
| `agent/` | 459 | PDP gate, approval service, hashchain audit |
| `workspace/` | 590 | workspace/project hierarchy |
| `studio/` | 512 | studio sessions and sketch graduation |
| `simulation/` | 364 | what-if simulation runs |
| `evaluation/` | 346 | golden datasets, replay gate |

Routes removed: `/api/agents/*`, `/api/approvals*`, `/api/orchestrator/*`,
`/api/scheduled-jobs*`, `/api/inbox*`, `/api/discovery/scan`,
`/api/evolution/*`, `/api/simulation/*`, `/api/studio/*`,
`/api/workspaces*`, `/api/projects*`, `/api/artifacts`, `/api/goals/{id}/plan`,
`/api/goal-plans/*`, `/api/goal-tasks/*`, `/api/settings/auto-*`,
`/api/safety/kill`.

Scheduler jobs removed: `orchestrator_autoplan` (was **every 60 minutes**),
`discovery_scan`, `approval_silence`.

## Kept

`memory/` (stores, retrieval, consolidation, governance), `knowledge/` (graph,
extraction, resolution), `identity/`, `cognition/` (reflection, attention, meta,
narrative, decision loop), `goals/` (goals, decisions, opportunities),
`shared/`, `safety/`, `mcp_server/`, `integrations/`, `hermes_integration/`.

Two pieces were rescued from deleted packages:

- `scope_parts()` moved from `agent/pdp_gate.py` to `safety/pdp.py`
  (repointed in `safety/killswitch.py` and `goals/__init__.py`).
- `orchestration/scheduler.py` moved to `shared/scheduler.py` — a
  dependency-free timer with no autonomy in it.

## Configuration changes

- `CURLYOS_SCHEDULER` was `0` in `.env` (scheduler disabled). Now `1`.
- `CURLYOS_RUNNER` removed from `.env` (the runner no longer exists).
- `shared/settings.py`: `auto_promote`, `auto_plan`, `auto_execute`,
  `agent_bypass` dropped from the registry and deleted from `app_settings`.
- `pyproject.toml`: `orchestration` extra dropped (langgraph,
  langgraph-checkpoint-postgres, langchain-core); package list updated.

## Webapp

Deleted pages: `/agent`, `/approvals`, `/care`, `/evolution`, `/journal`,
`/notes`, `/projects`, `/runs`, `/simulation`, `/studio`, `/workspaces`.
Navigation (`components/shell/nav.tsx`, `AppDock.tsx`) and the command palette
were rewritten to the surviving routes.

Deleted Next routes and libraries: `/api/notes`, `/api/capture`,
`/api/vault-search`, `/api/projects`, `/api/briefing`, and `lib/vault.ts`,
`lib/vault-fs.ts`, `lib/projects.ts`, `lib/project-types.ts`, `lib/briefing.ts`,
`lib/reindex.ts`, `lib/graph.ts`.

`lib/paths.ts` is now just `HOME`. The old constants pointed at `~/mind`,
`~/knowledge-graph` and `~/code/wa-bridge` — none exist on this machine, and
after the page cuts nothing imported them. They were **not** repointed at the
repo's `memory/` and `knowledge/`, which are Python packages, not markdown notes.

Chat (`app/api/chat/route.ts`, `lib/openrouter.ts`) now targets the local
OpenCode Go bridge at `http://127.0.0.1:8799/v1` (model chain
`kimi-k3 → claude-haiku-5-5 → glm-5.3`) instead of OpenRouter, which had no API
key configured.

## Follow-up cleanup pass (same day)

After the first cut, a second pass removed what the deletion had orphaned and
corrected three claims from the first draft:

- `safety/pdp.py` hosted `scope_parts()`, the only symbol the surviving packages
  needed. It now lives in `shared/scope.py`, and `goals/` + `shared/scheduler.py`
  import it from there. **The `safety/` package itself was left in place** — it
  is now callerless but intact, pending a decision.
- `shared/events/catalog.py` went from 58 registered types to 27. Everything
  without a live emitter went: `studio.*`, `simulation.*`, `evolution.*`,
  `runtime.*`, `tool.call.*`, `goal.plan.*`, `goal.task.*`, `goal.progress`,
  `goal.achieved`, `goal.needs_work`, `goal.derived`, `safety.approval.*`,
  `safety.budget.*`, `safety.pdp.*`. Historical rows with those types stay
  readable; they simply can no longer be written.
- `hermes_integration/plugin.py` still advertised `curlyos_pending_approvals`,
  `curlyos_approve` and `curlyos_deny`, which called `/api/approvals*` — routes
  that no longer exist. All three tools and their handlers were removed; five
  remain (recall, add_fact, add_note, invalidate, identity).
- Webapp: `lib/use-event-stream.ts` and `app/api/events/stream/route.ts` were
  deleted. The core has no `/api/events/stream` endpoint at all, so that proxy
  could never have worked. The command palette's stale `! agent` hint is gone.
- `lib/summarize.ts` no longer spawns `~/.local/bin/claude` (absent on this
  machine, so chat summaries silently never ran). It now calls the same OpenCode
  Go bridge as `/api/chat`.

## Known leftovers

Unused code:
- `safety/` (558 lines): `pdp.py`, `killswitch.py`, `budget.py`. Nothing outside
  the package imports it any more. Kept intact rather than deleted.
- `memory/governance/hard_forget()` is unreachable — it gates on a granted row
  in `approvals`, and nothing can mint one now. Not exposed over HTTP; only the
  tests call it. Soft `invalidate` is unaffected.
- Root scripts (`*_smoketest.py`, `curlyos_setup.py`, `import_mind.py`) are
  legacy. Under `deploy/`, only `run_service.py` (launchd entrypoint) and
  `docker-compose.yml` are live; the other 20-odd files are one-off Linux-era
  migrations. `deploy/backup.sh` is written for `/home/hiten/curlyos-core` and
  is not scheduled on this machine.
- 32 ruff findings in the surviving packages (`F401` unused imports, `F841`
  unused locals). No correctness issues.

Unconfigured:
- `hermes_integration/` is kept but not deployed — `~/.hermes/plugins/` does not
  exist and the Hermes bridge on `:8787` is not running.
- The `agentic` LLM tier still resolves in `/api/observability/llm` but nothing
  calls it now that the runner is gone.
- No backup is scheduled. Postgres lives in the `curlyos-pg` Docker container on
  a named volume; nothing snapshots it.

Database:
- 20 tables are referenced by no Python code. Three of them (`chatgpt_chunks`,
  `chatgpt_conversations`, `chatgpt_import_runs`) belong to the in-flight
  ChatGPT import (`migrations/0018_chatgpt_import.sql`), and two
  (`knowledge_edges_bak_20260618`, `knowledge_entities_bak_20260618`) are
  manual backups. The remaining 15 hold data and no longer have a code path:
  `artifacts` (18), `capability_grants` (0), `checkpoints` + `checkpoint_*`
  (2,809), `goal_plans` (81), `goal_tasks` (364), `inbox_items` (184),
  `knowledge_entity_merges` (79), `noesis_kv` (6), `orchestrator_messages` (34),
  `prompt_versions` (1), `scheduled_jobs` (1), `tool_calls` (697),
  `actions` (697), `observations` (697), `approvals` (26).
- **Correction:** `agent_runs` is *live* — `shared/scheduler.py` writes a row for
  every job firing (8,300 rows, and growing). It is not an orphan.
- `approvals`, `actions` and `observations` are still read by
  `memory/governance/` (the forget gate) and by historical queries, so they are
  not orphans either — they are dormant.
