---
title: Memory
description: Four-tier cognitive memory system (Working/Episodic/Semantic/Procedural) with bi-temporal validity, hybrid recall, and async consolidation.
---

The memory subsystem is the persistent knowledge core of CurlyOS. It stores every user turn as an immutable episode, distils durable facts into a bi-temporal semantic store backed by pgvector, and surfaces the most relevant context through a five-stage hybrid recall pipeline. A background "sleep" worker runs periodic consolidation passes — deduplication, conflict resolution, LLM distillation, decay, and recombination — without ever blocking the hot ingest path.

All writes are append-only or soft-invalidation; nothing is deleted. This invariant preserves full provenance and supports point-in-time queries across all tiers.

## Overview

### The four tiers

| Tier | Backing store | Scope | TTL / lifecycle |
|---|---|---|---|
| **Working** | Redis hash `mem:current:{scope}` | Session-scoped volatile read model | ~2 h (TTL); promoted to Semantic by consolidation |
| **Episodic** | Postgres `episodes` table | Append-only provenance ground-truth | Permanent (never deleted) |
| **Semantic** | Postgres `memories` + pgvector HNSW | Bi-temporal facts (`kind`: fact/preference/procedure) | Soft-invalidated (`valid_to`), never deleted |
| **Procedural** | Postgres `memories` row with `kind=procedure` (+ optional MinIO blob) | Skill/how-to knowledge | Same lifecycle as Semantic |

### Write discipline

The memory subsystem uses a **hot path / deferred path** split inspired by the graphiti/mem0 pattern:

- **Hot path** (`record_episode` + `add`): append-only, no dedup, no graph write. Returns in milliseconds. Every ingest lands here.
- **Async sleep** (`run_consolidation` / `run_once`): the consolidation worker runs every 30 minutes (fast path) or nightly at 03:00 (deep path). It owns all derived writes: embeddings, dedup merges, conflict resolution, LLM distillation, decay, and recombination.

The consolidation worker is the **only** writer of derived stores (pgvector embeddings, Redis read-model). This separation keeps the ingest path simple and crash-safe.

## Data model

### `episodes`

Append-only provenance. Every memory traces back to an episode.

```sql
CREATE TABLE episodes (
  id               text        PRIMARY KEY,   -- ULID prefixed "epi_"
  scope            text        NOT NULL,       -- e.g. "user:usr_hiten"
  content          text        NOT NULL,       -- raw turn content
  source_ref       text,                       -- origin tag (e.g. "web:capture", "jrnl:…")
  modality         text        NOT NULL DEFAULT 'text',
  embedding        vector(1024),               -- filled by consolidation worker
  search_tsv       tsvector,                   -- maintained by trg_episodes_tsv trigger
  ingested_at      timestamptz NOT NULL DEFAULT now(),
  created_at       timestamptz NOT NULL DEFAULT now()
);
```

Indexes: `idx_episodes_hnsw` (HNSW cosine, `m=32 ef_construction=200`), `idx_episodes_scope_time`, `idx_episodes_tsv` (GIN).

### `memories`

The semantic fact store. Bi-temporal, epistemic, append-only-with-invalidation.

```sql
CREATE TABLE memories (
  id                text        PRIMARY KEY,   -- ULID prefixed "mem_"
  scope             text        NOT NULL,
  statement         text        NOT NULL,       -- human-readable fact (may be "[REDACTED]" after forget())
  statement_key     text        NOT NULL,       -- normalised key for overlap detection
  kind              text        NOT NULL DEFAULT 'fact',        -- fact | preference | procedure
  tier              text        NOT NULL DEFAULT 'semantic',    -- working | semantic
  embedding         vector(1024),               -- filled by consolidation worker
  epistemic_status  text        NOT NULL DEFAULT 'canonical',  -- see epistemic axis below
  valid_from        timestamptz NOT NULL,        -- bi-temporal: when fact became true
  valid_to          timestamptz,                -- bi-temporal: NULL = still valid
  ingested_at       timestamptz NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  source_episode_id text        NOT NULL REFERENCES episodes(id),
  superseded_by     text        REFERENCES memories(id)
);
```

Indexes: `idx_memories_hnsw` (HNSW cosine), `idx_memories_scope_current` (partial, `valid_to IS NULL`), `idx_memories_bitemporal` `(scope, valid_from, valid_to)`, `idx_memories_epistemic` (partial, `valid_to IS NULL`), `idx_memories_tsv` (GIN on `search_tsv`).

#### Bi-temporal fields

| Field | Meaning |
|---|---|
| `valid_from` | When the fact became true in the world (defaults to `now()` at insert) |
| `valid_to` | When the fact was superseded; `NULL` means currently valid |
| `ingested_at` | When the row was written to the database (system time, never mutable) |

A row is "active" when `valid_from <= now() AND (valid_to IS NULL OR valid_to > now())`. Point-in-time queries pass an `as_of` timestamp to `_bitemporal_where()`.

#### Epistemic axis (`epistemic_status`)

| Value | Meaning | Recalled by default? |
|---|---|---|
| `canonical` | Established fact | Yes |
| `belief` | User's held worldview or values | Yes |
| `hypothesis` | Unconfirmed proposal | No (only `divergent` mode) |
| `conjecture` | RECOMBINE-generated connection hypothesis | No (only `divergent` mode) |
| `possible_world` | Simulation scenario | No (only `divergent` mode) |
| `seed` | Draft / studio sketch | No (only `divergent` mode) |

The `_epistemic_filter_for_mode` function encodes this mapping: normal recall (`fast` / `deep`) returns `{canonical, belief}`; `divergent` mode returns all six.

#### Epistemic provenance

Every `memories` row carries a `source_episode_id` foreign key. This chain means any recalled fact can be traced to the raw turn that produced it.

### `identity_facts`

Structured key-value self-model entries (predicate/object pairs with confidence). Share the same bi-temporal shape as `memories`.

```sql
CREATE TABLE identity_facts (
  id                text        PRIMARY KEY,
  scope             text        NOT NULL,
  predicate         text        NOT NULL,   -- e.g. "preferred_language"
  object            text        NOT NULL,   -- e.g. "Python"
  confidence        real        NOT NULL,
  epistemic_status  text        NOT NULL DEFAULT 'canonical',
  valid_from        timestamptz NOT NULL,
  valid_to          timestamptz,
  ingested_at       timestamptz NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  source_episode_id text        NOT NULL REFERENCES episodes(id),
  superseded_by     text        REFERENCES identity_facts(id)
);
```

### `events` (write-ahead log)

Immutable event log used by the consolidation worker as a projection source.

```sql
CREATE TABLE events (
  id          text        PRIMARY KEY,
  type        text        NOT NULL,   -- e.g. "art.curlybrackets.curlyos.memory.fact.stored"
  subject     text,
  scope       text        NOT NULL,
  data        jsonb       NOT NULL,
  seq         bigserial   UNIQUE,     -- monotonic sequence number
  created_at  timestamptz NOT NULL DEFAULT now()
);
```

### `projection_watermarks`

Tracks how far each projection (currently `pgvector` and `redis`) has consumed the event log per scope.

```sql
CREATE TABLE projection_watermarks (
  projection  text     NOT NULL,
  scope       text     NOT NULL,
  last_seq    bigint   NOT NULL DEFAULT 0,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (projection, scope)
);
```

### Working memory (Redis)

| Key pattern | Type | Contents |
|---|---|---|
| `mem:current:{scope}` | Hash | `mem_id → statement` for all active canonical memories |
| `lock:consol:{scope}` | String | Per-scope consolidation mutex (TTL 30 s) |
| `cache:retr:{scope}:*` | String | Retrieval result cache entries |
| `cache:recall:{scope}:{gen}:{mode}:{k}:{digest}` | String | Recall API response cache (generation-versioned) |

## Governance

**Module:** `memory/governance/__init__.py`

The governance layer owns the four write/lifecycle verbs. It enforces the invariant that rows are never physically deleted.

### Error classes

| Class | Raised when |
|---|---|
| `SourceEpisodeNotFound(epi_id)` | `source_episode_id` is not a valid `epi_` ULID or does not exist |
| `StatementReserved()` | Caller tries to store the redaction sentinel `[REDACTED]` as a statement |
| `MemoryNotFound(mem_id)` | `mem_id` not found in the given scope |
| `AlreadyInvalidated(mem_id)` | `valid_to` is already set |
| `SupersededByNotFound(mem_id)` | `superseded_by` references a non-existent memory |
| `ForgetRequiresApproval(approval_id)` | No granted, unexpired `memory_forget_hard` approval in scope |
| `ApprovalAlreadyUsed(approval_id)` | Approval has already been consumed by a previous `forget()` |
| `AlreadyForgotten(mem_id)` | Statement is already `[REDACTED]` |

### Helper functions

```python
def statement_key(statement: str) -> str
```
Normalises a statement to a lowercase, whitespace-collapsed, punctuation-stripped key used for overlap detection in DEDUP and CONFLICT-RESOLVE passes.

```python
async def _emit(publisher, subject, ev, type_str) -> None
```
Best-effort NATS emit after commit. Logs a warning on failure; the event is durable in the `events` table regardless.

### Core write functions

```python
async def record_episode(
    pool, publisher, scope_text: str, content: str,
    source_ref: str | None = None, modality: str = "text",
) -> dict
```
Inserts a row into `episodes` (synchronously, inside a transaction) and stages a `memory.episode.recorded` event. Returns `{"epi_id": str, "ingested_at": datetime}`. This is the entry point for every ingest; every `memories` row must trace to an episode through `source_episode_id`.

```python
async def add(
    pool, publisher, scope_text: str, statement: str,
    source_episode_id: str, kind: str = "fact", tier: str = "semantic",
    epistemic_status: str = "canonical", valid_from: datetime | None = None,
) -> dict
```
**Hot path** — append-only fact insert. No dedup, no embedding, no graph write. Validates that `source_episode_id` is a valid `epi_` ULID and exists in `episodes` (FK-checked). Mints a `mem_` ULID, writes to `memories` with `embedding = NULL`, stages `memory.fact.stored`. Returns a `FactRef` dict with `mem_id`, `valid_from`, `ingested_at`, `source_episode_id`.

```python
async def invalidate(
    pool, publisher, scope_text: str, mem_id: str,
    superseded_by: str | None = None, reason: str | None = None,
) -> dict
```
Soft-invalidates a memory by setting `valid_to = now()` and optionally `superseded_by`. Never deletes the row. Raises `AlreadyInvalidated` if `valid_to` is already set. Stages `memory.fact.invalidated` with `hard=False`.

```python
async def forget(
    pool, publisher, scope_text: str, mem_id: str,
    approval_id: str, reason: str,
) -> dict
```
Hard-forget: replaces `statement` and `statement_key` with `[REDACTED]`, sets `valid_to` if open, keeps the tombstone row. Gated by two checks: (1) a granted, unexpired `memory_forget_hard` approval in scope, (2) single-use enforcement via the `events` table. Uses `pg_advisory_xact_lock` to serialise concurrent forgets on the same approval. Stages `memory.fact.invalidated` with `hard=True` and `approval_id`.

### SoR read helpers (not ranked retrieval)

```python
async def list_episodes(pool, scope_text: str, limit: int = 100) -> list[dict]
```
Returns up to `limit` episodes ordered by `created_at DESC`. Fields: `id`, `content`, `source_ref`, `ingested_at`.

```python
async def list_memories(pool, scope_text: str, limit: int = 100) -> list[dict]
```
Returns up to `limit` active (`valid_to IS NULL`) memories ordered by `created_at DESC`. Fields: `id`, `statement`, `kind`, `valid_from`, `source_episode_id`.

### Capture hygiene

The ingest endpoint calls `_strip_scaffolding(text)` before calling `record_episode`. Turns that consist entirely of framework scaffolding (tool call wrappers, system prompts injected by the harness) produce an empty string and are skipped without creating any rows. This prevents scaffolding noise from polluting `episodes`, `memories`, or knowledge extraction.

## Retrieval

**Module:** `memory/retrieval/__init__.py`

The retrieval pipeline is a five-stage hybrid system that combines dense vector search, sparse full-text search, entity matching, graph expansion, and cross-encoder reranking.

### Pipeline overview

```mermaid
flowchart TD
    Q[Query] --> S1A[Dense recall\npgvector HNSW]
    Q --> S1B[Sparse recall\nBM25 / tsvector]
    Q --> S1C[Entity match\nidentity_facts]
    S1A & S1B & S1C --> S2[RRF fusion\n+ recency boost]
    S2 --> S3A[Graph expansion\nrecursive CTE k-hop]
    S3A --> S3B[Cross-encoder rerank\nbge-reranker-v2-m3]
    S3B --> S4[Agentic iterative loop\nmax 1 round fast/divergent]
    S4 --> S5[Context assembler\ntoken-budgeted tier allocation]
    S5 --> R[RetrievalResult]
```

### Stage 1: Hybrid first-stage

All three sub-stages run as concurrent `asyncio` tasks.

```python
async def _dense_recall(
    pool, embedder, query: str, scope: str,
    k: int = 20, ef_search: int = 64, as_of: datetime | None = None,
    epistemic_filter: frozenset[str] = frozenset({"canonical"}),
) -> list[dict]
```
pgvector HNSW cosine search on `memories.embedding`. Sets `hnsw.ef_search` per-query (64 for fast, 128 for deep; clamped to 10–400). Returns up to `k` rows with `score = 1 - cosine_distance`.

```python
async def _sparse_recall(
    pool, query: str, scope: str,
    k: int = 20, as_of: datetime | None = None,
    epistemic_filter: frozenset[str] = frozenset({"canonical"}),
) -> list[dict]
```
BM25 via Postgres `tsvector`. Uses the precomputed `search_tsv` column (maintained by `trg_memories_tsv` trigger, indexed by `idx_memories_tsv` GIN) to avoid expensive per-row `to_tsvector()` calls. First tries `plainto_tsquery`; falls back to `websearch_to_tsquery` if no results.

```python
async def _entity_match(
    pool, query: str, scope: str,
    k: int = 10, as_of: datetime | None = None,
) -> list[dict]
```
Case-insensitive `ILIKE` scan on `identity_facts.predicate` and `identity_facts.object`. Returns deterministic self-model results (e.g. "preferred_language = Python") with confidence as score.

### Stage 2: Reciprocal Rank Fusion

```python
def _rrf_fuse(
    candidates: list[dict],
    dense_ranked: list[dict], sparse_ranked: list[dict], entity_ranked: list[dict],
    recency_weight: float = 0.3, divergent: bool = False,
) -> list[dict]
```
RRF score formula: `Σ(1 / (60 + rank))` summed across up to three signal channels. A recency boost multiplier `exp(-0.01 * age_in_days)` is applied. In `divergent` mode the boost is inverted so older, less-recently-accessed memories are surfaced instead.

### Stage 3a: Graph expansion

```python
async def _graph_expand(
    pool, seed_ids: list[str], scope: str,
    k_hops: int = 1, as_of: datetime | None = None,
    epistemic_filter: frozenset[str] = frozenset({"canonical"}),
) -> list[dict]
```
A six-step recursive CTE:

1. Resolve top-5 seed `memories.id` → `source_episode_id`
2. Find `knowledge_entities` linked to those episodes
3. Walk `knowledge_edges` up to `k_hops` (1 for fast, 2 for deep)
4. Collect all reached entities
5. Find episodes linked to reached entities
6. Fetch memories from those episodes

Score is `1 / (1 + depth)`. All entity/edge traversals are guarded by bi-temporal predicates. Graph failures are caught and logged; the pipeline continues without graph results (`graph_skipped=True` in result).

### Stage 3b: Cross-encoder rerank

```python
async def _rerank(reranker, query: str, candidates: list[dict], top_k: int = 30) -> list[dict]
```
If a reranker is wired, calls `reranker.rerank(query, docs, top_k=top_k)` to produce cross-encoder scores. Without a reranker, returns the top `top_k` candidates from RRF order unchanged.

### Stage 4: Agentic iterative loop

```python
def _detect_coverage_gap(query: str, items: list[dict]) -> str | None
```
Heuristic: if the top result has `fused_score < 0.05`, returns a shortened follow-up query. If the query contains aspect markers (`and`, `or`, `vs`, etc.) and fewer than 3 results were found, extracts the second aspect as a follow-up. Returns `None` when coverage is sufficient.

**Round limits by mode:**
- `fast`: 0 rounds (unless `recall_fast_followups` setting is `True`, then 1)
- `deep`: 0 rounds (measured 2026-06-16: 3 follow-up rounds added latency with zero quality gain; `ef_search=128, k=50` already covers the query pool)
- `divergent`: 1 round (consumed in fused order by the orchestration discovery workflow)

### Stage 5: Context assembler

```python
def _assemble_context(items: list[dict], budget: int = 4000) -> tuple[list[dict], int, bool]
```
Token-budgeted packing with tier allocation:

| Tier | Budget share |
|---|---|
| `semantic` | 40% |
| `episodic` | 30% |
| `graph` | 20% |
| `working` | 10% |

Deduplicates by text prefix (first 100 chars). Lost-in-the-middle mitigation: highest-scoring item at position 0, second-highest at the end. Returns `(chosen_items, used_tokens, truncated)`.

### Divergent mode: MMR diversification

```python
def _mmr_diversify(items: list[dict], lambda_param: float = 0.5, top_k: int = 30) -> list[dict]
```
Maximal Marginal Relevance re-ordering applied only in `divergent` mode. Balances relevance against diversity using word-overlap as a similarity proxy. `lambda_param=1.0` is pure relevance, `0.0` is pure diversity.

### Main entry point

```python
async def retrieve(
    request: RetrievalRequest,
    pool, embedder, reranker=None, redis=None,
    fast_followup: bool = False,
) -> RetrievalResult
```
Orchestrates the full five-stage pipeline. `RetrievalRequest` fields: `query`, `scope`, `mode` (`fast`|`deep`|`divergent`), `token_budget`, `as_of`. Returns a `RetrievalResult` with `items: list[RetrievedItem]`, `used_tokens`, `rounds`, `truncated`, `cache_key`, `graph_skipped`, `reranked`.

### Bi-temporal helper

```python
def _bitemporal_where(as_of: datetime | None = None) -> tuple[str, list]
```
Returns the SQL fragment `valid_from <= %s AND (valid_to IS NULL OR valid_to > %s)` plus the two bind parameters. Used by all three Stage 1 queries and graph expansion.

```python
def _epistemic_filter_for_mode(mode: str) -> frozenset[str]
```
Maps a mode string to the set of `epistemic_status` values to include. Normal modes include `canonical` and `belief`; `divergent` includes all six values.

## Consolidation

**Module:** `memory/consolidation/__init__.py`  
**Scheduler:** `memory/consolidation/scheduler.py`

### Constants and thresholds

| Name | Value | Meaning |
|---|---|---|
| `PROJECTIONS` | `("pgvector", "redis")` | Projection names tracked in `projection_watermarks` |
| `CONSOL_LOCK_TTL_MS` | `30_000` | Redis mutex TTL for per-scope consolidation |
| `_DEDUP_SIMILARITY_THRESHOLD` | `0.92` | Cosine similarity floor to consider a pair as duplicate candidates |
| `_DEDUP_CONFIRM_THRESHOLD` | `0.85` | Cross-encoder score floor to confirm and merge a pair |
| `_DEDUP_AUTO_MERGE_SIM` | `0.985` | Near-exact similarity threshold for auto-merge when no reranker is available |
| `_DECAY_COLD_DAYS` | `90` | Days without access before working-tier canonical memories are archived |
| `_DECAY_SPECULATIVE_STATUSES` | `("seed", "conjecture", "possible_world")` | Statuses invalidated after 90 days |
| `_DISTILL_EPISODE_LIMIT` | `12` | Maximum episodes processed per SUMMARIZE pass |
| `_DISTILL_MAX_FACTS` | `3` | Maximum facts extracted per episode |
| `FAST_PASSES` | `("summarize", "dedup", "conflict_resolve")` | Passes run on the 30-minute cadence |
| `ALL_PASSES` | `("dedup", "merge_promote", "conflict_resolve", "summarize", "decay", "recombine_incubate")` | Passes run on the nightly deep path |

### Projection worker

```python
async def project_scope(
    pool, redis, embedder, publisher,
    scope_text: str, *, live: bool, replay: bool = False,
) -> dict[str, Any]
```
The per-scope event projection loop. Acquires a Redis mutex, reads unprocessed events from the `events` table (seq `> last_watermark`), dispatches each event to `project_event`, advances the watermark, then applies deferred Redis ops. If `replay=True`, clears all embeddings and resets the watermark to zero first (full replay from scratch).

```python
async def project_event(
    conn, ops: list[tuple], embedder,
    ev_type: str, subject: str | None, data: dict | None, scope_text: str,
) -> str
```
Dispatches a single event to the correct projector. Recognised short-types: `memory.fact.stored`, `memory.episode.recorded`, `memory.fact.invalidated`. Returns an action string (`"embedded"`, `"episode"`, `"invalidated"`, `"tombstoned"`, `"missing"`, `"noop"`).

Sub-projectors:

```python
async def project_fact_stored(conn, ops, embedder, mem_id: str, scope_text: str) -> str
```
Embeds the statement via `embedder.embed([statement])` and writes the vector to `memories.embedding`. For tombstoned (redacted) memories, sets `embedding = NULL` and queues a Redis `hdel`. Queues `hset` on the `mem:current:{scope}` hash for active memories.

```python
async def project_episode_recorded(conn, embedder, epi_id: str) -> str
```
Embeds episode content and writes to `episodes.embedding`.

```python
async def project_fact_invalidated(conn, ops, mem_id: str, scope_text: str) -> str
```
Clears `embedding` for tombstoned rows, queues Redis `hdel` from the `mem:current:{scope}` hash.

```python
async def run_once(
    pool, redis, embedder, publisher,
    *, scope: str | None = None, replay: bool = False, live: bool = True,
) -> dict[str, Any]
```
Projects all scopes with unprocessed events (or a single `scope` if specified). Does not run consolidation passes — only event projection. Used by the `ConsolidationScheduler` background thread.

### Consolidation passes

All passes are scoped (`scope_text` parameter) and return a `dict` with pass-specific metrics.

#### DEDUP pass

```python
async def _pass_dedup(pool, redis, embedder, reranker, publisher, scope_text: str) -> dict
```
Finds all active memories with embeddings (capped at 2000 most-recent), runs a pgvector self-join to find pairs with cosine similarity `>= 0.92`. For each pair:

- **With reranker**: confirms with cross-encoder score `>= 0.85`.
- **Without reranker**: only auto-merges pairs with similarity `>= 0.985` (near-exact), to prevent destructive over-merging of distinct-but-related memories.

Merge: invalidates the older (`created_at`) memory, setting `superseded_by` to the newer. Emits `memory.fact.invalidated` with `reason="dedup_merge"`. Returns `{candidates, merged, errors}`.

#### MERGE/PROMOTE pass

```python
async def _pass_merge_promote(pool, redis, embedder, publisher, scope_text: str) -> dict
```
Finds memories with `tier='working'` and promotes them to `tier='semantic'` inside a transaction that also creates a synthetic episode (source `working:{mem_id}`). Returns `{promoted, errors}`.

#### CONFLICT-RESOLVE pass

```python
async def _pass_conflict_resolve(pool, redis, publisher, scope_text: str) -> dict
```
Two sub-passes:

1. **Memories**: finds pairs sharing the same `statement_key` with `valid_to IS NULL`. Invalidates the older.
2. **Identity facts**: finds pairs sharing the same `predicate` with `valid_to IS NULL`. Invalidates the lower-confidence one.

Returns `{conflicts, resolved, errors}`.

#### SUMMARIZE pass (LLM distiller)

```python
async def _pass_summarize(
    pool, redis, embedder, publisher, scope_text: str,
    llm_client=None, llm_model: str = "",
) -> dict
```
The only producer of recallable memories from raw episode turns. Processes up to 12 episodes per pass that have no derived memory yet. Skips episodes with `source_ref LIKE 'jrnl:%'` (journal entries are written directly to memories at ingest). No-ops when `llm_client is None` — there is no regex/sentence-splitting fallback.

```python
async def _distill_episode_facts(llm_client, llm_model: str, content: str) -> list[tuple[str, str]] | None
```
Calls the LLM with a structured prompt that instructs it to extract only durable, long-term facts (stable preferences, concrete decisions, system/project facts, identity). Returns a list of `(statement, kind)` tuples or `None` on LLM failure (rate limit/timeout). On `None`, the pass aborts and defers remaining episodes rather than hammering more failing LLM calls. On an empty list (chatter-only turn), inserts a marker memory (`tier='working'`, `valid_to=now()`, `statement='(no durable facts)'`) to satisfy the `NOT EXISTS` guard and prevent re-distillation.

SUMMARIZE pass is the canonical cleaning choke point and the sole source of recallable facts from raw turns.

#### DECAY pass

```python
async def _pass_decay(pool, redis, publisher, scope_text: str) -> dict
```
Two sub-passes (both use a 90-day cutoff):

1. Archives `working`-tier `canonical` memories older than 90 days by setting `valid_to = now()`.
2. Invalidates any memory with `epistemic_status IN ('seed', 'conjecture', 'possible_world')` older than 90 days.

Returns `{archived, invalidated_speculative, errors}`.

#### RECOMBINE/INCUBATE pass

```python
async def _pass_recombine_incubate(pool, redis, embedder, publisher, scope_text: str) -> dict
```
Nightly creative pass. Finds pairs of canonical active memories whose word-overlap falls in the `[0.2, 0.7]` band (related but not duplicates). For each pair, generates a conjecture statement with `epistemic_status='conjecture'`, capped at 20 conjectures per run. Conjecture memories are invisible to normal recall (only `divergent` mode surfaces them) and are auto-decayed after 90 days. Returns `{clusters_found, conjectures, errors}`.

### Orchestrator

```python
async def run_consolidation(
    pool, redis, embedder, publisher, reranker=None,
    *, scope: str | None = None, deep: bool = False,
    llm_client=None, llm_model: str = "",
) -> dict[str, Any]
```
Full consolidation pipeline. Step 1: `project_scope` for event projection. Step 2: runs the applicable pass set (`FAST_PASSES` or `ALL_PASSES` based on `deep`). Returns a structured result with per-scope, per-pass metrics and `completed_at` timestamp.

### Public pass aliases

These are thin wrappers for task/API use:

```python
async def _dedup_pass(pool, scope: str, embedder, reranker) -> int
async def _conflict_resolve_pass(pool, scope: str) -> int
async def _summarize_pass(pool, scope: str, publisher, embedder) -> int
async def _decay_pass(pool, scope: str) -> int
async def _recombine_pass(pool, scope: str, publisher, embedder) -> int
```

Each returns a single integer count of operations performed.

### Scheduler

**Module:** `memory/consolidation/scheduler.py`

```python
class ConsolidationScheduler:
    def __init__(self, pool, redis, embedder, publisher, interval_seconds: int = 1800)
    def start(self) -> None
    def stop(self) -> None
    def run_once_sync(self, scope: str | None = None, replay: bool = False) -> dict
    @property is_running: bool
    @property last_run_at: str  # ISO-8601 or "never"
```
Spawns a daemon thread (`curlyos-consolidation`) that calls `run_once` every `interval_seconds` (default 30 minutes). Sleep is broken into 1-second increments so `stop()` is responsive. Each loop iteration creates and closes its own `asyncio` event loop to avoid interference with the main process loop.

```python
async def start_scheduler(pool, redis, embedder, publisher, interval_seconds: int = 1800) -> ConsolidationScheduler
```
Creates and starts a `ConsolidationScheduler`. Called at application startup (unless `CURLYOS_SCHEDULER=0`).

```python
async def run_fast_path(pool, redis, embedder, publisher, reranker=None, scope: str | None = None) -> dict
```
Runs `run_consolidation(deep=False)` — SUMMARIZE + DEDUP + CONFLICT-RESOLVE.

```python
async def run_deep_path(pool, redis, embedder, publisher, reranker=None, scope: str | None = None) -> dict
```
Runs `run_consolidation(deep=True)` — all six passes.

```python
async def get_consolidation_status(pool, scope: str) -> dict
```
Reads `projection_watermarks` and counts pending events. Returns `{last_seq, last_run_at, events_pending, status}` where `status` is `"idle"`, `"stale"`, or `"empty"`.

```python
def run_consolidation_standalone(
    dsn: str | None = None, redis_url: str | None = None,
    scope: str | None = None, replay: bool = False,
    embedder_type: str = "fake",
) -> dict
```
Synchronous, dependency-free runner for Hermes cron and CLI use (`python3 -m memory.consolidation.scheduler`). Falls back to `CURLYOS_DATABASE_URL` and `CURLYOS_REDIS_URL` env vars. Supports `embedder_type="bge-m3"` or `"fake"`.

## Stores

**Module:** `memory/stores/__init__.py`

This module is a DDL registry — it contains no runtime code, only SQL string constants. The application creates tables by executing these strings at startup.

### DDL constants

| Constant | Tables / objects created |
|---|---|
| `EPISODES_DDL` | `episodes` table, `idx_episodes_hnsw`, `idx_episodes_scope_time` |
| `MEMORIES_DDL` | `memories` table, HNSW + scope-current + bitemporal + epistemic indexes |
| `IDENTITY_FACTS_DDL` | `identity_facts` table, predicate-current + bitemporal indexes |
| `EVENTS_DDL` | `events` table |
| `PROJECTION_WATERMARKS_DDL` | `projection_watermarks` table |
| `MEMORIES_TSV_DDL` | `search_tsv` column on `memories`, `idx_memories_tsv` GIN, `trg_memories_tsv` trigger |
| `EPISODES_TSV_DDL` | `search_tsv` column on `episodes`, `idx_episodes_tsv` GIN, `trg_episodes_tsv` trigger |
| `ALL_DDL` | Concatenation of all DDL strings above (plus studio, simulation, workspace, task, evaluation DDL) |

### Embedding backend

The consolidation worker calls `embedder.embed([statement])` (batch) and `embedder.embed_single(query)` (single). The production backend is `LocalBgeM3` (BGE-M3, 1024-dimensional vectors). Tests and CLI use `FakeEmbedder`. Recall uses a `CachingEmbedder` wrapper that memoises the query embedding to avoid re-encoding across multiple pipeline stages.

### HNSW index parameters

Both `episodes` and `memories` use `HNSW` with `m=32, ef_construction=200` and `vector_cosine_ops`. The `ef_search` parameter is set per-query at retrieval time (`SET LOCAL hnsw.ef_search = N`).

## Public API surface

The functions other modules and the REST layer call directly:

| Function | Module | Description |
|---|---|---|
| `record_episode(pool, publisher, scope, content, ...)` | `memory.governance` | Append-only episode insert; returns `epi_id` |
| `add(pool, publisher, scope, statement, source_episode_id, ...)` | `memory.governance` | Hot-path fact insert; returns `mem_id` |
| `invalidate(pool, publisher, scope, mem_id, ...)` | `memory.governance` | Soft-invalidate a memory |
| `forget(pool, publisher, scope, mem_id, approval_id, reason)` | `memory.governance` | Hard-redact with approval gate |
| `list_episodes(pool, scope, limit)` | `memory.governance` | SoR read for REST list endpoint |
| `list_memories(pool, scope, limit)` | `memory.governance` | SoR read for REST list endpoint |
| `retrieve(request, pool, embedder, reranker, redis, fast_followup)` | `memory.retrieval` | Full five-stage recall pipeline |
| `run_once(pool, redis, embedder, publisher, ...)` | `memory.consolidation` | Project events for all or one scope |
| `run_consolidation(pool, redis, embedder, publisher, ...)` | `memory.consolidation` | Full consolidation with pass selection |
| `start_scheduler(pool, redis, embedder, publisher, interval_seconds)` | `memory.consolidation.scheduler` | Start background consolidation thread |
| `run_fast_path(pool, redis, embedder, publisher, ...)` | `memory.consolidation.scheduler` | Fast (SUMMARIZE+DEDUP+CONFLICT) path |
| `run_deep_path(pool, redis, embedder, publisher, ...)` | `memory.consolidation.scheduler` | Nightly deep path (all 6 passes) |
| `get_consolidation_status(pool, scope)` | `memory.consolidation.scheduler` | Watermark + pending event count |

## Related REST endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/ingest` | Record text as an episode + memory; schedules background KG extraction |
| `GET` | `/api/episodes` | List episodes for a scope with optional scope/source_ref filter |
| `GET` | `/api/episodes/{epi_id}` | Get a single episode with its derived memories |
| `GET` | `/api/memories` | List active memories with optional full-text filter |
| `GET` | `/api/memories/{mem_id}` | Get a single memory with provenance chain |
| `POST` | `/api/memories` | Directly insert a memory row (requires existing `source_episode_id`) |
| `POST` | `/api/memories/{mem_id}/invalidate` | Soft-invalidate a memory (sets `valid_to = now()`) |
| `POST` | `/api/recall` | Hybrid semantic recall (dense + BM25 + graph + rerank); the authoritative recall path |
| `GET` | `/api/search` | Lightweight BM25-only text search over `memories` |
| `POST` | `/api/consolidation/run` | Trigger a consolidation run (`mode="fast"` or `"deep"`) |
| `GET` | `/api/identity` | List active `identity_facts` for a scope |
| `POST` | `/api/identity` | Insert an `identity_fact` |
| `GET` | `/api/graph` | List knowledge entities |
| `GET` | `/api/graph/{entity_id}/expand` | K-hop graph expansion from an entity |
| `GET` | `/api/stats` | Total counts for `episodes`, `memories`, `identity_facts`, etc. |
| `GET` | `/api/stats/composition` | Memories breakdown by `epistemic_status` and `tier` |

## Configuration and settings

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `CURLYOS_DATABASE_URL` | `postgresql://curlyos:***@localhost:54321/curlyos` | Postgres DSN |
| `CURLYOS_REDIS_URL` | `""` (disabled) | Redis URL; working-memory and caching are skipped when empty |
| `CURLYOS_SCOPE` | `user:usr_hiten` | Default scope for single-user deployments |
| `CURLYOS_LOG_LEVEL` | `INFO` | Python logging level |
| `CURLYOS_SCHEDULER` | `1` | Set to `0` to disable the background consolidation thread |
| `CURLYOS_PREWARM_EMBEDDER` | `1` | Set to `0` to skip BGE-M3 pre-warm on startup |
| `CURLYOS_LLM_API_KEY` | — | Default LLM API key (OpenRouter or provider-specific) |
| `CURLYOS_LLM_BASE_URL` | `https://openrouter.ai/api/v1` | Default LLM base URL |
| `CURLYOS_LLM_MODEL` | — | Default LLM model ID (FAST tier) |
| `CURLYOS_AGENTIC_API_KEY` | falls back to `CURLYOS_LLM_API_KEY` | AGENTIC tier API key |
| `CURLYOS_AGENTIC_BASE_URL` | falls back to `CURLYOS_LLM_BASE_URL` | AGENTIC tier base URL |
| `CURLYOS_AGENTIC_MODEL` | — | AGENTIC tier model ID (used for SUMMARIZE distiller) |
| `CURLYOS_DEEP_API_KEY` | falls back to `CURLYOS_LLM_API_KEY` | DEEP tier API key |
| `CURLYOS_DEEP_MODEL` | — | DEEP tier model ID |
| `CURLYOS_API_PORT` | `8643` | HTTP server port |

### Settings registry knobs

These are read via `get_setting_cached(pool, key, default)` from the Postgres `settings` table at request time.

| Key | Default | Description |
|---|---|---|
| `recall_cache_enabled` | `True` | Enable generation-versioned Redis cache for `/api/recall` |
| `recall_cache_ttl_seconds` | `120` | TTL for cached recall results |
| `recall_fast_followups` | `False` | Enable agentic follow-up round for `fast` mode recall |
| `epistemic_classify_enabled` | `True` | Run epistemic classification on new memories during ingest |
| `kg_extraction_enabled` | `True` | Run knowledge graph extraction on new episodes during ingest |

### Recall cache invalidation

The cache is generation-versioned: each scope has a Redis counter (`recall:gen:{scope}`). Every `/api/ingest` call increments this counter (`_bump_recall_gen`), which immediately invalidates all cached recall results for that scope without a key scan.

## Gotchas and edge cases

**No embeddings on hot path.** `add()` inserts with `embedding = NULL`. A recall issued before the next consolidation pass runs will miss newly ingested facts in the dense stage. BM25 (`_sparse_recall`) will still find them if the `trg_memories_tsv` trigger has fired (it fires on INSERT synchronously).

**DEDUP safety without reranker.** Vector similarity in the `[0.92, 0.985)` band routinely matches distinct-but-related memories. Without a cross-encoder, the dedup pass only auto-merges pairs at similarity `>= 0.985`. Wiring a real reranker is required for reliable dedup in the middle band.

**`jrnl` ingest bypasses SUMMARIZE.** Episodes with `source_ref LIKE 'jrnl:%'` are skipped by the SUMMARIZE pass because journal entries are written directly to `memories` at ingest (not extracted from the raw episode). The `NOT EXISTS` guard in SUMMARIZE relies on there being at least one derived `memories` row for the episode.

**Marker memories for chatter-only turns.** When the LLM distiller returns an empty fact list, SUMMARIZE inserts a `(no durable facts)` marker memory at `tier='working'`, `valid_to=now()`, `epistemic_status='hypothesis'`. This row is invisible to recall (only active canonical/belief memories surface) but satisfies the `NOT EXISTS` guard, preventing the same episode from being re-distilled on every pass.

**LLM failure defers the entire remaining batch.** If `_distill_episode_facts` returns `None` (LLM error), SUMMARIZE aborts the whole pass for that scope rather than issuing N more failing calls. All unprocessed episodes remain unmarked and will retry on the next pass.

**Single-use forget approval.** The `forget()` function uses both a Postgres advisory lock and an `events`-table idempotency check to ensure each approval is consumed exactly once, even under concurrent requests.

**Watermark is the minimum across both projections.** `_read_watermark` returns `min(pgvector_seq, redis_seq)`. If one projection falls behind (e.g. Redis is unavailable), the watermark stalls and events are re-projected next pass (idempotent for embeddings; `UPDATE ... WHERE id = ?`).

**Working-memory hash vs Redis cache.** The `mem:current:{scope}` hash is the working-memory read model (statements only). The `cache:retr:*` and `cache:recall:*` keys are the retrieval caches. `_evict_retr_cache` uses `scan_iter` with a glob pattern; on large key sets this can be slow. The recall cache uses generation versioning instead of key eviction to avoid this scan on ingest.

**`deep` mode recall uses 0 agentic rounds.** Despite its name implying more work, `deep` retrieval mode achieves depth through `ef_search=128`, `k=50`, and `k_hops=2` — not through follow-up queries, which were measured to add latency with no quality improvement (verified 2026-06-16).
