# CurlyOS Core — Development Plan

## Current State Analysis

### What's Implemented (Real Code)
| Module | File | Status | Notes |
|--------|------|--------|-------|
| **shared/types** | `shared/types/__init__.py` | ✅ FULL | All Pydantic models: Episode, Memory, IdentityFact, RetrievalRequest/Result, etc. |
| **shared/types/ulid** | `shared/types/ulid.py` | ✅ FULL | ULID minting, validation, prefix registry |
| **shared/events** | `shared/events/__init__.py` | ✅ FULL | CloudEvents envelope, EventPublisher ABC |
| **shared/events/impl** | `shared/events/implementations.py` | ✅ FULL | PgNatsPublisher, PgOnlyPublisher |
| **shared/embeddings** | `shared/embeddings/__init__.py` | ✅ FULL | Embedder + Reranker ABCs |
| **shared/embeddings/impl** | `shared/embeddings/implementations.py` | ✅ FULL | FakeEmbedder, LocalBgeM3, OpenAIAdapter, FakeReranker |
| **memory/governance** | `memory/governance/__init__.py` | ✅ FULL | record_episode, add, invalidate, forget — all 4 verbs |
| **memory/stores** | `memory/stores/__init__.py` | ✅ FULL | DDL for episodes, memories, identity_facts, events, watermarks |
| **knowledge/resolution** | `knowledge/resolution/__init__.py` | ✅ FULL | Entity resolution |
| **knowledge/extraction** | `knowledge/extraction/__init__.py` | ✅ FULL | Entity extraction |
| **cognition/introspection** | `cognition/introspection/__init__.py` | ✅ FULL | emit_finding() with epistemic-humility contract |

### What's Partial (DDL + stubs, needs real implementation)
| Module | File | Status | What's Missing |
|--------|------|--------|----------------|
| **memory/retrieval** | `memory/retrieval/__init__.py` | 🔶 PARTIAL | DDL + first-stage dense recall stub. Needs: BM25, RRF fusion, graph expansion, rerank, context assembler |
| **memory/consolidation** | `memory/consolidation/__init__.py` | 🔶 PARTIAL | DDL + lock/worker stub. Needs: DEDUP, MERGE, CONFLICT-RESOLVE, SUMMARIZE, DECAY, RECOMBINE/INCUBATE passes |
| **memory/consolidation/scheduler** | `memory/consolidation/scheduler.py` | 🔶 PARTIAL | Scheduler stub. Needs: cron-triggered consolidation runs |
| **identity** | `identity/__init__.py` | 🔶 PARTIAL | propose_identity_fact stub. Needs: get_identity_context, conflict resolution, confidence gating |
| **cognition/meta** | `cognition/meta/__init__.py` | 🔶 PARTIAL | DDL for assumptions, mental_models, decision_audits, principles. Needs: CRUD APIs, blast-radius, audit runs |
| **cognition/attention** | `cognition/attention/__init__.py` | 🔶 PARTIAL | DDL + get_allocation stub. Needs: heatmap, alignment signals, cognitive-load, neglected detection |
| **cognition/reflection** | `cognition/reflection/__init__.py` | 🔶 PARTIAL | DDL + prompts stub. Needs: pattern analysis, goal tracking, identity candidate extraction |
| **cognition/narrative** | `cognition/narrative/__init__.py` | 🔶 PARTIAL | DDL stub. Needs: chapter composition, theme surfacing |
| **knowledge/graph** | `knowledge/graph/__init__.py` | 🔶 PARTIAL | DDL for entities + edges. Needs: CRUD, graph traversal, Neo4j projection |
| **api_server** | `api_server.py` | 🔶 PARTIAL | FastAPI skeleton with health/stats. Needs: all memory/knowledge/identity/cognition endpoints |

### What's Minimal (stub-only, needs full implementation)
| Module | File | Status | What's Needed |
|--------|------|--------|---------------|
| **studio** | `studio/__init__.py` | 🔴 STUB | DDL + create_studio/create_sketch/graduate_sketch stubs. Needs: full CRUD, linking, search, graduation ladder |
| **simulation** | `simulation/__init__.py` | 🔴 STUB | DDL + create/execute/get stubs. Needs: ABM/Monte-Carlo, forking, replay, sensitivity |
| **evaluation** | `evaluation/__init__.py` | 🔴 STUB | GateDecision enum + evaluate_candidate stub. Needs: golden datasets, scorers, gate service, replay |
| **workspace** | `workspace/__init__.py` | 🔴 STUB | create_workspace/create_project/create_task stubs. Needs: full CRUD, scoping, timelines |
| **hermes_integration** | `hermes_integration/provider.py` | 🔴 STUB | Tool schemas + provider class stub. Needs: full MemoryProvider implementation |

### What's Missing Entirely
- **Tests**: No test files exist (`tests/` directory is empty)
- **DDL for new tables**: studio_sketches, simulation_runs, golden_datasets, workspaces, projects, tasks, reflection_reports, life_chapters, themes, alignment_signals, assumptions, mental_models, decision_audits, principles
- **BM25/ParadeDB integration**: Referenced in retrieval but not implemented
- **Neo4j backend**: Knowledge graph uses Postgres fallback; Neo4j driver not wired
- **NATS integration**: PgNatsPublisher supports NATS but no connection setup
- **Consolidation scheduler**: No cron or event-driven trigger
- **Import pipeline**: `import_mind.py` is a stub

## Phase 1: Foundation Hardening (Week 1-2)

### 1.1 — Memory Retrieval Engine (Complete)
**File**: `memory/retrieval/__init__.py`
- Implement BM25 sparse recall via Postgres `tsvector`/`ts_rank` (ParadeDB-style or native)
- Implement Reciprocal Rank Fusion (RRF) combining dense + sparse + entity scores
- Implement Neo4j k-hop graph expansion (or Postgres recursive CTE fallback)
- Implement cross-encoder rerank using `Reranker` ABC
- Implement agentic iterative loop (max 3 rounds, query refinement)
- Implement context assembler with token budgeting, tier allocation, lost-in-the-middle mitigation
- Support `mode="divergent"` with MMR diversity, recency-inverted, speculative graph filtering

### 1.2 — Memory Consolidation Worker (Complete)
**File**: `memory/consolidation/__init__.py`
- Implement DEDUP pass: vector similarity ≥ 0.92 + cross-encoder → merge duplicates
- Implement MERGE/PROMOTE pass: working→episodic→semantic tier promotion
- Implement CONFLICT-RESOLVE pass: invalidate superseded facts
- Implement SUMMARIZE pass: LLM-extract distilled memories from episodes
- Implement DECAY pass: archive cold rows, invalidate expired speculative content
- Implement RECOMBINE/INCUBATE pass: nightly creative pass writing conjecture-level content

### 1.3 — Consolidation Scheduler
**File**: `memory/consolidation/scheduler.py`
- Implement watermark-based event processing (read `events` table by `seq`)
- Implement per-scope locking via Redis
- Implement cron-triggered runs (fast path every 15min, deep path nightly)
- Implement projection advancement (pgvector embeddings, Redis read models)

### 1.4 — Identity Engine (Complete)
**File**: `identity/__init__.py`
- Complete `propose_identity_fact()`: conflict detection, confidence-based auto-promote
- Implement `get_identity_context()`: structured context dict for agent injection
- Implement conflict resolution: invalidate lower-confidence older fact on (scope, predicate) collision
- Add DDL for identity_facts table (already in stores)

### 1.5 — DDL Completion
**File**: `memory/stores/__init__.py` (append)
- Add DDL for: `studio_sketches`, `simulation_runs`, `golden_datasets`, `workspaces`, `projects`, `tasks`, `reflection_reports`, `life_chapters`, `themes`, `alignment_signals`, `assumptions`, `assumption_edges`, `mental_models`, `decision_audits`, `principles`

## Phase 2: Cognition Engines (Week 3-4)

### 2.1 — Meta-Cognition Engine
**File**: `cognition/meta/__init__.py`
- Implement assumption CRUD (create, read, update, invalidate)
- Implement blast-radius query: given assumption X, find all facts/assumptions that rest on it
- Implement mental model CRUD
- Implement decision audit: `POST /audits/run` — analyze decision patterns from episodes
- Implement principle/heuristic distillation
- Wire `metacog.*` events to HITENOS_MEMORY stream

### 2.2 — Reflection Engine
**File**: `cognition/reflection/__init__.py`
- Implement `run_weekly_reflection(scope)`: scan recent episodes, extract patterns
- Implement goal delta tracking (compare goal states across windows)
- Implement identity candidate extraction (high-confidence patterns → propose_identity_fact)
- Implement InsightReport generation with findings[], goal_deltas[], identity_candidates[]
- Wire to Hermes cron: weekly (Monday 6am), monthly (1st 7am)

### 2.3 — Narrative Engine
**File**: `cognition/narrative/__init__.py`
- Implement theme surfacing: cluster episodes by topic, extract recurring themes
- Implement life-chapter composition: detect turning points, bound chapters
- Implement `POST /narrative/compose`: generate narrative summary from themes + chapters
- Wire `narrative.*` events to HITENOS_MEMORY stream

### 2.4 — Attention Engine
**File**: `cognition/attention/__init__.py`
- Implement attention allocation breakdown by category (7d/30d windows)
- Implement focus heatmap grid
- Implement alignment signal detection: value-action gap analysis
- Implement cognitive-load estimation
- Implement neglected-opportunity detection (high-priority goals with low attention)
- Wire `attention.*` events to HITENOS_EVENTS stream

## Phase 3: Knowledge, Studio, Simulation (Week 5-6)

### 3.1 — Knowledge Graph
**File**: `knowledge/graph/__init__.py`
- Implement entity CRUD with bi-temporal edges
- Implement graph traversal: k-hop expansion, path finding
- Implement canonical vs speculative projection separation
- Implement Neo4j backend option (with Postgres fallback)
- Wire to event-sourced projection rebuild

### 3.2 — Studio Engine
**File**: `studio/__init__.py`
- Implement full sketch CRUD with `epistemic_status` constraint (never canonical)
- Implement sketch linking (typed edges between sketches)
- Implement divergent retrieval within studio
- Implement graduation ladder: seed → conjecture → hypothesis → human/eval gate → workspace Project
- Wire `studio.*` events

### 3.3 — Simulation Engine
**File**: `simulation/__init__.py`
- Implement simulation run creation with world model forking
- Implement ABM/Monte-Carlo execution
- Implement outcome distribution storage at `possible_world` status
- Implement forking, replay, sensitivity analysis
- Wire `simulation.*` events

## Phase 4: Evaluation, Workspace, API Server (Week 7-8)

### 4.1 — Evaluation Engine
**File**: `evaluation/__init__.py`
- Implement golden dataset CRUD (content-addressed, sha256)
- Implement scorers: LLM-as-judge, RAG triad, embedding similarity, agent trajectory
- Implement Gate Service: `POST /gate/evaluate` → GateVerdict
- Implement promotion rule: promote iff all suites pass + no regressions + no budget regression
- Implement deterministic replay from Action→Observation events

### 4.2 — Workspace Engine
**File**: `workspace/__init__.py`
- Implement workspace CRUD with scoped memory/agent defaults
- Implement project CRUD within workspaces
- Implement task CRUD with priorities, dependencies
- Implement goal tracking within projects

### 4.3 — API Server (Complete)
**File**: `api_server.py`
- Implement all endpoints from PLAN_WEB.md:
  - `GET /api/health`, `GET /api/stats`
  - `GET/POST /api/memories`, `POST /api/memories/:id/invalidate`
  - `GET /api/episodes`, `GET /api/episodes/:id`
  - `GET/POST /api/identity`
  - `GET /api/graph`, `GET /api/graph/:id/expand`
  - `GET /api/search`
  - `GET /api/cognition/{meta,reflection,attention,narrative}`
  - `GET /api/events`
- Add proper error handling, pagination, filtering
- Add CORS, auth middleware

### 4.4 — Hermes Integration (Complete)
**File**: `hermes_integration/provider.py`
- Complete MemoryProvider implementation
- Wire curlyos_recall, curlyos_add_fact, curlyos_invalidate, curlyos_forget tools
- Wire identity operations
- Test with live Hermes instance

## Phase 5: Testing & Polish (Week 9-10)

### 5.1 — Test Suite
- Unit tests for all governance verbs (record_episode, add, invalidate, forget)
- Unit tests for retrieval pipeline (dense, sparse, RRF, rerank)
- Unit tests for consolidation passes
- Unit tests for identity conflict resolution
- Integration tests: full lifecycle (episode → fact → retrieve → invalidate → forget)
- API tests for all endpoints
- Epistemic-humility contract tests (findings always at hypothesis)

### 5.2 — Import Pipeline
**File**: `import_mind.py`
- Complete mind import from `~/mind/` vault
- Map existing facts to bi-temporal schema
- Generate episodes for each source file
- Validate provenance chain

### 5.3 — Documentation
- API docs (OpenAPI/Swagger from FastAPI)
- Architecture decision records for key tradeoffs
- Developer setup guide

## Dependencies Between Modules

```
shared/types ──┬── shared/events ──┬── memory/governance ──┬── memory/consolidation
               │                   │                        │
               ├── shared/embeddings┘                        ├── memory/retrieval
               │                                             │
               ├── memory/stores (DDL) ──────────────────────┤
               │                                             │
               ├── identity ─────────────────────────────────┤
               │                                             │
               ├── cognition/meta ───────────────────────────┤
               ├── cognition/reflection ─────────────────────┤
               ├── cognition/narrative ──────────────────────┤
               ├── cognition/attention ──────────────────────┤
               ├── cognition/introspection ──────────────────┤
               │                                             │
               ├── knowledge/graph ──────────────────────────┤
               ├── studio ───────────────────────────────────┤
               ├── simulation ───────────────────────────────┤
               ├── evaluation ───────────────────────────────┤
               ├── workspace ────────────────────────────────┤
               │                                             │
               └── api_server ◄──────────────────────────────┘
               └── hermes_integration ◄──────────────────────┘
```

## Key Architectural Constraints (Non-Negotiable)

1. **Epistemic humility**: All introspective findings at `hypothesis`; canonical only on user confirmation
2. **Invalidate-never-delete**: `valid_to` + `superseded_by`, never DELETE on facts
3. **Bi-temporal everywhere**: `valid_from`/`valid_to` + `ingested_at` on every fact
4. **Provenance chain**: Every fact cites `source_episode_id`; every event cites actor + scope
5. **One SoR**: Postgres is authoritative; Neo4j/pgvector/Redis are projections
6. **Event-sourced**: All writes emit events; projections rebuild from event log
7. **Scope isolation**: Every query filtered by scope; no cross-scope leakage
