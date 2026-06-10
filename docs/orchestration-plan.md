# Multi-Agent Orchestration in CurlyOS (LangGraph) — webapp control plane + Hermes/Telegram conversation plane

## Context

Hiten operates at AI-adoption Level 6–7: CurlyOS already runs proactive cognition engines, but the
multi-agent work is session-bound. The gap to a standing **Level-8 orchestrator** is multi-agent
orchestration living *inside* CurlyOS — a supervisor directing a team of sub-agents, persisted and
observable.

This plan adds that to `curlyos-core` with **LangGraph**, split across two planes per Hiten's directive:

- **Control plane = the webapp.** Everything *managing* curlyos-core — creating/configuring agents,
  setting goals, run lifecycle, watching runs, approvals — is done in the Next.js webapp.
- **Conversation plane = Hermes → Telegram.** Agents *talk to Hiten* through the **Hermes gateway**
  (Telegram): status, results, questions, and approval prompts are delivered to Telegram; Hiten can
  reply/approve on the go, and replies route back into the relevant run. ("I manage my self-improvement
  agents on the webapp, but I want them to reply to me on Telegram via Hermes.")

First use case: a **self-improvement loop ("alignment fixer")** — a supervisor team that reads cognition
findings (alignment gaps, reflections, identity) and drafts concrete fixes, where any write to
memory/identity **pauses for approval** (approvable from the webapp OR by Telegram reply). Agents run on
**OpenRouter by default** (Hermes-bridge/Claude-Max optional per run). v1 is **manual-trigger**;
autonomous event-triggering is a documented later phase.

## The three services (distinct roles — keep straight)

| Service | Port / addr | Auth | Role here |
|---|---|---|---|
| **curlyos-core** | `127.0.0.1:8643` | — | Control plane API + orchestration engine + persistence (`agent_runs`, langgraph checkpoints) |
| **Hermes gateway** (`~/.hermes/hermes-agent`) | `127.0.0.1:8642` | `API_SERVER_KEY` | **Conversation/delivery**: Telegram bot (polling), sessions, cron `deliver`, `send_message` |
| **hermes-bridge** | `127.0.0.1:8787` | `BRIDGE_API_KEY` | OpenAI-compatible Claude-Max model server — **optional** agent LLM backend |
| webapp (curly-os) | `127.0.0.1:3100` | — | **Control-plane UI**; proxies `/api/*` → curlyos-core |

(The stale "8642" in curlyos-core's `api_server.py` docstring is the Hermes gateway port, not curlyos — curlyos is 8643.)

## Locked decisions

- **Scope:** MVP supervisor team **+ approval-gated writes** + the Hermes/Telegram messaging layer. No
  autonomous triggering in v1.
- **Default model backend:** OpenRouter (metered). hermes-bridge (Claude Max) selectable per run.
- **First template:** `alignment_fixer` (self-improvement loop).
- **Control = webapp; conversation/delivery = Hermes gateway → Telegram.**
- **Framework:** LangGraph 1.x — hand-built `StateGraph` supervisor + `create_react_agent` workers
  (per-step observability + concurrency control), checkpointed via `AsyncPostgresSaver`.

## Architecture & data flow

```
 CONTROL PLANE (webapp :3100 → curlyos-core :8643)
   webapp /orchestrate ──POST /api/agents/runs──▶ create_agent_run()
        (create/configure agents, goals, watch runs, Approve/Reject)   INSERT agent_runs(status='created')
                                                                        BackgroundTasks(_run_orchestration_bg)
                                                                              │
 ENGINE (curlyos-core)                                                        ▼
   cognition/orchestration/run_orchestration()
      build supervisor StateGraph + react workers; AsyncPostgresSaver(thread_id=run_id)
      backend = openrouter (default) | hermes-bridge ; graph.astream(stream_mode="updates")
      each node →  INSERT agent_run_steps  +  publisher.stage(event) → events table   ──▶ webapp live view
      agent calls notify_user(text) / ask_user(q) ────────┐
      write tool → interrupt() → status='awaiting_approval'│ (also delivered to Telegram)
                                                           ▼
 CONVERSATION PLANE (Hermes gateway :8642 ↔ Telegram)
      curlyos messaging client ──POST gateway (API_SERVER_KEY)──▶ deliver to telegram:$TELEGRAM_HOME_CHANNEL
      user replies on Telegram ──▶ Hermes session  ──(poll GET /api/sessions/{id}/messages)──▶ curlyos
              │                                              matches reply ↔ run → resume
              └─ "approve"/"reject"/answer ──▶ POST /api/agents/runs/{id}/resume (Command(resume=...))

 APPROVAL is dual-surface: webapp Approve button OR Telegram reply → same checkpointed interrupt (idempotent).
```

Reuses existing curlyos plumbing (no new infra): `_make_llm_client`-style client build,
`_get_async_pool()` shared pool, `_make_publisher_sync()`/`PgOnlyPublisher.stage`, `BackgroundTasks`
(mirroring `_process_episode_bg`), `simulation_runs` persistence template, and `_load_env_key()` (already
reads `~/.hermes/.env`).

---

## Backend — `curlyos-core`

### Phase 0 — deps & schema (no behavior)

- **pyproject.toml** → optional extra `orchestration`: `langgraph`, `langgraph-checkpoint-postgres==3.1.0`
  (pin), `langchain-openai`, `langchain-core`. Install into existing `.venv` (Python 3.12; `psycopg[pool]>=3.1`
  → checkpointer is psycopg3-native).
- **Schema** — add to `memory/stores/__init__.py` (mirror `SIMULATION_RUNS_DDL`) + wire into `migrate.py`:
  - `agent_runs(id, scope, goal, template, backend, status default 'created', constraints jsonb, plan jsonb,
    outcome jsonb, pending_approval jsonb, hermes_session_id text, step_count int, created_at, started_at,
    completed_at)` — note `hermes_session_id` ties a run to its Telegram conversation.
  - `agent_run_steps(id, run_id, seq, role, kind, content jsonb, status, error, duration_ms, created_at)` + indexes.
- **Checkpointer** — `AsyncPostgresSaver(conn).setup()` once in `migrate.py`; set `LANGGRAPH_STRICT_MSGPACK=true`.

### Phase 1 — orchestration engine (`cognition/orchestration/`)

New module mirroring `cognition/attention/__init__.py`:
- `__init__.py` — `run_orchestration(...)`, `resume_orchestration(...)`.
- `state.py` — `OrchestrationState` TypedDict (`goal, constraints, scope, plan, messages[add_messages],
  worker_outputs[merge], next, step_count, approval`).
- `graph.py` — `build_orchestrator`: `supervisor` node (LLM `route` tool → next worker / `finalize` / END;
  force-finalize past `max_steps`), worker nodes (`create_react_agent(model, role_tools, prompt=role_prompt)`),
  `finalize` node, conditional edges, workers loop back to supervisor.
- `templates.py` — team templates (role prompts + per-role tool subsets). **v1 = `alignment_fixer`:**
  `diagnostician` (read: pulls open alignment gaps + relevant reflections/identity/memory) and `fixer`
  (read + gated write: drafts concrete fixes; may propose `ingest`/`propose_identity_fact`, interrupt-gated;
  calls `notify_user` to surface results on Telegram).
- `backends.py` — `make_chat_model(backend, model, temperature)` → `ChatOpenAI`. OpenRouter (default) via
  `_load_env_key("OPENROUTER_API_KEY")`+`CURLYOS_LLM_MODEL`; hermes-bridge via `base_url="http://127.0.0.1:8787/v1"`,
  `_load_env_key("BRIDGE_API_KEY")`, `claude-sonnet-4-6`. `Semaphore(3)` guards hermes-bridge only (4-session cap);
  server-wide `Semaphore(2)` caps concurrent runs.
- `tools.py` — LangChain `@tool` wrappers over existing curlyos fns (shared pool):
  `recall`→`memory.retrieval.retrieve`, `search`→`/api/search` SQL, `get_identity_context`,
  `get_alignment_gaps`→`alignment_signals`, **`ingest`** (write, gated), **`propose_identity_fact`** (write,
  gated; confidence clamped < 0.75 so agents can't auto-canonicalize), **`notify_user(text)`** and
  **`ask_user(question)`** → the messaging layer (below). `tools.py` filters per role per `constraints.tool_scope`.
- `messaging.py` — **NEW: the Hermes/Telegram client** (§ Conversation plane).
- `persistence.py` — `agent_runs`/`agent_run_steps` writes + event staging (keeps graph code pure).

**Execution** — mirror `_process_episode_bg`: handler returns `{id, status:'created'}` and schedules
`_run_orchestration_bg`. On each `astream` chunk: INSERT `agent_run_steps` + `publisher.stage(build_event(
"orchestration.<verb>", subject=run_id, scope=_scope_obj(scope), data={...}), conn)` (reuse `build_event`/`_scope_obj`
from `simulation/__init__.py`). Verbs: `run.created, supervisor.delegated, worker.started, worker.completed,
tool.called, message.sent, run.awaiting_approval, run.completed, run.failed`. try/except → `status='failed'`.

**Checkpointer caveat:** dedicated `psycopg.AsyncConnection(DSN, autocommit=True, row_factory=dict_row)` per run
(the shared pool is `tuple_row` — never hand it to the saver). Bounded by the run semaphore.

### Endpoints (`api_server.py`, beside `/api/simulation/runs`)

- `POST /api/agents/runs` (async) — `{goal, template="alignment_fixer", backend="openrouter", constraints={},
  scope, auto_approve_writes=false, notify="telegram"}`; INSERT + `BackgroundTasks`. (pattern: `ingest()`)
- `GET /api/agents/runs?scope=&status=&limit=` — list (sync, copy `list_simulation_runs`).
- `GET /api/agents/runs/{id}` — run + ordered steps.
- `POST /api/agents/runs/{id}/resume` (async) — `{decision: approve|reject|edit, payload}`; resume the
  checkpointed thread with `Command(resume=...)`. Called by the webapp Approve button AND by the inbound
  Telegram-reply bridge — idempotent on an already-resolved interrupt.
- `POST /api/agents/inbound` (optional, for the webhook variant) — receives a Telegram reply forwarded by
  Hermes, matches it to a run via `hermes_session_id`, and resolves the pending approval/question.
- v1 live progress = **`GET /api/events?scope=` filtered to `orchestration.*`** (rows already land there).

### Phase 1.1 — writes + human-in-the-loop (dual-surface approval)

- Write tools call `interrupt({"kind":"write_approval","tool":...,"args":...})` (works via checkpointer). The bg
  task sets `status='awaiting_approval'`, stores `agent_runs.pending_approval`, emits `run.awaiting_approval`, AND
  **delivers the approval request (+ the proposed write) to Telegram via `messaging.py`.**
- Approval resolves from **either** surface → `POST .../resume`: the webapp Approve/Reject button, or a Telegram
  reply ("approve"/"reject"/edited text) picked up by the inbound bridge. First responder wins; resume is idempotent.
- Default `constraints.tool_scope='synthesis'` (writes enabled but always interrupt-gated); `auto_approve_writes`
  defaults false.

---

## Conversation plane — Hermes gateway → Telegram (`messaging.py` + inbound bridge)

curlyos owns persistence + execution; **Hermes owns delivery + the Telegram conversation.** A thin curlyos client
talks to the Hermes gateway (`http://127.0.0.1:8642`, Bearer `API_SERVER_KEY` from `~/.hermes/hermes-agent/.env`).

**Outbound — `notify_user(text)` / `ask_user(question)`** (agent → Hiten on Telegram):
- Deliver to `telegram:$TELEGRAM_HOME_CHANNEL` via the gateway. **Confirm the exact one-shot deliver endpoint in
  Phase 0** — candidates found: `POST /api/sessions/{id}/chat` (conversational, returns + tracks replies),
  `POST /api/jobs/{id}/run` with a `deliver:"telegram:…"` relay job, or a direct route in `gateway/delivery.py` /
  the `send_message` tool. **Recommend the session route**: each run opens/uses one Hermes **session**
  (store its id in `agent_runs.hermes_session_id`) so messages and replies are correlated to the run.
- Emit a `orchestration.message.sent` event so the webapp also shows what was sent.

**Inbound — replies back into the run (the piece that does NOT exist yet → build it):**
- **v1 (no Hermes change): polling.** While a run is `awaiting_approval`/`awaiting_input`, a curlyos poller reads
  `GET /api/sessions/{hermes_session_id}/messages` for a new user reply, interprets it (approve/reject/answer),
  and calls `resume`. Correlate strictly by `hermes_session_id ↔ run_id`. Lowest risk; touches no Hermes code.
- **v1.1 optimization (real-time): webhook.** Add a small hook in `gateway/platforms/telegram.py` that POSTs user
  replies to curlyos `POST /api/agents/inbound`. Faster, but modifies the Hermes gateway — defer unless polling
  latency is a problem.

**Scope discipline:** the webapp remains the place to *create/configure* runs and goals. Telegram is for
*conversation* — receiving agent messages and replying/approving. (A future option: let a quick Telegram reply also
trigger a saved run, but full control stays in the webapp.)

---

## Webapp — `/orchestrate` page (the control plane UI)

New route `app/(shell)/orchestrate/page.tsx` (distinct from the conversational `/agent`), reusing
`components/agent/AgentConsole.tsx` + `lib/use-chat-stream.ts`:
- **Manage agents & goals:** create a run (goal, template dropdown, backend toggle OpenRouter/hermes-bridge,
  scope, notify on/off), and (later) saved-agent configs.
- **Live run:** poll `GET /api/events?scope=` filtered to `orchestration.*`; a thin adapter maps event rows →
  the `phase/activity/result` frame shape the hook renders, so the supervisor→worker delegation timeline +
  per-step output (including `message.sent` to Telegram) appear with no new parser. Show **Approve/Reject** when
  `awaiting_approval` → `POST .../resume`.
- **History:** `GET /api/agents/runs` → drill into `GET /api/agents/runs/{id}`.
- Nav: add `{ href:"/orchestrate", label:"Orchestrate", Icon:<new> }` to the **Make** group in `nav.tsx`.

---

## Phasing

- **Phase 0** — deps + schema (incl. `hermes_session_id`) + checkpointer `setup()`; **confirm the Hermes gateway
  deliver endpoint + `API_SERVER_KEY`/`TELEGRAM_HOME_CHANNEL`**. Throwaway DB/port.
- **Phase 1** — engine + `alignment_fixer` (prove supervisor→worker loop **read-only** first) + endpoints +
  webapp `/orchestrate` + `notify_user` outbound to Telegram (status/results). OpenRouter default.
- **Phase 1.1** — gated write tools + `interrupt()` + `/resume` + dual-surface approval (webapp button **and**
  Telegram-reply via the polling inbound bridge) + `ask_user`.
- **Phase 2 (deferred, documented)** — autonomy: a `lifespan` event-subscriber (like `_sweep_unembedded`) that
  reads new `events` by `seq` and auto-dispatches a run on `attention.alignment_gap` (writes still approval-gated,
  per-day budget, debounce); optional Telegram-reply webhook; optional `~/.hermes/cron` standing job.

## Risks & guardrails

- **Runaway loops** — `max_steps`(12)/`max_tool_calls`(20)/`deadline_s` enforced in supervisor + tool counters;
  supervisor force-routes to `finalize` past the cap.
- **Cost (OpenRouter metered)** — cheap worker model (e.g. `openai/gpt-4o-mini`), stronger supervisor; per-run cap;
  hermes-bridge (unmetered Claude Max) one toggle away.
- **Tool blast radius** — writes interrupt-gated; identity confidence clamped < 0.75; every tool call + every
  Telegram message evented for audit.
- **Inbound reply routing is missing in Hermes** — v1 uses polling of the run's Hermes session (no Hermes change);
  webhook is a later optimization. Correlate strictly by `hermes_session_id` to avoid cross-talk.
- **Telegram spam / loops** — cap messages per run; never deliver raw transcripts (summaries only); the `notify`
  flag can be turned off per run.
- **Checkpointer pool/row-factory mismatch** — dedicated `dict_row`+`autocommit` connection for the saver.
- **RAM (~2.2 GB free)** — LangGraph is light (remote LLMs, no local model); one extra conn per active run, bounded.
- **Restart** — backend changes need `sudo systemctl restart curlyos-api` (hand to Hiten). Confirm Hermes gateway is
  up on 8642 and the bot is polling before testing delivery.

## Verification (throwaway port first)

1. **Deps/schema** — install extra in `.venv`; `migrate.py` on a throwaway DB; confirm `agent_runs`,
   `agent_run_steps`, langgraph `checkpoints*`; `setup()` idempotent.
2. **Backend smoke** — 1-tool ReAct agent on `backend=openrouter` (and `hermes`) calling `recall`.
3. **Single run** — curlyos on a throwaway port (e.g. 8699); `POST /api/agents/runs {template:"alignment_fixer"}`
   → `created` → poll to `completed`; assert ≥1 supervisor + ≥1 worker step + matching `orchestration.*` events.
4. **Telegram outbound** — `notify_user` from a run lands a message in Hiten's Telegram via the Hermes gateway;
   confirm `orchestration.message.sent` evented and `hermes_session_id` recorded.
5. **Reply round-trip / HITL** — a write-scoped run parks at `awaiting_approval` AND delivers the approval prompt to
   Telegram; replying "approve" on Telegram (polled from the session) → run resumes and the write lands; "reject" → no
   write; webapp Approve button resolves the same interrupt.
6. **Webapp** — `/orchestrate`: create a run, watch the live timeline (incl. message-sent), approve. Restart the
   production curlyos-core only after the throwaway run is green.

## Critical files

- `curlyos-core/api_server.py` — `/api/agents/runs*` (+ optional `/api/agents/inbound`) endpoints, request models,
  `_run_orchestration_bg`, `migrate`/`lifespan` `setup()`; mirrors `ingest`/`_process_episode_bg`/`/api/simulation/runs`.
- `curlyos-core/cognition/orchestration/` — **new**: `__init__.py`, `graph.py`, `state.py`, `templates.py`
  (`alignment_fixer`), `backends.py`, `tools.py`, **`messaging.py`** (Hermes gateway/Telegram client), `persistence.py`.
- `curlyos-core/memory/stores/__init__.py` + `curlyos-core/migrate.py` — new DDL (+`hermes_session_id`) + checkpointer setup.
- `curlyos-core/shared/events/{__init__,implementations}.py` — `build_event`/`PgOnlyPublisher.stage` reused;
  `simulation/__init__.py` as the `build_event`/`_scope_obj` reference.
- **Hermes gateway** (read-only ref for the client; possibly a small hook in Phase-1.1 webhook): `~/.hermes/hermes-agent/
  gateway/platforms/api_server.py` (HTTP API :8642), `gateway/platforms/telegram.py`, `gateway/delivery.py`,
  `tools/send_message_tool.py`. Auth `API_SERVER_KEY`; target `telegram:$TELEGRAM_HOME_CHANNEL`.
- `curly-os/app/(shell)/orchestrate/page.tsx` (**new**) + `components/agent/AgentConsole.tsx` +
  `lib/use-chat-stream.ts` + `components/shell/nav.tsx` — control-plane page reusing the SSE parser; nav entry.
- `curlyos-core/pyproject.toml` — `orchestration` optional-deps extra.
```
