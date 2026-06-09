# Plan: CurlyOS Core — Autonomous Operation in Hermes Agent

## Current State

### What works
- Plugin is registered and loaded (`plugins.enabled: ['curlyos']`)
- `memory.provider: curlyos` is configured
- All 5 tool schemas exposed (curlyos_recall, curlyos_add_fact, curlyos_add_note, curlyos_invalidate, curlyos_identity)
- Hermes smoketest passes 12/12
- Database has data: 59 episodes, 210 memories, 15 identity_facts, 74 knowledge_entities, 37 knowledge_edges
- LocalBgeM3 embedder loads (real 1024-dim embeddings working)

### Bugs found
1. **Graph expansion SQL parameter mismatch** — `knowledge_edges` query has 21 params but 22 placeholders. Recall falls back to BM25-only.
2. **No `prefetch()` implementation** — The provider returns empty string, so no context is injected into the agent's system prompt automatically.
3. **No `queue_prefetch()` implementation** — Background prefetch for next turn doesn't work.
4. **No `on_session_end()` hook** — Session-end fact extraction never fires.
5. **No `on_pre_compress()` hook** — Insights lost during context compression.
6. **No `on_memory_write()` hook** — Built-in memory writes are not mirrored to CurlyOS.
7. **No `on_delegation()` hook** — Subagent results not captured.
8. **No `on_turn_start()` hook** — Periodic maintenance never runs.
9. **No consolidation scheduler** — `sync_turn` records episodes but never triggers consolidation.
10. **Missing tables** — `studio_sketches`, `simulation_runs`, `simulation_scenarios`, `golden_datasets`, `workspaces`, `projects`, `tasks`, `studios`, `studio_links`, `evaluation_runs` don't exist in DB.
11. **API server not integrated** — `api_server.py` exists but isn't wired into the plugin.
12. **Hermes integration code duplicated** — `hermes_integration/provider.py` is a copy of `plugins/curlyos/__init__.py`, not the source of truth.

## Plan

### Phase 1: Fix Critical Bugs (autonomous memory operation)

#### 1.1 Fix graph expansion SQL
- File: `memory/retrieval/__init__.py`
- Fix the parameter count mismatch in `_graph_expand()`
- Test with actual knowledge_edges data

#### 1.2 Implement prefetch() for automatic context injection
- File: `plugins/curlyos/__init__.py`
- On each turn, call `curlyos_recall` with the user's query
- Format results as fenced context block for the system prompt
- Use background thread + cache for non-blocking operation

#### 1.3 Implement queue_prefetch() for background recall
- Queue the recall in a background thread
- Result consumed by prefetch() on next turn

#### 1.4 Implement on_session_end() for session summarization
- Extract key facts from the conversation
- Write as episodes + memories
- Trigger identity fact proposals from patterns

#### 1.5 Implement on_pre_compress() for compression-time extraction
- Before context is compressed, extract insights from messages about to be discarded
- Write as memories at hypothesis epistemic_status

#### 1.6 Implement on_memory_write() to mirror built-in memory
- When the built-in memory tool writes, mirror to CurlyOS
- Map `memory` → semantic fact, `user` → identity fact

#### 1.7 Implement on_delegation() for subagent capture
- When a subagent completes, record the task+result as an observation

#### 1.8 Implement consolidation trigger in sync_turn
- After recording an episode, check if consolidation is needed
- Run fast-path consolidation (DEDUP + CONFLICT-RESOLVE) every N turns
- Run deep-path consolidation on session_end

### Phase 2: Deploy Missing Tables + Wire API Server

#### 2.1 Create missing tables in database
- Run DDL for: studios, studio_sketches, studio_links, simulation_runs, simulation_scenarios, golden_datasets, workspaces, projects, tasks, evaluation_runs
- Write a migration script that's idempotent (uses IF NOT EXISTS)

#### 2.2 Wire API server into plugin
- Start FastAPI server as background thread in plugin initialize()
- Add health check endpoint
- Add endpoints for all major operations

### Phase 3: Autonomous Cognition Engines

#### 3.1 Implement autonomous reflection (cron-triggered)
- Weekly reflection: scan recent episodes, extract patterns, propose identity facts
- Monthly reflection: deeper analysis + principle distillation
- Wire to Hermes cron system

#### 3.2 Implement autonomous consolidation scheduler
- Background thread in plugin
- Fast path every 15 min: DEDUP + CONFLICT-RESOLVE
- Deep path nightly: full consolidation including SUMMARIZE + DECAY + RECOMBINE

#### 3.3 Implement autonomous meta-cognition
- After every N turns: run decision audit on recent episodes
- Surface assumptions that might be stale
- Propose principle updates

### Phase 4: Single Source of Truth

#### 4.1 Make plugins/curlyos/__init__.py the source of Truth
- hermes_integration/provider.py should import from the plugin, not duplicate
- Or: delete hermes_integration/provider.py and use plugin directly

#### 4.2 Write comprehensive Hermes integration tests
- Test full lifecycle: initialize → turns → prefetch → sync → session_end → shutdown
- Test tool calls with real database
- Test hook interactions
