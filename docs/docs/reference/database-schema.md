---
title: Database Schema
description: Complete reference for all 43 PostgreSQL tables in CurlyOS Core, including bi-temporal conventions, HNSW indexes, and the migration system.
---

CurlyOS Core stores long-term memory, the knowledge graph, cognition state, goals, agent execution traces, and configuration in **PostgreSQL 16** with the **pgvector** extension. Hot path state (rate counters, approval queues, session caches) lives in **Redis**. These two stores are strictly separate: Postgres owns every durable fact; Redis holds transient data that can be rebuilt from Postgres.

**Extensions enabled:**

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

**ID conventions.** Every primary key is a `text` column carrying a prefixed ULID, e.g. `mem_<ulid>`, `epi_<ulid>`, `run_<ulid>`. Prefix families: `epi_` episodes, `mem_` memories, `idf_` identity facts, `ent_` entities, `edg_` edges, `goal_` goals, `dec_` decisions, `opp_` opportunities, `run_` agent runs, `act_` actions, `obs_` observations, `tcl_` tool calls, `apv_` approvals, `cap_` capability grants, `bgt_` budget ledger, `pmt_` prompt versions, `out_` outcomes, `les_` lessons, `sjob_` scheduled jobs, `inb_` inbox items, `gpl_` goal plans, `gtk_` goal tasks, `omsg_` orchestrator messages, `art_` artifacts.

**Embedding dimensions.** All vector columns are `vector(1024)` (1024-dimensional dense embeddings from the configured encoder).

---

## Migration system

### How `migrate.py` works

`migrate.py` is the single authoritative path for applying schema changes. It must not be bypassed; `curlyos_setup.py` calls `run_migrations()` internally and there is no second DDL path.

**Algorithm:**

1. Opens a single `psycopg` connection with `autocommit=False`.
2. Creates the `schema_migrations` ledger table (`IF NOT EXISTS`) and commits.
3. Reads all already-applied filenames from the ledger.
4. Warns about ledger entries whose files have been removed from disk.
5. **Gap check** — if any unapplied file sorts before the newest applied file, it raises `RuntimeError`. History must not be rewritten.
6. Iterates `pending` files in sorted order; each file is executed in a transaction and, on success, recorded in the ledger. Any error triggers a rollback and re-raises, halting the run.
7. After all files are applied, it logs the total count of public tables.

**`schema_migrations` ledger:**

```sql
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename   text PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
);
```

Each row records the filename (e.g. `0003_hnsw_reparam.sql`) and when it was committed. The filename is the primary key, so applying the same file twice silently fails at the `INSERT` level (the file is already in `applied`).

**Idempotency.** Migrations 0001 and most later ones use `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, and `CREATE INDEX IF NOT EXISTS`, making them safe to re-run against an existing database. Migration 0003 is an explicit exception — it does `DROP INDEX IF EXISTS` before `CREATE INDEX` to force a parameter rebuild, which is documented in the file header.

### CLI usage

```
# apply all pending migrations (reads CURLYOS_DATABASE_URL)
.venv/bin/python3 migrate.py

# preview pending migrations without touching the database
.venv/bin/python3 migrate.py --dry-run

# override the connection string
.venv/bin/python3 migrate.py --dsn postgresql://user:pass@host:5432/curlyos
```

The `CURLYOS_DATABASE_URL` environment variable is the default DSN. The runner exits with code 1 on any error.

---

## Conventions

### Bi-temporal columns

Several tables carry a **bi-temporal-lite** design: two independent time axes.

| Column | Type | Meaning |
|---|---|---|
| `valid_from` | `timestamptz NOT NULL` | When the fact became true in the world |
| `valid_to` | `timestamptz` | When the fact stopped being true (`NULL` = currently true) |
| `ingested_at` | `timestamptz NOT NULL` | When CurlyOS recorded the row |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Row insert time (same as `ingested_at` for most tables) |

### Soft-invalidation (invalidate-not-delete)

Rows are never hard-deleted from memory-bearing tables. When a fact is superseded, `valid_to` is set to `now()` and an optional `superseded_by` FK points at the replacement row. Queries over "current" state filter `WHERE valid_to IS NULL`.

### Provenance

Every memory-bearing row carries `source_episode_id text REFERENCES episodes(id)` so every derived fact traces back to the raw episode that gave rise to it.

### `epistemic_status`

A text enum carried by memories, entities, assumptions, outcomes, lessons, and others:

- `canonical` — high-confidence, current fact
- `hypothesis` — plausible but unconfirmed
- `provisional` — inferred, awaiting validation
- `validated` / `observed` / `inferred` / `reported` — domain-specific confidence grades
- `seed` / `conjecture` — studio/sketch exploration states

### HNSW index parameters

All vector columns are indexed with HNSW at `m=32, ef_construction=200` (Spike-02 measured recall@10 = 0.963 at these parameters vs. 0.844 at `m=16/ef_construction=64`). Query-time `hnsw.ef_search=128` is set in the retrieval layer, not DDL.

```sql
USING hnsw (embedding vector_cosine_ops)
WITH (m=32, ef_construction=200)
```

### FTS triggers

`memories.search_tsv` and `episodes.search_tsv` are maintained by `BEFORE INSERT OR UPDATE` triggers that call `to_tsvector('english', ...)`. GIN indexes back them for full-text search.

---

## Tables

### Domain: Memory

---

#### `episodes`

Raw ingestion unit — every observation, conversation turn, journal entry, or sensory input arrives as an episode. All derived tables reference episodes for provenance.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | `epi_<ulid>` |
| `scope` | `text NOT NULL` | User/tenant scope |
| `content` | `text NOT NULL` | Raw text content |
| `source_ref` | `text` | External reference, e.g. `brain:`, `jrnl:`, `mind:` prefixes |
| `modality` | `text NOT NULL DEFAULT 'text'` | Input modality |
| `embedding` | `vector(1024)` | Dense semantic embedding |
| `ingested_at` | `timestamptz NOT NULL DEFAULT now()` | Ingestion time |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Row insert time |
| `search_tsv` | `tsvector` | FTS vector; maintained by trigger |

**Indexes:**
- `idx_episodes_hnsw` — HNSW on `embedding` (cosine, m=32, ef_construction=200)
- `idx_episodes_scope_time` — btree on `(scope, created_at)`
- `idx_episodes_tsv` — GIN on `search_tsv`
- `idx_episodes_scope_source_ref` — btree on `(scope, source_ref text_pattern_ops) WHERE source_ref IS NOT NULL` (supports left-anchored `LIKE` prefix scans)

---

#### `memories`

Distilled, bi-temporal facts derived from episodes. The primary long-term memory store retrieved via vector similarity.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | `mem_<ulid>` |
| `scope` | `text NOT NULL` | User/tenant scope |
| `statement` | `text NOT NULL` | Human-readable fact statement |
| `statement_key` | `text NOT NULL` | Deduplication/lookup key |
| `kind` | `text NOT NULL DEFAULT 'fact'` | Memory kind |
| `tier` | `text NOT NULL DEFAULT 'semantic'` | Memory tier |
| `embedding` | `vector(1024)` | Dense semantic embedding |
| `epistemic_status` | `text NOT NULL DEFAULT 'canonical'` | Confidence grade |
| `valid_from` | `timestamptz NOT NULL` | Bi-temporal: world-valid start |
| `valid_to` | `timestamptz` | Bi-temporal: world-valid end (`NULL` = current) |
| `ingested_at` | `timestamptz NOT NULL` | System ingestion time |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Row insert time |
| `source_episode_id` | `text NOT NULL REFERENCES episodes(id)` | Provenance back-reference |
| `superseded_by` | `text REFERENCES memories(id)` | Soft-invalidation chain |
| `search_tsv` | `tsvector` | FTS vector; maintained by trigger |

**Indexes:**
- `idx_memories_hnsw` — HNSW on `embedding` (cosine, m=32, ef_construction=200)
- `idx_memories_scope_current` — btree on `(scope) WHERE valid_to IS NULL`
- `idx_memories_bitemporal` — btree on `(scope, valid_from, valid_to)`
- `idx_memories_epistemic` — btree on `(scope, epistemic_status) WHERE valid_to IS NULL`
- `idx_memories_tsv` — GIN on `search_tsv`

---

#### `identity_facts`

Subject–predicate–object triples that describe the user's stable identity (name, values, roles, relationships). Bi-temporal; soft-invalidation via `superseded_by`.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | `idf_<ulid>` |
| `scope` | `text NOT NULL` | User/tenant scope |
| `predicate` | `text NOT NULL` | e.g. `is_a`, `values`, `works_at` |
| `object` | `text NOT NULL` | The fact value |
| `confidence` | `real NOT NULL` | 0.0–1.0 confidence |
| `epistemic_status` | `text NOT NULL DEFAULT 'canonical'` | Confidence grade |
| `valid_from` | `timestamptz NOT NULL` | Bi-temporal: world-valid start |
| `valid_to` | `timestamptz` | Bi-temporal: world-valid end |
| `ingested_at` | `timestamptz NOT NULL` | System ingestion time |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Row insert time |
| `source_episode_id` | `text NOT NULL REFERENCES episodes(id)` | Provenance |
| `superseded_by` | `text REFERENCES identity_facts(id)` | Soft-invalidation chain |

**Indexes:**
- `idx_idf_scope_predicate_current` — btree on `(scope, predicate) WHERE valid_to IS NULL`
- `idx_idf_bitemporal` — btree on `(scope, predicate, valid_from, valid_to)`

---

#### `events`

Append-only event log for the event-sourcing projection layer. Each row is an immutable domain event with a monotonic `seq`.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | Event ID |
| `type` | `text NOT NULL` | Event type string |
| `subject` | `text` | Subject entity reference |
| `scope` | `text NOT NULL` | User/tenant scope |
| `data` | `jsonb NOT NULL` | Event payload |
| `seq` | `bigserial UNIQUE` | Monotonic sequence number |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Event timestamp |

---

#### `projection_watermarks`

Tracks the highest `seq` processed by each projection per scope, enabling resumable event replay.

| Column | Type | Notes |
|---|---|---|
| `projection` | `text NOT NULL` | Projection name |
| `scope` | `text NOT NULL` | User/tenant scope |
| `last_seq` | `bigint NOT NULL DEFAULT 0` | Highest applied event seq |
| `updated_at` | `timestamptz NOT NULL DEFAULT now()` | Last update time |

**Primary key:** `(projection, scope)`

---

### Domain: Knowledge Graph

---

#### `knowledge_entities`

Nodes in the semantic knowledge graph. Bi-temporal; soft-invalidation via `valid_to`.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | `ent_<ulid>` |
| `scope` | `text NOT NULL` | User/tenant scope |
| `name` | `text NOT NULL` | Entity name |
| `label` | `text NOT NULL DEFAULT 'Entity'` | Node type label (e.g. `Person`, `Project`) |
| `properties` | `jsonb NOT NULL DEFAULT '{}'` | Arbitrary key-value properties |
| `embedding` | `vector(1024)` | Dense semantic embedding |
| `epistemic_status` | `text NOT NULL DEFAULT 'canonical'` | Confidence grade |
| `valid_from` | `timestamptz NOT NULL DEFAULT now()` | Bi-temporal: world-valid start |
| `valid_to` | `timestamptz` | Bi-temporal: world-valid end |
| `ingested_at` | `timestamptz NOT NULL DEFAULT now()` | System ingestion time |
| `source_episode_id` | `text` | Provenance (soft FK to episodes) |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Row insert time |

**Indexes:**
- `idx_ke_hnsw` — HNSW on `embedding` (cosine, m=32, ef_construction=200)
- `idx_ke_scope_label` — btree on `(scope, label) WHERE valid_to IS NULL`
- `idx_ke_name` — btree on `(name) WHERE valid_to IS NULL`

---

#### `knowledge_edges`

Directed, typed relationships between knowledge entities. Bi-temporal.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | `edg_<ulid>` |
| `src_entity_id` | `text NOT NULL REFERENCES knowledge_entities(id)` | Source node |
| `dst_entity_id` | `text NOT NULL REFERENCES knowledge_entities(id)` | Destination node |
| `rel_type` | `text NOT NULL` | Relationship type (e.g. `works_at`, `uses`) |
| `properties` | `jsonb NOT NULL DEFAULT '{}'` | Arbitrary edge metadata |
| `valid_from` | `timestamptz NOT NULL DEFAULT now()` | Bi-temporal: world-valid start |
| `valid_to` | `timestamptz` | Bi-temporal: world-valid end |
| `source_episode_id` | `text` | Provenance (soft FK to episodes) |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Row insert time |

**Indexes:**
- `idx_kedge_src` — btree on `(src_entity_id) WHERE valid_to IS NULL`
- `idx_kedge_dst` — btree on `(dst_entity_id) WHERE valid_to IS NULL`
- `idx_kedge_rel` — btree on `(rel_type) WHERE valid_to IS NULL`

---

### Domain: Identity

Identity facts are stored in `identity_facts` (see Memory domain). The tables below support the goal-level identity alignment surface.

---

### Domain: Cognition

---

#### `reflection_reports`

Periodic reflection summaries: what episodes were scanned, what findings emerged, what goal deltas and identity candidates were identified.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | Report ID |
| `scope` | `text NOT NULL` | User/tenant scope |
| `report_type` | `text NOT NULL DEFAULT 'weekly'` | `weekly`, `monthly`, or `manual` |
| `time_window_start` | `timestamptz NOT NULL` | Start of scanned window |
| `time_window_end` | `timestamptz NOT NULL` | End of scanned window |
| `episodes_scanned` | `integer NOT NULL DEFAULT 0` | Count of episodes reviewed |
| `findings` | `jsonb NOT NULL DEFAULT '[]'` | Structured finding list |
| `goal_deltas` | `jsonb NOT NULL DEFAULT '[]'` | Goal progress changes |
| `identity_candidates` | `jsonb NOT NULL DEFAULT '[]'` | Candidate identity facts |
| `summary` | `text` | Free-form reflection summary |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Report creation time |

---

#### `assumptions`

Tracked beliefs the system operates under. Bi-temporal; soft-invalidation via `valid_to`.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | Assumption ID |
| `scope` | `text NOT NULL` | User/tenant scope |
| `statement` | `text NOT NULL` | The assumption in prose |
| `domain` | `text NOT NULL DEFAULT 'general'` | Epistemic domain |
| `confidence` | `real NOT NULL DEFAULT 0.5` | 0.0–1.0 |
| `epistemic_status` | `text NOT NULL DEFAULT 'hypothesis'` | Confidence grade |
| `valid_from` | `timestamptz NOT NULL DEFAULT now()` | Bi-temporal: world-valid start |
| `valid_to` | `timestamptz` | Bi-temporal: world-valid end |
| `source_episode_id` | `text` | Provenance (soft FK to episodes) |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Row insert time |

**Indexes:**
- `idx_assumptions_scope` — btree on `(scope, domain) WHERE valid_to IS NULL`

---

#### `assumption_edges`

Directed dependency graph between assumptions (rests_on, contradicts, derived_from, audited_by).

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | Edge ID |
| `src_assumption_id` | `text NOT NULL REFERENCES assumptions(id)` | Source assumption |
| `dst_assumption_id` | `text NOT NULL REFERENCES assumptions(id)` | Destination assumption |
| `rel_type` | `text NOT NULL` | `rests_on`, `contradicts`, `derived_from`, `audited_by` |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Row insert time |

---

#### `mental_models`

Versioned world-models the system maintains. Bi-temporal; soft-invalidation via `valid_to`.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | Model ID |
| `scope` | `text NOT NULL` | User/tenant scope |
| `name` | `text NOT NULL` | Model name |
| `domain` | `text NOT NULL DEFAULT 'general'` | Epistemic domain |
| `description` | `text NOT NULL` | Model description |
| `confidence` | `real NOT NULL DEFAULT 0.5` | 0.0–1.0 |
| `version` | `integer NOT NULL DEFAULT 1` | Version counter |
| `valid_from` | `timestamptz NOT NULL DEFAULT now()` | Bi-temporal: world-valid start |
| `valid_to` | `timestamptz` | Bi-temporal: world-valid end |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Row insert time |

---

#### `decision_audits`

Metacognition records linking a decision to its predicted and actual outcomes for quality scoring.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | Audit ID |
| `scope` | `text NOT NULL` | User/tenant scope |
| `decision` | `text NOT NULL` | Decision text |
| `domain` | `text NOT NULL DEFAULT 'general'` | Decision domain |
| `predicted_outcome` | `text` | What was predicted |
| `actual_outcome` | `text` | What happened |
| `quality_score` | `real` | 0.0–1.0 quality score |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Row insert time |

---

#### `principles`

Distilled operating principles derived from reflection. Bi-temporal.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | Principle ID |
| `scope` | `text NOT NULL` | User/tenant scope |
| `statement` | `text NOT NULL` | Principle in prose |
| `domain` | `text NOT NULL DEFAULT 'general'` | Domain |
| `epistemic_status` | `text NOT NULL DEFAULT 'hypothesis'` | Confidence grade |
| `valid_from` | `timestamptz NOT NULL DEFAULT now()` | Bi-temporal: world-valid start |
| `valid_to` | `timestamptz` | Bi-temporal: world-valid end |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Row insert time |

**Indexes:**
- `idx_principles_scope` — btree on `(scope, domain) WHERE valid_to IS NULL`

---

#### `alignment_signals`

Detected gaps between stated values and actual actions (value_action_gap, fulfillment, regret signals). Bi-temporal.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | Signal ID |
| `scope` | `text NOT NULL` | User/tenant scope |
| `signal_type` | `text NOT NULL` | `value_action_gap`, `fulfillment`, `regret` |
| `description` | `text NOT NULL` | Signal description |
| `severity` | `real NOT NULL DEFAULT 0.5` | 0.0–1.0 severity |
| `epistemic_status` | `text NOT NULL DEFAULT 'hypothesis'` | Confidence grade |
| `valid_from` | `timestamptz NOT NULL DEFAULT now()` | Bi-temporal: world-valid start |
| `valid_to` | `timestamptz` | Bi-temporal: world-valid end |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Row insert time |

**Indexes:**
- `idx_aln_scope` — btree on `(scope, signal_type) WHERE valid_to IS NULL`

---

#### `life_chapters`

Narrative chapters segmenting the user's life into periods. Bi-temporal.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | Chapter ID |
| `scope` | `text NOT NULL` | User/tenant scope |
| `title` | `text NOT NULL` | Chapter title |
| `summary` | `text` | Narrative summary |
| `start_date` | `timestamptz NOT NULL` | Chapter start |
| `end_date` | `timestamptz` | Chapter end (`NULL` = ongoing) |
| `epistemic_status` | `text NOT NULL DEFAULT 'hypothesis'` | Confidence grade |
| `valid_from` | `timestamptz NOT NULL DEFAULT now()` | Bi-temporal: world-valid start |
| `valid_to` | `timestamptz` | Bi-temporal: world-valid end |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Row insert time |

**Indexes:**
- `idx_chapters_scope` — btree on `(scope)`

---

#### `themes`

Recurring themes identified across episodes and life chapters. Bi-temporal.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | Theme ID |
| `scope` | `text NOT NULL` | User/tenant scope |
| `name` | `text NOT NULL` | Theme name |
| `description` | `text` | Theme description |
| `frequency` | `integer NOT NULL DEFAULT 1` | Occurrence count |
| `epistemic_status` | `text NOT NULL DEFAULT 'hypothesis'` | Confidence grade |
| `valid_from` | `timestamptz NOT NULL DEFAULT now()` | Bi-temporal: world-valid start |
| `valid_to` | `timestamptz` | Bi-temporal: world-valid end |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Row insert time |

**Indexes:**
- `idx_themes_scope` — btree on `(scope)`

---

#### `theme_chapter_links`

Many-to-many join between themes and life chapters.

| Column | Type | Notes |
|---|---|---|
| `theme_id` | `text NOT NULL REFERENCES themes(id)` | FK to themes |
| `chapter_id` | `text NOT NULL REFERENCES life_chapters(id)` | FK to life_chapters |

**Primary key:** `(theme_id, chapter_id)`

---

### Domain: Goals / Workspace

---

#### `goals`

The Goal Operating System: the user's active intentions across horizons (life, year, quarter, month). Bi-temporal-lite; invalidate-not-delete via `valid_to`.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | `goal_<ulid>` |
| `scope` | `text NOT NULL` | User/tenant scope |
| `parent_id` | `text REFERENCES goals(id)` | Hierarchy: direction → goal → subgoal |
| `title` | `text NOT NULL` | Goal title |
| `description` | `text` | Detailed description |
| `horizon` | `text` | `life`, `year`, `quarter`, `month` |
| `status` | `text NOT NULL DEFAULT 'active'` | `active`, `paused`, `achieved`, `abandoned` |
| `priority` | `int NOT NULL DEFAULT 0` | Relative priority |
| `identity_refs` | `text[] NOT NULL DEFAULT '{}'` | `idf_` IDs this goal serves |
| `project_refs` | `text[] NOT NULL DEFAULT '{}'` | `prj_` IDs executing the goal |
| `success_criteria` | `text` | Measurable criteria for completion |
| `progress` | `real NOT NULL DEFAULT 0` | 0.0–1.0 progress fraction |
| `properties` | `jsonb NOT NULL DEFAULT '{}'` | Extensibility seam (reflection writes `last_reflection` here) |
| `source_episode_id` | `text REFERENCES episodes(id)` | Provenance |
| `valid_from` | `timestamptz NOT NULL DEFAULT now()` | Bi-temporal: world-valid start |
| `valid_to` | `timestamptz` | Bi-temporal: world-valid end |
| `ingested_at` | `timestamptz NOT NULL DEFAULT now()` | System ingestion time |
| `project_id` | `text REFERENCES projects(id)` | Placed project (added in 0013) |

**Indexes:**
- `idx_goals_active` — btree on `(scope, status) WHERE valid_to IS NULL`
- `idx_goals_parent` — btree on `(parent_id) WHERE valid_to IS NULL`
- `idx_goals_project` — btree on `(project_id) WHERE project_id IS NOT NULL`

---

#### `decisions`

Records of significant choices, with structured rationale, prediction, and review lifecycle.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | `dec_<ulid>` |
| `scope` | `text NOT NULL` | User/tenant scope |
| `title` | `text NOT NULL` | Short decision title |
| `context` | `text` | Situation at decision time |
| `options_considered` | `jsonb NOT NULL DEFAULT '[]'` | Alternatives evaluated |
| `chosen` | `text NOT NULL` | The chosen option |
| `rationale` | `text NOT NULL` | Reasoning for the choice |
| `reversibility` | `text` | `reversible`, `costly`, `one_way` |
| `goal_id` | `text REFERENCES goals(id)` | Linked goal |
| `review_at` | `timestamptz` | When to revisit the outcome |
| `outcome` | `text` | Human-readable outcome summary (at review) |
| `audit_id` | `text` | `dau_` link to metacognition review |
| `decided_at` | `timestamptz NOT NULL DEFAULT now()` | Decision timestamp |
| `reviewed_at` | `timestamptz` | When the decision was reviewed |
| `source_episode_id` | `text REFERENCES episodes(id)` | Provenance |
| `properties` | `jsonb NOT NULL DEFAULT '{}'` | Extensibility seam (council-mode results, etc.) — added in 0005 |
| `predicted_outcome` | `text` | Falsifiable prediction made at decision time — added in 0007 |
| `prediction_confidence` | `real` | 0.0–1.0 confidence in the prediction — added in 0007 |
| `outcome_id` | `text` | FK to `outcomes.id` (set at review) — added in 0007 |

**Indexes:**
- `idx_decisions_review` — btree on `(review_at) WHERE outcome IS NULL AND review_at IS NOT NULL`

---

#### `opportunities`

Detected possibilities for goal creation, scored by novelty, value, and feasibility.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | `opp_<ulid>` |
| `scope` | `text NOT NULL` | User/tenant scope |
| `title` | `text NOT NULL` | Opportunity title |
| `description` | `text NOT NULL` | Opportunity description |
| `source` | `text NOT NULL` | `discovery_scan`, `manual`, `reflection` |
| `evidence_refs` | `text[] NOT NULL DEFAULT '{}'` | `mem_`/`epi_`/`ent_` IDs that triggered it |
| `novelty` | `real` | 0.0–1.0 novelty score |
| `value_est` | `real` | 0.0–1.0 estimated value |
| `feasibility` | `real` | 0.0–1.0 feasibility score |
| `score` | `real` | Combined score (set by scorer) |
| `status` | `text NOT NULL DEFAULT 'detected'` | `detected`, `scored`, `accepted`, `rejected`, `expired` |
| `resolution` | `text` | `goal_id`/`prj_id` if accepted; reason if rejected |
| `detected_at` | `timestamptz NOT NULL DEFAULT now()` | Detection timestamp |
| `resolved_at` | `timestamptz` | Resolution timestamp |

**Indexes:**
- `idx_opportunities_status` — btree on `(status, detected_at DESC)`

---

#### `outcomes`

Structured records of what actually happened after a decision, scored against the original prediction.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | `out_<ulid>` |
| `scope` | `text NOT NULL` | User/tenant scope |
| `decision_id` | `text NOT NULL REFERENCES decisions(id)` | Decision reviewed |
| `goal_id` | `text REFERENCES goals(id)` | Linked goal (denormalized for scoring) |
| `summary` | `text NOT NULL` | What actually happened, in prose |
| `valence` | `text NOT NULL` | `success`, `partial`, `failure`, `mixed`, `too_early` |
| `matched_prediction` | `boolean` | Did actual match prediction? |
| `surprise` | `real` | Brier score: `(prediction_confidence - hit)^2` — 0.0–1.0 |
| `metrics` | `jsonb NOT NULL DEFAULT '{}'` | Structured measures (revenue, latency, etc.) |
| `evidence_refs` | `text[] NOT NULL DEFAULT '{}'` | `epi_`/`mem_`/`ent_` IDs showing the outcome |
| `embedding` | `vector(1024)` | Semantic embedding for similarity retrieval |
| `epistemic_status` | `text NOT NULL DEFAULT 'observed'` | `observed`, `inferred`, `reported` |
| `source_episode_id` | `text REFERENCES episodes(id)` | Provenance |
| `observed_at` | `timestamptz NOT NULL DEFAULT now()` | Observation timestamp |
| `valid_from` | `timestamptz NOT NULL DEFAULT now()` | Bi-temporal: world-valid start |
| `valid_to` | `timestamptz` | Bi-temporal: world-valid end |
| `ingested_at` | `timestamptz NOT NULL DEFAULT now()` | System ingestion time |

**Indexes:**
- `idx_outcomes_hnsw` — HNSW on `embedding` (cosine, m=32, ef_construction=200)
- `idx_outcomes_decision` — btree on `(decision_id) WHERE valid_to IS NULL`
- `idx_outcomes_goal` — btree on `(goal_id) WHERE valid_to IS NULL`

---

#### `lessons`

Reusable, embedded lessons distilled from outcomes. Retrieved by semantic similarity for the Executive's future decision context.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | `les_<ulid>` |
| `scope` | `text NOT NULL` | User/tenant scope |
| `statement` | `text NOT NULL` | "When X under Y, prefer Z because…" |
| `applies_when` | `text` | Applicability prose |
| `conditions` | `jsonb NOT NULL DEFAULT '{}'` | Structured match criteria (domain, reversibility, etc.) |
| `confidence` | `real NOT NULL DEFAULT 0.5` | 0.0–1.0 |
| `support_count` | `int NOT NULL DEFAULT 1` | Number of outcomes supporting the lesson |
| `contradiction_count` | `int NOT NULL DEFAULT 0` | Number of contradicting outcomes |
| `derived_from_outcomes` | `text[] NOT NULL DEFAULT '{}'` | `out_` IDs (provenance) |
| `applies_to_entities` | `text[] NOT NULL DEFAULT '{}'` | `ent_` IDs (KG link) |
| `updates_model` | `text REFERENCES mental_models(id)` | Mental model this lesson revises |
| `status` | `text NOT NULL DEFAULT 'provisional'` | `provisional`, `validated`, `retired`, `contradicted` |
| `embedding` | `vector(1024)` | Semantic embedding for retrieval |
| `epistemic_status` | `text NOT NULL DEFAULT 'provisional'` | Confidence grade |
| `properties` | `jsonb NOT NULL DEFAULT '{}'` | Extensibility seam (e.g. `last_reinforced_at`) |
| `source_episode_id` | `text REFERENCES episodes(id)` | Provenance |
| `valid_from` | `timestamptz NOT NULL DEFAULT now()` | Bi-temporal: world-valid start |
| `valid_to` | `timestamptz` | Bi-temporal: world-valid end |
| `ingested_at` | `timestamptz NOT NULL DEFAULT now()` | System ingestion time |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Row insert time |

**Indexes:**
- `idx_lessons_hnsw` — HNSW on `embedding` (cosine, m=32, ef_construction=200)
- `idx_lessons_active` — btree on `(scope, status) WHERE valid_to IS NULL`
- `idx_lessons_model` — btree on `(updates_model) WHERE valid_to IS NULL`

---

#### `workspaces`

Top-level life-area containers. A workspace organizes projects and goals under a shared name and physical directory.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | Workspace ID |
| `scope` | `text NOT NULL` | User/tenant scope |
| `name` | `text NOT NULL` | Display name |
| `kind` | `text NOT NULL DEFAULT 'project'` | Workspace kind |
| `properties` | `jsonb NOT NULL DEFAULT '{}'` | Extensibility seam |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Row insert time |
| `updated_at` | `timestamptz NOT NULL DEFAULT now()` | Last update time |
| `slug` | `text` | URL-safe identifier (added in 0013) |
| `path` | `text` | Physical directory path (added in 0013) |
| `summary` | `text` | One-liner description (added in 0013) |
| `status` | `text NOT NULL DEFAULT 'active'` | `active`, `archived` (added in 0013) |

**Indexes:**
- `idx_workspaces_scope` — btree on `(scope)`
- `uq_workspaces_scope_slug` — unique on `(scope, slug) WHERE slug IS NOT NULL`

---

#### `projects`

High-level goal containers ("north star" projects) within a workspace. Each project gets a real directory on disk at `~/curlyos/workspaces/<ws>/<prj>/{studio,src}`.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | `prj_<ulid>` |
| `workspace_id` | `text NOT NULL REFERENCES workspaces(id)` | Parent workspace |
| `name` | `text NOT NULL` | Display name |
| `status` | `text NOT NULL DEFAULT 'active'` | `active` or lifecycle variant |
| `properties` | `jsonb NOT NULL DEFAULT '{}'` | Extensibility seam |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Row insert time |
| `scope` | `text` | User/tenant scope (added in 0013) |
| `slug` | `text` | URL-safe identifier (added in 0013) |
| `path` | `text` | Physical directory path (added in 0013) |
| `summary` | `text` | One-liner description (added in 0013) |
| `north_star_goal_id` | `text REFERENCES goals(id)` | The defining goal (added in 0013) |
| `updated_at` | `timestamptz NOT NULL DEFAULT now()` | Last update time (added in 0013) |

**Indexes:**
- `idx_projects_workspace` — btree on `(workspace_id)`
- `idx_projects_status` — btree on `(status)`
- `uq_projects_ws_slug` — unique on `(workspace_id, slug) WHERE slug IS NOT NULL`

---

#### `tasks`

Work items within a project. Simple task management model (distinct from `goal_tasks` which are orchestrator-managed).

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | Task ID |
| `project_id` | `text NOT NULL REFERENCES projects(id)` | Parent project |
| `title` | `text NOT NULL` | Task title |
| `priority` | `text NOT NULL DEFAULT 'medium'` | `high`, `medium`, `low` |
| `status` | `text NOT NULL DEFAULT 'pending'` | `pending`, `active`, `done`, etc. |
| `depends_on` | `jsonb NOT NULL DEFAULT '[]'` | Array of dependency task IDs |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Row insert time |
| `completed_at` | `timestamptz` | Completion timestamp |

**Indexes:**
- `idx_tasks_project` — btree on `(project_id)`
- `idx_tasks_status` — btree on `(status)`
- `idx_tasks_priority` — btree on `(priority)`

---

#### `artifacts`

Every tangible deliverable an agent produces toward a goal: files, documents, PDFs, images, code, deployments, or links.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | `art_<ulid>` |
| `scope` | `text NOT NULL` | User/tenant scope |
| `project_id` | `text REFERENCES projects(id)` | Parent project |
| `goal_id` | `text REFERENCES goals(id)` | Linked goal |
| `run_id` | `text` | `agent_runs.id` that produced it (soft FK) |
| `task_id` | `text` | `goal_tasks.id` that produced it (soft FK) |
| `kind` | `text NOT NULL DEFAULT 'file'` | `file`, `doc`, `pdf`, `image`, `code`, `deploy`, `link`, `data` |
| `title` | `text NOT NULL` | Artifact title |
| `path` | `text` | Absolute filesystem path (file-backed kinds) |
| `url` | `text` | External/deploy/link URL |
| `bytes` | `bigint` | File size in bytes |
| `status` | `text NOT NULL DEFAULT 'created'` | `created`, `updated`, `published`, `archived` |
| `summary` | `text` | What it is / why it matters |
| `meta` | `jsonb NOT NULL DEFAULT '{}'` | Extensibility seam |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Row insert time |
| `updated_at` | `timestamptz NOT NULL DEFAULT now()` | Last update time |

**Indexes:**
- `idx_artifacts_project` — btree on `(project_id, created_at DESC)`
- `idx_artifacts_goal` — btree on `(goal_id)`
- `idx_artifacts_run` — btree on `(run_id) WHERE run_id IS NOT NULL`

---

### Domain: Orchestration / Agent Spine

---

#### `agent_runs`

Central execution record for every agent invocation — scheduled workflows, Executive goal runs, interactive sessions.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | `run_<ulid>` |
| `agent` | `text NOT NULL` | `Executive` or `workflow:<name>` |
| `scope` | `text NOT NULL` | User/tenant scope |
| `task` | `text` | Natural-language task description |
| `status` | `text NOT NULL DEFAULT 'running'` | `running`, `parked`, `completed`, `failed`, `cancelled` |
| `autonomy_level` | `text NOT NULL DEFAULT 'confirm_each'` | `suggest_only`, `confirm_each`, `bounded_auto`, `full_auto` (expanded in 0011) |
| `parent_run_id` | `text REFERENCES agent_runs(id)` | Parent run (sub-agents) |
| `source_episode_id` | `text REFERENCES episodes(id)` | Linked episode |
| `result` | `jsonb` | Synthesized outcome |
| `error` | `text` | Error text if failed |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Run start time |
| `finished_at` | `timestamptz` | Run end time |
| `goal_id` | `text` | Linked goal (added in 0009; soft FK to goals.id) |

**Indexes:**
- `idx_agent_runs_status` — btree on `(status)`
- `idx_agent_runs_created` — btree on `(created_at DESC)`
- `agent_runs_goal_idx` — btree on `(goal_id) WHERE goal_id IS NOT NULL`

---

#### `actions`

Individual actions taken within an agent run (tool invocations, memory writes, etc.).

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | `act_<ulid>` |
| `run_id` | `text NOT NULL REFERENCES agent_runs(id)` | Parent run |
| `kind` | `text NOT NULL` | Action class |
| `payload` | `jsonb NOT NULL` | Action payload |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Action timestamp |

**Indexes:**
- `idx_actions_run` — btree on `(run_id)`

---

#### `observations`

Tool call results: the environment's response to a specific action (1:1 with actions).

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | `obs_<ulid>` |
| `action_id` | `text NOT NULL UNIQUE REFERENCES actions(id)` | Linked action |
| `result` | `jsonb NOT NULL` | Tool result payload |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Observation timestamp |

---

#### `tool_calls`

Hash-chained audit log of every tool invocation. `entry_hash = H(prev_hash || canonical_row)` forms a tamper-evident chain.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | `tcl_<ulid>` |
| `action_id` | `text REFERENCES actions(id)` | Linked action |
| `tool` | `text NOT NULL` | Tool name |
| `args` | `jsonb NOT NULL` | Tool arguments |
| `result_hash` | `bytea` | Hash of the result |
| `prev_hash` | `bytea` | Hash of the previous entry in chain |
| `entry_hash` | `bytea NOT NULL` | `H(prev_hash || canonical_row)` |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Invocation timestamp |

**Indexes:**
- `idx_toolcalls_action` — btree on `(action_id)`

---

#### `approvals`

Human-in-the-loop approval queue. Agent-originated approvals must carry a `run_id`; human-originated approvals (e.g. webapp `forget(hard)`) may have `run_id = NULL`.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | `apv_<ulid>` |
| `run_id` | `text REFERENCES agent_runs(id)` | Linked run (NULL for human-originated) |
| `origin` | `text NOT NULL DEFAULT 'agent'` | `agent` or `human` |
| `scope` | `text NOT NULL` | User/tenant scope |
| `action_class` | `text NOT NULL` | Action class being approved |
| `payload` | `jsonb` | What is being approved (display + audit) |
| `state` | `text NOT NULL DEFAULT 'pending'` | `pending`, `granted`, `denied`, `expired` |
| `expires_at` | `timestamptz NOT NULL` | Approval expiry |
| `decided_at` | `timestamptz` | When a decision was made |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Creation timestamp |

**Check constraint:** agent-originated approvals (`origin = 'agent'`) must carry a non-NULL `run_id`.

**Indexes:**
- `idx_approvals_pending` — btree on `(state) WHERE state = 'pending'`

---

#### `capability_grants`

Deny-by-default, time-boxed grants that authorize specific tools within a run's scope.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | `cap_<ulid>` |
| `run_id` | `text NOT NULL REFERENCES agent_runs(id)` | Authorized run |
| `tool` | `text NOT NULL` | Tool name (e.g. `read`, `memory_write`) |
| `scope` | `text NOT NULL` | Scope of the grant |
| `expires_at` | `timestamptz NOT NULL` | Grant expiry (time-boxed) |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Grant creation time |

**Indexes:**
- `idx_capgrants_run` — btree on `(run_id)`

---

#### `budget_ledger`

Daily reconciliation of Redis counters for token, tool, spend, and time budgets. Unique per `(scope, day)`.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | `bgt_<ulid>` |
| `scope` | `text NOT NULL` | `per_run`, `per_agent_day`, `per_user_day` |
| `dims` | `jsonb NOT NULL` | Dimension names: `{tokens, tool_actions, usd_spend, wall_clock_seconds}` |
| `consumed` | `jsonb NOT NULL` | Consumed amounts per dimension |
| `"limit"` | `jsonb NOT NULL` | Limit values per dimension |
| `day` | `date NOT NULL` | Budget day |

**Unique constraint:** `(scope, day)`

---

### Domain: Decision Loop

---

#### `goal_plans`

A decomposition of a goal into executable tasks. A goal may be re-planned; the latest non-abandoned plan is current.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | `gpl_<ulid>` |
| `scope` | `text NOT NULL` | User/tenant scope |
| `goal_id` | `text NOT NULL REFERENCES goals(id)` | Goal being executed |
| `status` | `text NOT NULL DEFAULT 'proposed'` | `proposed`, `approved`, `executing`, `done`, `abandoned` |
| `rationale` | `text NOT NULL DEFAULT ''` | Orchestrator's reasoning for the breakdown |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Plan creation time |
| `updated_at` | `timestamptz NOT NULL DEFAULT now()` | Last update time |

**Indexes:**
- `goal_plans_goal_idx` — btree on `(goal_id, created_at DESC)`

---

#### `goal_tasks`

One executable task within a goal plan. Dispatched to a worker (Executive) run. Includes verify-and-iterate columns for the feedback loop.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | `gtk_<ulid>` |
| `scope` | `text NOT NULL` | User/tenant scope |
| `plan_id` | `text NOT NULL REFERENCES goal_plans(id) ON DELETE CASCADE` | Parent plan |
| `goal_id` | `text NOT NULL REFERENCES goals(id)` | Linked goal (denormalized) |
| `seq` | `int NOT NULL DEFAULT 0` | Execution order within the plan |
| `title` | `text NOT NULL` | Short label |
| `task` | `text NOT NULL` | Natural-language instruction for the worker |
| `why` | `text NOT NULL DEFAULT ''` | How this task advances the goal |
| `status` | `text NOT NULL DEFAULT 'pending'` | `pending`, `dispatched`, `running`, `parked`, `verifying`, `completed`, `failed`, `skipped` |
| `run_id` | `text` | `agent_runs.id` of the worker (soft FK) |
| `result_summary` | `text` | Worker's synthesized output |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Row insert time |
| `updated_at` | `timestamptz NOT NULL DEFAULT now()` | Last update time |
| `attempt` | `int NOT NULL DEFAULT 0` | Re-dispatch attempt counter (added in 0012) |
| `max_attempts` | `int NOT NULL DEFAULT 2` | Retry ceiling (added in 0012) |
| `verify` | `text` | Success criteria for the verifier (added in 0012) |
| `verdict` | `jsonb` | Latest verification result: `{passed, critique, evidence, at}` (added in 0012) |

**Indexes:**
- `goal_tasks_plan_idx` — btree on `(plan_id, seq)`
- `goal_tasks_goal_idx` — btree on `(goal_id)`
- `goal_tasks_run_idx` — btree on `(run_id) WHERE run_id IS NOT NULL`

---

#### `orchestrator_messages`

Conversation transcript between the user and the orchestrator (the "steer the plan" window). Scoped to a goal, a project, or global.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | `omsg_<ulid>` |
| `scope` | `text NOT NULL` | User/tenant scope |
| `goal_id` | `text REFERENCES goals(id)` | Linked goal (nullable = global) |
| `role` | `text NOT NULL` | `user` or `orchestrator` |
| `content` | `text NOT NULL` | Message text |
| `meta` | `jsonb NOT NULL DEFAULT '{}'` | Actions taken, refs (extensibility seam) |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Message timestamp |
| `project_id` | `text REFERENCES projects(id)` | Linked project (added in 0014) |

**Indexes:**
- `orchestrator_messages_idx` — btree on `(scope, goal_id, created_at)`
- `idx_omsg_project` — btree on `(project_id, created_at) WHERE project_id IS NOT NULL`

---

### Domain: Scheduled Jobs

---

#### `scheduled_jobs`

User-defined autonomous jobs described in natural language, run on a cadence, fired through the Executive agent.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | `sjob_<ulid>` |
| `scope` | `text NOT NULL` | User/tenant scope |
| `name` | `text NOT NULL` | Display name (unique per scope) |
| `task` | `text NOT NULL` | Natural-language task description |
| `cadence_type` | `text NOT NULL` | `every`, `daily_at`, `weekly_at`, `monthly_at` |
| `cadence_json` | `jsonb NOT NULL DEFAULT '{}'` | Cadence params: `{"minutes":60}`, `{"hhmm":"09:00"}`, `{"weekdays":[0,2],"hhmm":"09:00"}`, `{"day":1,"hhmm":"09:00"}` |
| `delivery` | `text NOT NULL DEFAULT 'inbox'` | Delivery method (currently only `inbox`) |
| `enabled` | `boolean NOT NULL DEFAULT true` | Whether the job fires |
| `last_fired` | `timestamptz` | Timestamp of last firing |
| `last_status` | `text NOT NULL DEFAULT 'never'` | `never`, `running`, `completed`, `failed`, `parked`, `skipped` |
| `last_run_id` | `text` | `agent_runs.id` of most recent firing (soft FK) |
| `last_error` | `text` | Error text from last firing |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Row insert time |
| `updated_at` | `timestamptz NOT NULL DEFAULT now()` | Last update time |

**Unique constraint:** `(scope, name)`

**Indexes:**
- `scheduled_jobs_scope_idx` — btree on `(scope)`
- `scheduled_jobs_enabled_idx` — btree on `(scope, enabled)`

---

#### `inbox_items`

Delivery inbox for scheduled job outputs. Each completed job firing drops one item here. Deleting a job preserves past outputs (`ON DELETE SET NULL` on `job_id`).

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | `inb_<ulid>` |
| `scope` | `text NOT NULL` | User/tenant scope |
| `job_id` | `text REFERENCES scheduled_jobs(id) ON DELETE SET NULL` | Source job (nullable after job deletion) |
| `run_id` | `text` | `agent_runs.id` (soft FK; runs may be pruned) |
| `title` | `text NOT NULL` | Job name + timestamp |
| `body` | `text NOT NULL` | Agent's synthesized output |
| `meta` | `jsonb NOT NULL DEFAULT '{}'` | `{status, steps, parked, …}` extensibility seam |
| `read_at` | `timestamptz` | NULL = unread |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Item creation time |

**Indexes:**
- `inbox_items_scope_created_idx` — btree on `(scope, created_at DESC)`
- `inbox_items_unread_idx` — btree on `(scope) WHERE read_at IS NULL`
- `inbox_items_job_idx` — btree on `(job_id)`

---

### Domain: Settings

---

#### `app_settings`

Runtime key/value store for global toggles and settings. First use: agent bypass mode (`full_auto` without parking for approval).

| Column | Type | Notes |
|---|---|---|
| `key` | `text PRIMARY KEY` | Setting key |
| `value` | `jsonb NOT NULL` | Setting value (any JSON type) |
| `updated_at` | `timestamptz NOT NULL DEFAULT now()` | Last update time |

---

### Domain: Safety

---

#### `prompt_versions`

Versioned LLM prompts under the self-modify dual gate: proposed candidates must pass an eval run (gate 1) and a `self_modify` approval (gate 2) before activation.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | `pmt_<ulid>` |
| `scope` | `text NOT NULL` | User/tenant scope |
| `name` | `text NOT NULL` | Prompt name (e.g. `executive.plan`) |
| `version` | `int NOT NULL` | Version counter |
| `content` | `text NOT NULL` | Full prompt text |
| `status` | `text NOT NULL DEFAULT 'candidate'` | `candidate`, `active`, `held`, `retired` |
| `eval_run_id` | `text` | `evr_` link (gate 1: eval pass) |
| `approval_id` | `text` | `apv_` link (gate 2: self_modify approval) |
| `proposed_by` | `text NOT NULL DEFAULT 'manual'` | `manual` or `meta_cognition` |
| `notes` | `text` | Free-form notes |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Row insert time |
| `activated_at` | `timestamptz` | When the prompt became active |

**Unique constraint:** `(name, version)`

**Indexes:**
- `idx_prompt_versions_active` — btree on `(name) WHERE status = 'active'`

---

### Domain: Studio / Simulation / Evaluation

---

#### `studios`

Creative sketch workspaces. May be linked to a project (added in 0013).

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | Studio ID |
| `scope` | `text NOT NULL` | User/tenant scope |
| `title` | `text NOT NULL` | Studio title |
| `status` | `text NOT NULL DEFAULT 'active'` | Lifecycle status |
| `properties` | `jsonb NOT NULL DEFAULT '{}'` | Extensibility seam |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Row insert time |
| `updated_at` | `timestamptz NOT NULL DEFAULT now()` | Last update time |
| `project_id` | `text REFERENCES projects(id)` | Linked project (added in 0013) |

**Indexes:**
- `idx_studios_scope` — btree on `(scope)`
- `idx_studios_status` — btree on `(status)`

---

#### `studio_sketches`

Individual sketch nodes within a studio. Status is constrained to exploration stages only.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | Sketch ID |
| `studio_id` | `text NOT NULL` | Parent studio (soft FK) |
| `content` | `text NOT NULL` | Sketch content |
| `kind` | `text NOT NULL DEFAULT 'text'` | Content kind |
| `epistemic_status` | `text NOT NULL DEFAULT 'seed'` | `seed`, `conjecture`, `hypothesis` |
| `properties` | `jsonb NOT NULL DEFAULT '{}'` | Extensibility seam |
| `embedding` | `vector(1024)` | Semantic embedding |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Row insert time |
| `updated_at` | `timestamptz NOT NULL DEFAULT now()` | Last update time |

**Check:** `epistemic_status IN ('seed', 'conjecture', 'hypothesis')`

**Indexes:**
- `idx_studio_sketches_studio` — btree on `(studio_id)`
- `idx_studio_sketches_epistemic` — btree on `(epistemic_status)`

---

#### `studio_links`

Directed relationships between studio sketches.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | Link ID |
| `src_sketch_id` | `text NOT NULL` | Source sketch (soft FK) |
| `dst_sketch_id` | `text NOT NULL` | Destination sketch (soft FK) |
| `rel_type` | `text NOT NULL` | Relationship type |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Row insert time |

**Indexes:**
- `idx_studio_links_src` — btree on `(src_sketch_id)`
- `idx_studio_links_dst` — btree on `(dst_sketch_id)`

---

#### `simulation_runs`

Counterfactual simulation runs. `epistemic_status = 'possible_world'` marks these as hypothetical.

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | Simulation run ID |
| `scope` | `text NOT NULL` | User/tenant scope |
| `question` | `text NOT NULL` | The "what if" question |
| `world_model_id` | `text` | Linked mental model |
| `status` | `text NOT NULL DEFAULT 'created'` | Lifecycle status |
| `epistemic_status` | `text NOT NULL DEFAULT 'possible_world'` | Always `possible_world` |
| `outcome_distribution` | `jsonb NOT NULL DEFAULT '{}'` | Probability distribution over outcomes |
| `parameters` | `jsonb NOT NULL DEFAULT '{}'` | Simulation parameters |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Row insert time |
| `completed_at` | `timestamptz` | Completion timestamp |

**Indexes:**
- `idx_sim_runs_scope` — btree on `(scope)`
- `idx_sim_runs_status` — btree on `(status)`

---

#### `simulation_scenarios`

Individual scenario branches within a simulation run (one run, many scenarios).

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | Scenario ID |
| `run_id` | `text NOT NULL` | Parent simulation run (soft FK) |
| `description` | `text` | Scenario description |
| `assumptions` | `jsonb NOT NULL DEFAULT '[]'` | Assumptions active in this branch |
| `probability` | `real` | Scenario probability |
| `outcome` | `text` | Scenario outcome |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Row insert time |

**Indexes:**
- `idx_sim_scenarios_run` — btree on `(run_id)`

---

#### `golden_datasets`

Immutable reference datasets used for deterministic eval scoring (gate 1 of the self-modify dual gate).

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | Dataset ID |
| `name` | `text NOT NULL` | Dataset name |
| `content_hash` | `text NOT NULL` | Content integrity hash |
| `data` | `jsonb NOT NULL` | Dataset content |
| `metadata` | `jsonb NOT NULL DEFAULT '{}'` | Metadata |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Row insert time |

**Indexes:**
- `idx_golden_datasets_name` — btree on `(name)`
- `idx_golden_datasets_hash` — btree on `(content_hash)`

---

#### `evaluation_runs`

Records of evaluation runs against golden datasets (the eval gate for prompt promotion).

| Column | Type | Notes |
|---|---|---|
| `id` | `text PRIMARY KEY` | `evr_<ulid>` |
| `candidate_ref` | `text NOT NULL` | Reference to the candidate being evaluated |
| `dataset_ids` | `jsonb NOT NULL` | Array of golden dataset IDs |
| `scorers` | `jsonb NOT NULL` | Scorer configuration |
| `pass_rate` | `real NOT NULL` | Fraction passing |
| `decision` | `text NOT NULL` | `pass` or `fail` |
| `created_at` | `timestamptz NOT NULL DEFAULT now()` | Row insert time |

---

### Domain: Feedback

The feedback loop tables (`outcomes`, `lessons`) are documented under Goals / Workspace above, as they are tightly coupled to `decisions` and `goals`. The `schema_migrations` ledger itself is documented in the Migration system section.

---

## Migration history

| File | What it added |
|---|---|
| `0001_baseline.sql` | Full baseline schema: `episodes`, `memories`, `identity_facts`, `events`, `projection_watermarks`, `studios`, `studio_sketches`, `studio_links`, `simulation_runs`, `simulation_scenarios`, `golden_datasets`, `workspaces`, `projects`, `tasks`, `evaluation_runs`, `knowledge_entities`, `knowledge_edges`, `reflection_reports`, `assumptions`, `assumption_edges`, `mental_models`, `decision_audits`, `principles`, `alignment_signals`, `life_chapters`, `themes`, `theme_chapter_links`; FTS triggers + GIN indexes on `memories` and `episodes` |
| `0002_agent_spine.sql` | Agent execution + safety tables: `agent_runs`, `actions`, `observations`, `tool_calls`, `approvals`, `capability_grants`, `budget_ledger` |
| `0003_hnsw_reparam.sql` | Rebuilt HNSW indexes on `episodes`, `memories`, `knowledge_entities` at m=32/ef_construction=200 to meet recall@10 ≥ 0.95 gate |
| `0004_goal_os.sql` | Goal Operating System: `goals`, `decisions`, `opportunities` |
| `0005_decision_properties.sql` | Added `decisions.properties jsonb` extensibility seam |
| `0006_evolution.sql` | Versioned prompt management: `prompt_versions` with self-modify dual gate |
| `0007_decision_loop.sql` | Decision → outcome → lesson feedback loop: `outcomes`, `lessons`; added `predicted_outcome`, `prediction_confidence`, `outcome_id` columns to `decisions` |
| `0008_scheduled_jobs.sql` | User-defined scheduled jobs: `scheduled_jobs`, `inbox_items` |
| `0009_goal_execution.sql` | Goal execution orchestrator: `goal_plans`, `goal_tasks`, `orchestrator_messages`; added `agent_runs.goal_id` |
| `0010_app_settings.sql` | Runtime key/value settings store: `app_settings` |
| `0011_autonomy_full_set.sql` | Expanded `agent_runs.autonomy_level` CHECK to full ladder: `suggest_only`, `confirm_each`, `bounded_auto`, `full_auto` |
| `0012_feedback_loop.sql` | Verify-and-iterate on goal tasks: added `attempt`, `max_attempts`, `verify`, `verdict` to `goal_tasks`; added `verifying` to the status enum |
| `0013_hierarchy_artifacts.sql` | Workspace/project hierarchy on disk + artifact tracking: added `slug`, `path`, `summary`, `status` to `workspaces`; added `scope`, `slug`, `path`, `summary`, `north_star_goal_id`, `updated_at` to `projects`; added `project_id` to `goals` and `studios`; created `artifacts` table |
| `0014_orchestrator_project_chat.sql` | Added `orchestrator_messages.project_id` for project-scoped conversation threads |
| `0015_episodes_source_ref_index.sql` | Added `idx_episodes_scope_source_ref` btree index with `text_pattern_ops` on `(scope, source_ref)` for left-anchored `LIKE` prefix scans |

---

## Entity-relationship overview

The diagram below covers the ~15 most structurally important tables. For brevity, `scope`, timestamps, and `properties` columns are omitted.

```mermaid
erDiagram
    episodes {
        text id PK
        text content
        vector_1024 embedding
        text source_ref
    }
    memories {
        text id PK
        text statement
        vector_1024 embedding
        text source_episode_id FK
        text superseded_by FK
        timestamptz valid_to
    }
    identity_facts {
        text id PK
        text predicate
        text object
        text source_episode_id FK
        timestamptz valid_to
    }
    knowledge_entities {
        text id PK
        text name
        text label
        vector_1024 embedding
        timestamptz valid_to
    }
    knowledge_edges {
        text id PK
        text src_entity_id FK
        text dst_entity_id FK
        text rel_type
    }
    goals {
        text id PK
        text title
        text status
        text parent_id FK
        text project_id FK
        real progress
    }
    decisions {
        text id PK
        text title
        text goal_id FK
        text outcome_id FK
    }
    outcomes {
        text id PK
        text decision_id FK
        text goal_id FK
        text valence
        vector_1024 embedding
    }
    lessons {
        text id PK
        text statement
        vector_1024 embedding
        text updates_model FK
    }
    agent_runs {
        text id PK
        text agent
        text status
        text goal_id
        text parent_run_id FK
    }
    goal_plans {
        text id PK
        text goal_id FK
        text status
    }
    goal_tasks {
        text id PK
        text plan_id FK
        text goal_id FK
        text run_id
        text status
    }
    projects {
        text id PK
        text workspace_id FK
        text north_star_goal_id FK
    }
    workspaces {
        text id PK
        text name
        text slug
    }
    artifacts {
        text id PK
        text project_id FK
        text goal_id FK
        text run_id
        text kind
    }

    episodes ||--o{ memories : "source_episode_id"
    episodes ||--o{ identity_facts : "source_episode_id"
    memories ||--o| memories : "superseded_by"
    knowledge_entities ||--o{ knowledge_edges : "src_entity_id"
    knowledge_entities ||--o{ knowledge_edges : "dst_entity_id"
    goals ||--o| goals : "parent_id"
    goals ||--o{ goal_plans : "goal_id"
    goals ||--o{ goal_tasks : "goal_id"
    goals ||--o{ decisions : "goal_id"
    goal_plans ||--o{ goal_tasks : "plan_id"
    goal_tasks ||--o| agent_runs : "run_id"
    decisions ||--o{ outcomes : "decision_id"
    outcomes ||--o{ lessons : "derived_from_outcomes"
    workspaces ||--o{ projects : "workspace_id"
    projects ||--o| goals : "north_star_goal_id"
    goals }o--|| projects : "project_id"
    projects ||--o{ artifacts : "project_id"
    goals ||--o{ artifacts : "goal_id"
    agent_runs ||--o{ artifacts : "run_id"
```
