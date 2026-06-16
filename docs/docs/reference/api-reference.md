---
title: REST API Reference
description: Complete reference for every endpoint exposed by the CurlyOS Core FastAPI service (port 8643).
---

**Base URL:** `http://127.0.0.1:8643`

**Authentication:** None. The service is localhost-only; origin-based CORS is
the only browser gate (allowed origins: `localhost:3000`, `localhost:3100`,
`127.0.0.1:3000`, `127.0.0.1:3100`, `https://os.curlybrackets.art`).

**Content type:** `application/json` for all request and response bodies.

**Error conventions:** Non-2xx responses return `{"detail": "<message>"}` (FastAPI
default) or `{"error": "<message>"}` from cognition/recall endpoints that swallow
exceptions rather than raising. 404 = not found; 409 = conflict (already
invalidated, parked run, etc.); 503 = external dependency unavailable.

---

## Health and Stats

### GET `/api/health`

Returns connectivity status for Postgres, Redis, and the bge-m3 embedder.

**Query params:** none

**Response:**

```json
{
  "timestamp": "ISO8601",
  "postgres": { "ok": true, "version": "string", "pgvector": true },
  "redis":    { "ok": true, "version": "string" },
  "embedder": { "ok": true, "model": "string" }
}
```

---

### GET `/api/stats`

Row counts for `episodes`, `memories`, `identity_facts`, `knowledge_entities`,
`knowledge_edges`.

**Query params:** none

**Response:** `{ "episodes": int, "memories": int, ... }`

---

### GET `/api/stats/composition`

Breakdown of memories and identity facts by epistemic status and tier, plus
memories changed in the last 7 days. Powers the dashboard "state of mind" view.

**Query params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `scope` | `string` | env default | User scope |

**Response:**

```json
{
  "memories_by_status": { "canonical": int, "hypothesis": int, ... },
  "memories_by_tier":   { "semantic": int, ... },
  "identity_by_status": { "canonical": int, ... },
  "memories_changed_7d": int
}
```

---

## Ingestion and Episodes

### POST `/api/ingest`

Record raw text as an episode and (optionally) a recallable memory, then
schedule LLM knowledge-graph extraction in the background. Strips harness
scaffolding tags before recording; returns immediately with processing status.

**Request body (`IngestRequest`):**

| Field | Type | Default | Constraints |
|-------|------|---------|-------------|
| `text` | `string` | required | 1–100 000 chars |
| `source_ref` | `string` | `"web:capture"` | |
| `scope` | `string` | env default | |
| `add_memory` | `bool` | `true` | |
| `extract_knowledge` | `bool` | `true` | |
| `kind` | `"fact" \| "procedure" \| "preference"` | `"fact"` | |
| `epistemic_status` | `"canonical" \| "hypothesis" \| "belief"` | `"canonical"` | |

**Response:**

```json
{ "epi_id": "string", "mem_id": "string", "processing": "scheduled" }
```

Returns `{"skipped": "scaffolding-only"}` when the text was entirely harness
markup.

---

### GET `/api/episodes`

List episodes with optional filters.

**Query params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `scope` | `string` | env default | |
| `modality` | `string` | `null` | Filter by modality |
| `limit` | `int` | `50` | Max 200 |
| `offset` | `int` | `0` | |

**Response:** `{ "items": [...], "count": int }`

---

### GET `/api/episodes/{epi_id}`

Fetch a single episode with its derived memories.

**Path params:** `epi_id` — episode ID

**Response:**

```json
{ "episode": { ...row }, "memories": [ { "id", "statement", "epistemic_status", "valid_from", "valid_to" } ] }
```

---

## Memories and Facts

### GET `/api/memories`

List memories with optional filtering and full-text search.

**Query params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `scope` | `string` | env default | |
| `kind` | `string` | `null` | `fact`, `procedure`, `preference` |
| `epistemic_status` | `string` | `null` | |
| `valid` | `bool \| null` | `true` | `true`=active, `false`=invalidated, omit=all |
| `limit` | `int` | `50` | Max 200 |
| `offset` | `int` | `0` | |
| `q` | `string` | `null` | Full-text search via `search_tsv` GIN index |

**Response:** `{ "items": [...], "count": int }`

---

### GET `/api/memories/{mem_id}`

Fetch a single memory with its source episode, superseded-by pointer, and the
memory it superseded (version chain).

**Path params:** `mem_id` — memory ID

**Response:**

```json
{
  "memory": { ...row },
  "source_episode": { ...row },
  "superseded_by": { "id": "string", "statement": "string" } | null,
  "supersedes":    { "id": "string", "statement": "string" } | null
}
```

---

### POST `/api/memories`

Manually insert a memory row (no embedding / KG extraction). Use `/api/ingest`
for the full pipeline.

**Request body (`AddMemoryRequest`):**

| Field | Type | Default | Constraints |
|-------|------|---------|-------------|
| `statement` | `string` | required | 1–8 000 chars |
| `source_episode_id` | `string` | required | Must reference existing episode |
| `kind` | `string` | `"fact"` | |
| `epistemic_status` | `string` | `"canonical"` | |

**Response:** `{ "id": "string", "created_at": "ISO8601" }`

---

### POST `/api/memories/{mem_id}/invalidate`

Soft-delete a memory by setting `valid_to = now()`. Returns 409 if already
invalidated.

**Path params:** `mem_id`

**Request body (`InvalidateRequest`, optional):**

| Field | Type | Default |
|-------|------|---------|
| `reason` | `string` | `""` |

**Response:** `{ "id": "string", "valid_to": "ISO8601", "deleted": false }`

---

## Search and Recall

### GET `/api/search`

Full-text search over valid memories using the Postgres `plainto_tsquery` index.
Returns ranked results.

**Query params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `q` | `string` | required | Search query |
| `scope` | `string` | env default | |
| `limit` | `int` | `20` | Max 50 |

**Response:** `{ "query": "string", "items": [...], "count": int }`

---

### POST `/api/recall`

Semantic + graph retrieval (dense pgvector + sparse BM25 + entity + graph +
rerank). Authoritative recall path for Hermes and all agents. Results are
re-ranked by true cosine similarity against stored embeddings.

**Request body (`RecallRequest`):**

| Field | Type | Default | Constraints |
|-------|------|---------|-------------|
| `query` | `string` | required | 1–2 000 chars |
| `scope` | `string` | env default | |
| `mode` | `"fast" \| "deep" \| "divergent"` | `"fast"` | |
| `k` | `int` | `6` | 1–20 |

**Response:**

```json
{
  "results": [ { "id": "string", "text": "string", "score": float, "tier": "string", "epistemic_status": "string" } ],
  "count": int,
  "cached": true
}
```

---

## Knowledge Graph

### GET `/api/graph`

Fetch the full knowledge graph — entities ordered by degree (hubs first), edges
filtered to both endpoints in the returned node set.

**Query params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `scope` | `string` | env default | |
| `limit` | `int` | `20000` | Max 50 000 nodes |

**Response:**

```json
{
  "nodes": [ { "id": "string", "name": "string", "label": "string", "degree": int } ],
  "links": [ { "source": "string", "target": "string", "rel_type": "string" } ]
}
```

---

### GET `/api/graph/{entity_id}/expand`

BFS expansion from an entity: up to `k` hops of neighbours.

**Path params:** `entity_id`

**Query params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `k` | `int` | `1` | Hop depth, max 3 |

**Response:**

```json
{
  "entities": [ { "id", "name", "label", "properties", "epistemic_status" } ],
  "edges": [ { "id", "src_entity_id", "dst_entity_id", "rel_type" } ]
}
```

---

## Identity

### GET `/api/identity`

List identity facts, optionally filtered by predicate list and validity.

**Query params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `scope` | `string` | env default | |
| `predicates` | `string` | `null` | Comma-separated predicate names |
| `valid` | `bool \| null` | `true` | `true`=current, `false`=superseded, omit=all |

**Response:** `{ "items": [...], "count": int }`

---

### POST `/api/identity`

Propose an identity fact through the governance path (conflict resolution,
supersession, confidence gating). A missing `source_episode_id` auto-creates a
provenance episode. Auto-promotes to canonical when confidence ≥ 0.75.

**Request body (`ProposeIdentityRequest`):**

| Field | Type | Default | Constraints |
|-------|------|---------|-------------|
| `predicate` | `string` | required | 1–200 chars |
| `object` | `string` | required | 1–2 000 chars |
| `confidence` | `float` | `0.5` | 0.0–1.0 |
| `source_episode_id` | `string` | `""` | Auto-generated if empty |

**Response:** `{ "idf_id": "string", "id": "string", ...governance fields }`

---

## Cognition

### GET `/api/cognition/meta`

Fetch all current principles, assumptions, decision audits (last 50), and mental
models for a scope.

**Query params:**

| Param | Type | Default |
|-------|------|---------|
| `scope` | `string` | env default |

**Response:**

```json
{
  "principles": [...],
  "assumptions": [...],
  "decision_audits": [...],
  "mental_models": [...]
}
```

---

### GET `/api/cognition/reflection`

Last 10 reflection reports for a scope.

**Query params:** `scope` (string, env default)

**Response:** `{ "reports": [...] }`

---

### GET `/api/cognition/attention`

KG-grounded attention data: alignment gaps, focus areas (cognitive mass),
neglected entities, cognitive breadth, and load estimate.

**Query params:**

| Param | Type | Default |
|-------|------|---------|
| `scope` | `string` | env default |
| `window_days` | `int` | `7` |

**Response:**

```json
{
  "alignment_gaps": [...],
  "focus_areas": [...],
  "neglected": [...],
  "breadth": { ... },
  "cognitive_load": { ... }
}
```

---

### GET `/api/cognition/narrative`

Current life chapters and themes (valid, ordered by date / frequency).

**Query params:** `scope` (string, env default)

**Response:** `{ "chapters": [...], "themes": [...] }`

---

### POST `/api/cognition/narrative/compose`

Compose a personalised narrative passage from episodes and memories matching a
query, using the DEEP-tier LLM. Falls back to a heuristic excerpt if no LLM is
configured.

**Request body (`ComposeNarrativeRequest`):**

| Field | Type | Default | Constraints |
|-------|------|---------|-------------|
| `query` | `string` | required | 1–2 000 chars |
| `since` | `string \| null` | `null` | ISO timestamp lower bound |
| `domain` | `string \| null` | `null` | Optional domain focus hint |

**Response:**

```json
{
  "query": "string",
  "narrative": "string",
  "sources": int,
  "memories_referenced": int,
  "llm": bool
}
```

---

### POST `/api/reflection/weekly`

Run a weekly reflection over the last `window_days` days. Syncs identity
candidates and goal deltas from the report, distils lessons, and decays stale
lesson confidence. Uses the DEEP LLM tier.

**Request body (`ReflectionRequest`, optional):**

| Field | Type | Default | Constraints |
|-------|------|---------|-------------|
| `scope` | `string` | env default | |
| `window_days` | `int` | `7` | 1–90 |

**Response:** Reflection result merged with `identity_promoted`, `identity_skipped`,
`goals_synced`, `lessons`, `lesson_decay`, `llm: bool`.

---

### POST `/api/reflection/monthly`

Run a monthly reflection (30-day window). Syncs identity and goal deltas.
Uses the DEEP LLM tier.

**Request body (`ReflectionRequest`, optional):**

| Field | Type | Default |
|-------|------|---------|
| `scope` | `string` | env default |

**Response:** Reflection result merged with `identity_promoted`, `goals_synced`,
`llm: bool`.

---

### POST `/api/meta/audit`

Run a decision audit over recent episodes and distil principles. Syncs
principles into recallable memories. Uses the DEEP LLM tier.

**Request body (`MetaAuditRequest`, optional):**

| Field | Type | Default | Constraints |
|-------|------|---------|-------------|
| `scope` | `string` | env default | |
| `window_days` | `int` | `30` | 1–365 |

**Response:**

```json
{
  "audit": { ... },
  "principles_distilled": int,
  "principles": [...],
  "principles_mirrored": int,
  "principles_skipped": int
}
```

---

### POST `/api/meta/distill`

Distil principles from existing decision audits (no new audit pass). Syncs
resulting principles to memories. Uses the DEEP LLM tier.

**Request body (`MetaDistillRequest`, optional):**

| Field | Type | Default | Constraints |
|-------|------|---------|-------------|
| `scope` | `string` | env default | |
| `min_confidence` | `float` | `0.7` | 0.0–1.0 |

**Response:** `{ "principles_distilled": int, "principles": [...], "principles_mirrored": int, "principles_skipped": int }`

---

### POST `/api/narrative/generate`

Surface themes from episodes and compose life chapters. Supersedes prior
hypothesis-status themes and chapters. Uses the DEEP LLM tier.

**Request body (`NarrativeGenerateRequest`, optional):**

| Field | Type | Default | Constraints |
|-------|------|---------|-------------|
| `scope` | `string` | env default | |
| `min_frequency` | `int` | `3` | ≥ 1 |

**Response:** `{ "scope": "string", "themes_surfaced": int, "chapters_composed": int, "top_themes": [...], "llm": bool }`

---

### POST `/api/attention/scan`

Detect value-action alignment gaps and snapshot cognitive allocation and load.
Writes hypothesis-status alignment signals (supersedes prior hypothesis signals).

**Request body (`AttentionScanRequest`, optional):**

| Field | Type | Default | Constraints |
|-------|------|---------|-------------|
| `scope` | `string` | env default | |
| `window_days` | `int` | `14` | 1–365 |

**Response:** `{ "scope": "string", "alignment_gaps_found": int, "alignment_gaps": [...], "allocation": {...}, "cognitive_load": {...} }`

---

### POST `/api/consolidation/run`

Run memory consolidation passes (dedup, merge/promote, conflict resolve,
summarise, decay, recombine-incubate). `fast` runs dedup+conflict only; `deep`
runs all passes including the LLM summarise pass.

**Request body (`ConsolidationRunRequest`, optional):**

| Field | Type | Default |
|-------|------|---------|
| `mode` | `"fast" \| "deep"` | `"fast"` |
| `scope` | `string` | env default |

**Response:** Consolidation pass result dict (pass names → counts).

---

## Goals, Tasks, and Workspaces

### GET `/api/goals`

List goals, optionally filtered by status.

**Query params:**

| Param | Type | Default |
|-------|------|---------|
| `status` | `string \| null` | `null` |
| `include_invalidated` | `bool` | `false` |

**Response:** `{ "items": [...], "count": int }`

---

### POST `/api/goals`

Create a new goal.

**Request body (`CreateGoalRequest`):**

| Field | Type | Default | Constraints |
|-------|------|---------|-------------|
| `title` | `string` | required | 1–300 chars |
| `description` | `string \| null` | `null` | max 4 000 chars |
| `horizon` | `string \| null` | `null` | `life \| year \| quarter \| month` |
| `parent_id` | `string \| null` | `null` | |
| `priority` | `int` | `0` | −100–100 |
| `success_criteria` | `string \| null` | `null` | max 2 000 chars |
| `identity_refs` | `list[string]` | `[]` | |
| `project_refs` | `list[string]` | `[]` | |

**Response:** `{ "goal_id": "string", ... }`

---

### GET `/api/goals/{goal_id}`

Fetch a single goal by ID.

**Path params:** `goal_id`

**Response:** Goal row dict.

---

### PATCH `/api/goals/{goal_id}`

Update mutable goal fields. Only provided fields are changed.

**Path params:** `goal_id`

**Request body (`UpdateGoalRequest`):**

| Field | Type | Constraints |
|-------|------|-------------|
| `title` | `string \| null` | 1–300 chars |
| `description` | `string \| null` | max 4 000 chars |
| `horizon` | `string \| null` | `life \| year \| quarter \| month` |
| `status` | `string \| null` | `active \| paused \| achieved \| abandoned` |
| `priority` | `int \| null` | −100–100 |
| `success_criteria` | `string \| null` | max 2 000 chars |
| `progress` | `float \| null` | 0.0–1.0 |
| `parent_id` | `string \| null` | |

**Response:** Updated goal row dict.

---

### POST `/api/goals/{goal_id}/invalidate`

Soft-delete a goal.

**Path params:** `goal_id`

**Request body (`InvalidateGoalRequest`, optional):**

| Field | Type | Default |
|-------|------|---------|
| `reason` | `string` | `""` (max 500 chars) |

**Response:** `{ "goal_id": "string", ... }`

---

### GET `/api/goals/{goal_id}/plan`

Get the current decomposition plan for a goal (or `{"plan": null}` if none).

**Path params:** `goal_id`

**Response:** `{ "plan": { ...plan row with tasks } \| null }`

---

### POST `/api/goals/{goal_id}/decompose`

Decompose a goal into an agent plan of tasks using the AGENTIC LLM tier.
Creates a `goal_plans` row with status `proposed`.

**Path params:** `goal_id`

**Request body (`DecomposeRequest`, optional):**

| Field | Type | Default |
|-------|------|---------|
| `guidance` | `string \| null` | `null` (max 2 000 chars) |

**Response:** `{ "plan_id": "string", "tasks": [...] }`

---

### GET `/api/goals/{goal_id}/artifacts`

List artifacts produced under a goal.

**Path params:** `goal_id`

**Response:** `{ "items": [...], "count": int }`

---

### GET `/api/decisions`

List recorded decisions.

**Query params:**

| Param | Type | Default |
|-------|------|---------|
| `due_for_review` | `bool` | `false` |
| `limit` | `int` | `100` (max 500) |

**Response:** `{ "items": [...], "count": int }`

---

### POST `/api/decisions`

Record a decision with rationale, options, reversibility, and optional outcome
prediction.

**Request body (`RecordDecisionRequest`):**

| Field | Type | Default | Constraints |
|-------|------|---------|-------------|
| `title` | `string` | required | 1–300 chars |
| `chosen` | `string` | required | 1–2 000 chars |
| `rationale` | `string` | required | 1–4 000 chars |
| `context` | `string \| null` | `null` | max 4 000 chars |
| `options_considered` | `list` | `[]` | |
| `reversibility` | `string \| null` | `null` | `reversible \| costly \| one_way` |
| `goal_id` | `string \| null` | `null` | |
| `review_at` | `string \| null` | `null` | ISO timestamp |
| `predicted_outcome` | `string \| null` | `null` | max 2 000 chars |
| `prediction_confidence` | `float \| null` | `null` | 0–1 |

**Response:** `{ "dec_id": "string", ... }`

---

### POST `/api/decisions/{dec_id}/review`

Record the outcome of a past decision and optionally note a lesson learned.

**Path params:** `dec_id`

**Request body (`ReviewDecisionRequest`):**

| Field | Type | Default | Constraints |
|-------|------|---------|-------------|
| `outcome` | `string` | required | 1–4 000 chars |
| `valence` | `string` | `"mixed"` | `success \| partial \| failure \| mixed \| too_early` |
| `matched_prediction` | `bool \| null` | `null` | |
| `lesson` | `string \| null` | `null` | max 2 000 chars |
| `applies_to_entities` | `list[string]` | `[]` | |

**Response:** `{ "dec_id": "string", ... }`

---

### POST `/api/decisions/{dec_id}/council`

Stress-test a decision with a 4-perspective council synthesis (orchestration
workflow). Persists the result in `decisions.properties.council`.

**Path params:** `dec_id`

**Response:** `{ "dec_id": "string", "council": { ... } }`

---

### GET `/api/opportunities`

List opportunities with optional status filter.

**Query params:**

| Param | Type | Default |
|-------|------|---------|
| `status` | `string \| null` | `null` |
| `limit` | `int` | `100` (max 500) |

**Response:** `{ "items": [...], "count": int }`

---

### POST `/api/opportunities`

Create an opportunity (signal that something is worth exploring).

**Request body (`CreateOpportunityRequest`):**

| Field | Type | Default | Constraints |
|-------|------|---------|-------------|
| `title` | `string` | required | 1–300 chars |
| `description` | `string` | required | 1–4 000 chars |
| `source` | `string` | `"manual"` | max 50 chars |
| `evidence_refs` | `list[string]` | `[]` | |
| `novelty` | `float \| null` | `null` | 0.0–1.0 |
| `value_est` | `float \| null` | `null` | 0.0–1.0 |
| `feasibility` | `float \| null` | `null` | 0.0–1.0 |

**Response:** `{ "opp_id": "string", ... }`

---

### POST `/api/opportunities/{opp_id}/resolve`

Accept or dismiss an opportunity.

**Path params:** `opp_id`

**Request body (`ResolveOpportunityRequest`):**

| Field | Type | Constraints |
|-------|------|-------------|
| `accept` | `bool` | required |
| `resolution` | `string` | 1–500 chars |

**Response:** `{ "opp_id": "string", "status": "accepted" \| "dismissed" }`

---

### GET `/api/workspaces`

List active workspaces with project counts.

**Query params:** `scope` (string, env default)

**Response:** `{ "items": [...], "count": int }`

---

### POST `/api/workspaces`

Create a workspace.

**Request body (`CreateWorkspaceRequest`):**

| Field | Type | Default | Constraints |
|-------|------|---------|-------------|
| `name` | `string` | required | 1–500 chars |
| `kind` | `string` | `"project"` | |
| `properties` | `dict` | `{}` | |

**Response:** `{ "id": "string", "name": "string", "kind": "string", "created_at": "ISO8601" }`

---

### GET `/api/workspaces/{workspace_id}`

Fetch workspace detail with its non-archived projects (goal + artifact counts).

**Path params:** `workspace_id`

**Response:** `{ "workspace": { ...row }, "projects": [...] }`

---

### GET `/api/projects`

List projects with optional workspace filter, goal counts, and artifact counts.

**Query params:**

| Param | Type | Default |
|-------|------|---------|
| `workspace_id` | `string \| null` | `null` |
| `scope` | `string` | env default |

**Response:** `{ "items": [...], "count": int }`

---

### POST `/api/projects`

Create a project inside a workspace.

**Request body (`CreateProjectRequest`):**

| Field | Type | Default | Constraints |
|-------|------|---------|-------------|
| `workspace_id` | `string` | required | Must exist |
| `name` | `string` | required | 1–500 chars |
| `properties` | `dict` | `{}` | |

**Response:** `{ "id": "string", "workspace_id": "string", "name": "string", "status": "active", "created_at": "ISO8601" }`

---

### GET `/api/project/{project_id}`

Fetch project detail with placed goals and produced artifacts. Note the singular
`/api/project/` prefix (avoids shadowing Next.js `/api/projects/[slug]`).

**Path params:** `project_id`

**Response:**

```json
{
  "project": { ...row, "workspace_name": "string", "workspace_slug": "string" },
  "goals": [...],
  "artifacts": [...]
}
```

---

### GET `/api/artifacts`

List artifacts, optionally filtered by project or goal.

**Query params:**

| Param | Type | Default |
|-------|------|---------|
| `project_id` | `string \| null` | `null` |
| `goal_id` | `string \| null` | `null` |
| `scope` | `string` | env default |

**Response:** `{ "items": [...], "count": int }` (max 200 rows)

---

## Studio

### GET `/api/studio`

List all studios (creative workspaces) for a scope.

**Query params:** `scope` (string, env default)

**Response:** `{ "items": [...], "count": int }`

---

### POST `/api/studio`

Create a studio. Emits a `studio.created` event.

**Request body (`CreateStudioRequest`):**

| Field | Type | Constraints |
|-------|------|-------------|
| `title` | `string` | 1–500 chars |
| `properties` | `dict` | optional, default `{}` |

**Response:** `{ "stu_id": "string", ... }`

---

### GET `/api/studio/{studio_id}`

Fetch a studio with its sketches and inter-sketch links.

**Path params:** `studio_id`

**Response:**

```json
{
  "studio": { ...row },
  "sketches": [...],
  "links": [ { "id", "src_sketch_id", "dst_sketch_id", "rel_type" } ]
}
```

---

### POST `/api/studio/{studio_id}/sketch`

Add a sketch to a studio.

**Path params:** `studio_id`

**Request body (`CreateSketchRequest`):**

| Field | Type | Default | Constraints |
|-------|------|---------|-------------|
| `content` | `string` | required | 1–20 000 chars |
| `kind` | `string` | `"text"` | |
| `properties` | `dict` | `{}` | |

**Response:** `{ "sketch_id": "string", ... }`

---

### PATCH `/api/studio/sketch/{sketch_id}`

Edit sketch content or promote its epistemic status up the ladder
(`seed → conjecture → hypothesis`). `canonical` is unreachable — graduate to
promote fully.

**Path params:** `sketch_id`

**Request body (`UpdateSketchRequest`):**

| Field | Type | Constraints |
|-------|------|-------------|
| `content` | `string \| null` | 1–20 000 chars |
| `epistemic_status` | `"conjecture" \| "hypothesis" \| null` | |

**Response:** Updated sketch row dict.

---

### POST `/api/studio/sketch/{sketch_id}/graduate`

Graduate a sketch (must be ≥ conjecture) into a Project in the "Studio
Graduates" workspace. One-way; the sketch gains a `properties.graduated_to`
pointer.

**Path params:** `sketch_id`

**Request body (`GraduateSketchRequest`, optional):**

| Field | Type | Default |
|-------|------|---------|
| `target_type` | `string` | `"project"` |

**Response:** `{ "sketch_id": "string", "project_id": "string", ... }`

---

## Simulation

### GET `/api/simulation/runs`

List simulation runs for a scope.

**Query params:** `scope` (string, env default)

**Response:** `{ "items": [...], "count": int }`

---

### POST `/api/simulation/runs`

Create a simulation run (question + optional world model). Does not execute;
call the execute endpoint to run it.

**Request body (`CreateSimulationRunRequest`):**

| Field | Type | Default | Constraints |
|-------|------|---------|-------------|
| `question` | `string` | required | 1–2 000 chars |
| `world_model_id` | `string \| null` | `null` | |
| `parameters` | `dict` | `{}` | |

**Response:** `{ "sim_id": "string", ... }`

---

### POST `/api/simulation/runs/{sim_id}/execute`

Execute a simulation: projects scenarios and possible-world memories into an
isolated `scenario:<id>` scope (invisible to default recall, never promoted).

**Path params:** `sim_id`

**Response:** `{ "sim_id": "string", "outcomes": [...] }`

---

## Orchestration and Jobs

### GET `/api/events`

Activity feed: paginated events from the `events` table.

**Query params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `scope` | `string` | env default | |
| `limit` | `int` | `50` | Max 100 |
| `offset` | `int` | `0` | |

**Response:** `{ "items": [...], "count": int }`

---

### GET `/api/events/stream`

Server-sent event stream (2 s poll) over the `events` table. Clients receive
live updates without polling REST endpoints. Connect and disconnect to manage
the stream.

**Query params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `types` | `string \| null` | `null` | Comma-separated short-type prefixes to filter |
| `last_seq` | `int` | `0` | Resume from sequence number (`0` = start at tip) |

**Response:** `text/event-stream` — each event: `id: <seq>\ndata: <json>\n\n`. An
initial `event: hello` frame sends the current tip sequence. Keepalive pings
sent every 2 s.

---

### POST `/api/agents/runs`

Start an agent run with a natural-language task string.

**Request body (`StartRunRequest`):**

| Field | Type | Constraints |
|-------|------|-------------|
| `task` | `string` | 1–4 000 chars |

**Response:** `{ "run_id": "string", "status": "running" }`

---

### POST `/api/agents/inbound`

Hermes-facing task intake. Same as `POST /api/agents/runs` but also accepts
`source` and `session_ref` for cross-boundary traceability.

**Request body (`InboundRequest`):**

| Field | Type | Default | Constraints |
|-------|------|---------|-------------|
| `task` | `string` | required | 1–4 000 chars |
| `source` | `string` | `"hermes"` | max 50 chars |
| `session_ref` | `string \| null` | `null` | max 200 chars |

**Response:** `{ "run_id": "string", "status": "running" }`

---

### GET `/api/agents/runs`

List agent runs with optional status and agent-name filters.

**Query params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `status` | `string \| null` | `null` | e.g. `running`, `done`, `failed` |
| `agent` | `string \| null` | `null` | Prefix match on agent name |
| `limit` | `int` | `50` | Max 200 |

**Response:** `{ "items": [...], "count": int }`

---

### GET `/api/agents/runs/{run_id}`

Full execution trace for a run: run metadata, actions+observations, tool calls,
and approval records.

**Path params:** `run_id`

**Response:**

```json
{
  "id": "string", "agent": "string", "task": "string",
  "status": "string", "result": "string", "error": "string",
  "created_at": "ISO8601", "finished_at": "ISO8601",
  "actions": [ { "id", "kind", "payload", "created_at", "observation" } ],
  "tool_calls": [ { "id", "tool", "args", "entry_hash", "created_at" } ],
  "approvals": [ { "apv_id", "action_class", "payload", "state", "origin", "created_at", "decided_at" } ]
}
```

---

### POST `/api/agents/runs/{run_id}/resume`

Resume a parked run (after an approval decision). Returns 409 if the run is not
in the `parked` state.

**Path params:** `run_id`

**Response:** `{ "run_id": "string", "status": "running" }`

---

### POST `/api/agents/runs/{run_id}/cancel`

Cancel a running or parked run.

**Path params:** `run_id`

**Response:** `{ "run_id": "string", "status": "cancelled" }`

---

### POST `/api/scheduled-jobs`

Create a user-defined autonomous job (persisted in `scheduled_jobs`; live
registered into the scheduler immediately).

**Request body (`ScheduledJobCreate`):**

| Field | Type | Default | Constraints |
|-------|------|---------|-------------|
| `name` | `string` | required | 1–120 chars; unique per scope |
| `task` | `string` | required | 1–4 000 chars (NL task for the Executive) |
| `cadence_type` | `string` | required | `every \| daily_at \| weekly_at \| monthly_at` |
| `cadence_json` | `dict` | `{}` | Cadence parameters (e.g. `{"minutes": 60}`) |
| `enabled` | `bool` | `true` | |

**Response:** Job row dict with `next_due`, `registered`, `cadence_display`.

---

### GET `/api/scheduled-jobs`

List all user-defined scheduled jobs with live scheduler state (next due time,
registration status).

**Response:** `{ "items": [...], "count": int }`

---

### GET `/api/scheduled-jobs/{job_id}`

Fetch a single scheduled job.

**Path params:** `job_id`

**Response:** Job row dict.

---

### PATCH `/api/scheduled-jobs/{job_id}`

Update a scheduled job. Immediately re-registers the live job with the new
cadence; disabling unregisters it.

**Path params:** `job_id`

**Request body (`ScheduledJobUpdate`):**

| Field | Type |
|-------|------|
| `name` | `string \| null` |
| `task` | `string \| null` |
| `cadence_type` | `string \| null` |
| `cadence_json` | `dict \| null` |
| `enabled` | `bool \| null` |

**Response:** Updated job row dict.

---

### DELETE `/api/scheduled-jobs/{job_id}`

Delete a scheduled job and unregister it from the live scheduler.

**Path params:** `job_id`

**Response:** `{ "id": "string", "deleted": true }`

---

### POST `/api/scheduled-jobs/{job_id}/run-now`

Fire a job immediately (off-cadence). Starts the Executive run async and
returns once the run is started.

**Path params:** `job_id`

**Response:** `{ "id": "string", "status": "started" }`

---

### GET `/api/inbox`

List inbox items (job-run outputs delivered to the user).

**Query params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `unread` | `bool` | `false` | Filter to unread only |
| `job` | `string \| null` | `null` | Filter by job ID |
| `limit` | `int` | `100` | Max 300 |

**Response:** `{ "items": [...], "count": int }`

---

### GET `/api/inbox/unread-count`

Unread inbox item count.

**Response:** `{ "unread": int }`

---

### POST `/api/inbox/{item_id}/read`

Mark an inbox item as read.

**Path params:** `item_id`

**Response:** `{ "id": "string", "read": true }`

---

### POST `/api/goal-plans/{plan_id}/approve`

Approve a proposed goal plan (transitions status from `proposed` to `approved`).

**Path params:** `plan_id`

**Response:** `{ "plan_id": "string", "status": "approved" }`

---

### POST `/api/goal-plans/{plan_id}/dispatch-all`

Dispatch all approved tasks in a plan to the Executive agent.

**Path params:** `plan_id`

**Response:** `{ "dispatched": int, "tasks": [...] }`

---

### POST `/api/goal-plans/{plan_id}/execute`

Approve and dispatch a plan in one step (combines approve + dispatch-all).

**Path params:** `plan_id`

**Response:** `{ "plan_id": "string", "dispatched": int }`

---

### POST `/api/goal-tasks/{task_id}/dispatch`

Dispatch a single goal task to the Executive agent.

**Path params:** `task_id`

**Response:** `{ "task_id": "string", "run_id": "string", ... }`

---

### GET `/api/orchestrator/overview`

Active goals, pending plans, and recent run statistics — the orchestrator
dashboard summary.

**Response:**

```json
{
  "goals": { "active": int, "total": int },
  "plans": { "proposed": int, "approved": int },
  "runs":  { "running": int, "done_24h": int }
}
```

---

### GET `/api/orchestrator/messages`

List orchestrator messages (goal/project chat history).

**Query params:**

| Param | Type | Default |
|-------|------|---------|
| `goal_id` | `string \| null` | `null` |
| `project_id` | `string \| null` | `null` |
| `limit` | `int` | `100` |

**Response:** `{ "items": [...], "count": int }`

---

### POST `/api/orchestrator/chat`

Send a message to the orchestrator (goal-aware conversational planning). Routes
through the AGENTIC LLM tier and the Executive runner.

**Request body (`OrchestratorChatRequest`):**

| Field | Type | Default | Constraints |
|-------|------|---------|-------------|
| `message` | `string` | required | 1–4 000 chars |
| `goal_id` | `string \| null` | `null` | max 60 chars |
| `project_id` | `string \| null` | `null` | max 60 chars |

**Response:** `{ "reply": "string", "run_id": "string \| null" }`

---

### POST `/api/orchestrator/autoplan`

Manually run the autoplan sweep: decomposes active unplanned goals into
proposed plans (respects the `auto_plan` setting).

**Response:** `{ "planned": int }`

---

### POST `/api/orchestrator/promote`

Manually run the opportunity-to-goal promotion sweep (respects `auto_promote`).

**Response:** `{ "promoted": int }`

---

### POST `/api/discovery/scan`

Manually trigger the weekly opportunity-discovery scan (also runs on the weekly
scheduler job). Scans episodes/memories for emerging themes and creates
opportunities.

**Response:** `{ "opportunities_found": int, ... }`

---

### GET `/api/scheduler`

Heartbeat table for all background scheduler jobs: cadence, last fire time,
next due time, and consecutive-failure count.

**Response:**

```json
{
  "running": bool,
  "jobs": [
    { "name": "string", "cadence": "string", "last_fired": "ISO8601",
      "next_due": "ISO8601", "consecutive_failures": int }
  ]
}
```

---

### GET `/api/evolution/prompts`

List prompt versions, optionally filtered by name.

**Query params:**

| Param | Type | Default |
|-------|------|---------|
| `name` | `string \| null` | `null` |

**Response:** `{ "items": [...], "count": int }`

---

### POST `/api/evolution/prompts`

Propose a new prompt version.

**Request body (`ProposePromptRequest`):**

| Field | Type | Constraints |
|-------|------|-------------|
| `name` | `string` | 1–100 chars |
| `content` | `string` | 20–20 000 chars |
| `notes` | `string` | optional, max 1 000 chars |

**Response:** `{ "pmt_id": "string", ... }`

---

### POST `/api/evolution/prompts/{pmt_id}/evaluate`

Score a proposed prompt version using the AGENTIC LLM tier.

**Path params:** `pmt_id`

**Response:** `{ "pmt_id": "string", "score": float, ... }`

---

### POST `/api/evolution/prompts/{pmt_id}/activate`

Activate a prompt version. Requires a granted `self_modify` approval
(`POST /api/approvals` then `POST /api/approvals/{apv_id}/grant` first).

**Path params:** `pmt_id`

**Request body (`ActivatePromptRequest`):**

| Field | Type | Constraints |
|-------|------|-------------|
| `approval_id` | `string` | 5–60 chars |

**Response:** `{ "pmt_id": "string", "status": "active" }`

---

### GET `/api/evolution/timeline`

Evolution event feed (newest first).

**Query params:** `limit` (int, default 50, max 200)

**Response:** `{ "items": [ { "seq", "type", "subject", "data", "at" } ] }`

---

## Safety

### GET `/api/approvals`

List pending, unexpired approvals for the current scope.

**Response:** `{ "items": [...], "count": int }`

---

### POST `/api/approvals`

Create a human-originated approval (e.g. for a hard-forget flow). Deliberate
two-step: create here, then grant explicitly before calling the gated action.

**Request body (`CreateApprovalRequest`):**

| Field | Type | Default | Constraints |
|-------|------|---------|-------------|
| `action_class` | `string` | required | 1–50 chars |
| `payload` | `dict` | `{}` | |
| `ttl_seconds` | `int \| null` | `null` | 60–2 592 000 (30 days) |

**Response:**

```json
{ "apv_id": "string", "state": "pending", "origin": "human", "action_class": "string", "ttl_seconds": int }
```

---

### POST `/api/approvals/{apv_id}/grant`

Grant a pending approval. If a run was parked waiting for this approval it is
automatically resumed.

**Path params:** `apv_id`

**Response:** `{ "apv_id": "string", "state": "granted", "run_id": "string \| null", "resumed": bool }`

---

### POST `/api/approvals/{apv_id}/deny`

Deny a pending approval (with optional reason). Parked run is resumed so the
agent can degrade gracefully.

**Path params:** `apv_id`

**Request body (`DenyApprovalRequest`, optional):**

| Field | Type | Default |
|-------|------|---------|
| `reason` | `string` | `"user_denied"` (max 500 chars) |

**Response:** `{ "apv_id": "string", "state": "denied", "run_id": "string \| null", "resumed": bool }`

---

### GET `/api/safety/kill`

Read killswitch state: whether the global (or per-agent) kill flag is set.

**Query params:**

| Param | Type | Default |
|-------|------|---------|
| `agent` | `string \| null` | `null` |

**Response:** `{ "killed": bool, "agent": "string \| null", ... }`

---

### POST `/api/safety/kill`

Engage the killswitch. Fail-closed: every PDP verdict above read returns DENY
while the flag is set. Pass an agent name to kill only one agent's actions.

**Request body (`KillRequest`, optional):**

| Field | Type | Default |
|-------|------|---------|
| `agent` | `string \| null` | `null` |

**Response:** `{ "killed": true, "agent": "string \| null", "set_by": "string" }`

---

### DELETE `/api/safety/kill`

Clear the killswitch (re-enable agent side effects).

**Query params:**

| Param | Type | Default |
|-------|------|---------|
| `agent` | `string \| null` | `null` |

**Response:** `{ "killed": false, "agent": "string \| null" }`

---

## Observability

### GET `/api/observability/llm`

LLM routing health: per-tier (fast/agentic/deep) provider, model, fallback
chain, and since-boot usage counters.

**Response:**

```json
{
  "tiers": {
    "fast":    { "model": "string", "endpoint": "string", "chain": [...], "configured": bool,
                 "calls": int, "errors": int, "fallbacks": int, "avg_latency_ms": float,
                 "last_model": "string", "last_error": "string", "error_rate": float },
    "agentic": { ... },
    "deep":    { ... }
  },
  "uptime_seconds": float
}
```

---

### GET `/api/observability/recall`

Recall throughput and cache stats (since boot).

**Response:**

```json
{
  "requests": int, "cache_hits": int, "cache_misses": int, "errors": int,
  "hit_rate": float, "avg_latency_ms": float, "avg_latency_cached_ms": float,
  "uptime_seconds": float
}
```

---

### GET `/api/observability/pipeline`

Ingest pipeline backlog (unembedded episodes/memories, episodes awaiting
distillation), recent ingest rate, and KG size.

**Query params:** `scope` (string, env default)

**Response:**

```json
{
  "scope": "string",
  "backlog": { "unembedded_episodes": int, "unembedded_memories": int, "episodes_awaiting_distillation": int },
  "ingest_rate": { "last_1h": int, "last_24h": int },
  "knowledge_graph": { "entities": int, "edges": int }
}
```

---

### GET `/api/observability/overview`

Single-call rollup for the home-page monitor: health, counts, composition, LLM
tiers, recall, pipeline, and scheduler in one request.

**Query params:** `scope` (string, env default)

**Response:** Combined fields from health, stats, composition, LLM, recall,
pipeline, and scheduler endpoints plus a `timestamp` field.

---

### POST `/api/observability/reset`

Zero all in-process metric counters (uptime is preserved). Does not reset
database rows.

**Response:** `{ "ok": true, "reset_at": "ISO8601" }`

---

### GET `/api/systems`

Aggregate systems-dashboard payload: infrastructure health (Postgres/Redis/
embedder/API), table counts, per-engine event activity (last run, runs 24h/7d,
recent events), and scheduler summary.

**Response:**

```json
{
  "timestamp": "ISO8601",
  "infrastructure": [ { "name": "string", "ok": bool, "detail": "string" } ],
  "stats": { ...counts },
  "engines": [ { "name", "label", "last_run", "last_event_type", "runs_24h", "runs_7d", "recent" } ],
  "scheduler": { "running": bool, "jobs": int, "failing": [...], "next_due": "ISO8601" }
}
```

---

### GET `/api/logs/sources`

List known log sources (api, deploy, gate, up) and their file metadata (size,
modified time, existence).

**Response:** `{ "sources": [ { "name", "path", "exists", "size_bytes", "modified" } ] }`

---

### GET `/api/logs`

Tail the last `lines` lines of a named log file.

**Query params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `source` | `string` | `"api"` | One of `api`, `deploy`, `gate`, `up` |
| `lines` | `int` | `200` | Max 2 000 |

**Response:** `{ "source", "path", "exists", "size_bytes", "modified", "lines": [...], "count": int }`

---

## Settings

### GET `/api/settings`

List all runtime settings with effective value, type, default, category, and
description.

**Response:** `{ "settings": { "<key>": { "value": ..., "type": "string", "default": ..., "category": "string", "description": "string" } } }`

---

### GET `/api/settings/{key}`

Fetch a single setting by key.

**Path params:** `key`

**Response:** `{ "key": "string", "value": ..., "type": "string", ... }`

---

### PUT `/api/settings/{key}`

Update a setting. Value is coerced and validated against the registry.

**Path params:** `key`

**Request body:** `{ "value": <any> }`

**Response:** `{ "key": "string", "value": ..., "type": "string", ... }`

---

### GET `/api/settings/agent-bypass`

Read the agent-bypass toggle (skip approval gates for side-effecting actions).

**Response:** `{ "bypass": bool }`

---

### POST `/api/settings/agent-bypass`

Set the agent-bypass toggle.

**Request body (`BypassRequest`):** `{ "enabled": bool }`

**Response:** `{ "bypass": bool }`

---

### GET `/api/settings/auto-plan`

Read the auto-plan toggle (scheduler decomposes active goals automatically).

**Response:** `{ "auto_plan": bool }`

---

### POST `/api/settings/auto-plan`

Set the auto-plan toggle.

**Request body (`BypassRequest`):** `{ "enabled": bool }`

**Response:** `{ "auto_plan": bool }`

---

### GET `/api/settings/auto-promote`

Read the auto-promote toggle (scheduler promotes high-scoring opportunities to
goals automatically).

**Response:** `{ "auto_promote": bool }`

---

### POST `/api/settings/auto-promote`

Set the auto-promote toggle.

**Request body (`BypassRequest`):** `{ "enabled": bool }`

**Response:** `{ "auto_promote": bool }`

---

## Endpoint Index

All 97 routes documented in this reference:

| Method | Path | Summary |
|--------|------|---------|
| GET | `/api/health` | Connectivity check (Postgres, Redis, embedder) |
| GET | `/api/stats` | Row counts for core tables |
| GET | `/api/stats/composition` | Memory/identity breakdown by status and tier |
| POST | `/api/ingest` | Ingest text as episode + memory + KG extraction |
| GET | `/api/episodes` | List episodes |
| GET | `/api/episodes/{epi_id}` | Episode with derived memories |
| GET | `/api/memories` | List memories with full-text search |
| GET | `/api/memories/{mem_id}` | Single memory with version chain |
| POST | `/api/memories` | Manually insert a memory |
| POST | `/api/memories/{mem_id}/invalidate` | Soft-delete a memory |
| GET | `/api/search` | Full-text search over valid memories |
| POST | `/api/recall` | Semantic + graph retrieval (dense + rerank) |
| GET | `/api/graph` | Full knowledge graph (nodes + links) |
| GET | `/api/graph/{entity_id}/expand` | BFS neighbourhood expansion |
| GET | `/api/identity` | List identity facts |
| POST | `/api/identity` | Propose an identity fact (governance path) |
| GET | `/api/cognition/meta` | Principles, assumptions, audits, mental models |
| GET | `/api/cognition/reflection` | Last 10 reflection reports |
| GET | `/api/cognition/attention` | KG-grounded attention data |
| GET | `/api/cognition/narrative` | Life chapters and themes |
| POST | `/api/cognition/narrative/compose` | LLM-composed narrative passage |
| POST | `/api/reflection/weekly` | Run weekly reflection |
| POST | `/api/reflection/monthly` | Run monthly reflection |
| POST | `/api/meta/audit` | Decision audit + principle distillation |
| POST | `/api/meta/distill` | Distil principles from existing audits |
| POST | `/api/narrative/generate` | Surface themes + compose life chapters |
| POST | `/api/attention/scan` | Detect alignment gaps + snapshot load |
| POST | `/api/consolidation/run` | Run memory consolidation passes |
| GET | `/api/goals` | List goals |
| POST | `/api/goals` | Create goal |
| GET | `/api/goals/{goal_id}` | Fetch goal |
| PATCH | `/api/goals/{goal_id}` | Update goal |
| POST | `/api/goals/{goal_id}/invalidate` | Soft-delete goal |
| GET | `/api/goals/{goal_id}/plan` | Get decomposition plan |
| POST | `/api/goals/{goal_id}/decompose` | Decompose goal into agent plan |
| GET | `/api/goals/{goal_id}/artifacts` | List goal artifacts |
| GET | `/api/decisions` | List decisions |
| POST | `/api/decisions` | Record a decision |
| POST | `/api/decisions/{dec_id}/review` | Record decision outcome |
| POST | `/api/decisions/{dec_id}/council` | Council stress-test |
| GET | `/api/opportunities` | List opportunities |
| POST | `/api/opportunities` | Create opportunity |
| POST | `/api/opportunities/{opp_id}/resolve` | Accept or dismiss opportunity |
| GET | `/api/workspaces` | List workspaces |
| POST | `/api/workspaces` | Create workspace |
| GET | `/api/workspaces/{workspace_id}` | Workspace detail with projects |
| GET | `/api/projects` | List projects |
| POST | `/api/projects` | Create project |
| GET | `/api/project/{project_id}` | Project detail with goals + artifacts |
| GET | `/api/artifacts` | List artifacts |
| GET | `/api/studio` | List studios |
| POST | `/api/studio` | Create studio |
| GET | `/api/studio/{studio_id}` | Studio with sketches + links |
| POST | `/api/studio/{studio_id}/sketch` | Add sketch to studio |
| PATCH | `/api/studio/sketch/{sketch_id}` | Edit sketch content or status |
| POST | `/api/studio/sketch/{sketch_id}/graduate` | Graduate sketch to project |
| GET | `/api/simulation/runs` | List simulation runs |
| POST | `/api/simulation/runs` | Create simulation run |
| POST | `/api/simulation/runs/{sim_id}/execute` | Execute a simulation |
| GET | `/api/events` | Activity feed (paginated) |
| GET | `/api/events/stream` | SSE live event stream |
| POST | `/api/agents/runs` | Start agent run |
| POST | `/api/agents/inbound` | Hermes-facing task intake |
| GET | `/api/agents/runs` | List agent runs |
| GET | `/api/agents/runs/{run_id}` | Full run trace (actions, tools, approvals) |
| POST | `/api/agents/runs/{run_id}/resume` | Resume parked run |
| POST | `/api/agents/runs/{run_id}/cancel` | Cancel run |
| POST | `/api/scheduled-jobs` | Create scheduled job |
| GET | `/api/scheduled-jobs` | List scheduled jobs |
| GET | `/api/scheduled-jobs/{job_id}` | Get scheduled job |
| PATCH | `/api/scheduled-jobs/{job_id}` | Update scheduled job |
| DELETE | `/api/scheduled-jobs/{job_id}` | Delete scheduled job |
| POST | `/api/scheduled-jobs/{job_id}/run-now` | Fire job immediately |
| GET | `/api/inbox` | List inbox items |
| GET | `/api/inbox/unread-count` | Unread inbox count |
| POST | `/api/inbox/{item_id}/read` | Mark inbox item read |
| POST | `/api/goal-plans/{plan_id}/approve` | Approve goal plan |
| POST | `/api/goal-plans/{plan_id}/dispatch-all` | Dispatch all plan tasks |
| POST | `/api/goal-plans/{plan_id}/execute` | Approve + dispatch in one step |
| POST | `/api/goal-tasks/{task_id}/dispatch` | Dispatch single task |
| GET | `/api/orchestrator/overview` | Orchestrator dashboard summary |
| GET | `/api/orchestrator/messages` | Orchestrator chat history |
| POST | `/api/orchestrator/chat` | Send message to orchestrator |
| POST | `/api/orchestrator/autoplan` | Manually run autoplan sweep |
| POST | `/api/orchestrator/promote` | Manually run promote sweep |
| POST | `/api/discovery/scan` | Trigger opportunity discovery scan |
| GET | `/api/scheduler` | Scheduler heartbeat table |
| GET | `/api/evolution/prompts` | List prompt versions |
| POST | `/api/evolution/prompts` | Propose prompt version |
| POST | `/api/evolution/prompts/{pmt_id}/evaluate` | Evaluate prompt version |
| POST | `/api/evolution/prompts/{pmt_id}/activate` | Activate prompt version |
| GET | `/api/evolution/timeline` | Evolution event feed |
| GET | `/api/approvals` | List pending approvals |
| POST | `/api/approvals` | Create human-originated approval |
| POST | `/api/approvals/{apv_id}/grant` | Grant approval |
| POST | `/api/approvals/{apv_id}/deny` | Deny approval |
| GET | `/api/safety/kill` | Read killswitch state |
| POST | `/api/safety/kill` | Engage killswitch |
| DELETE | `/api/safety/kill` | Clear killswitch |
| GET | `/api/observability/llm` | LLM routing health |
| GET | `/api/observability/recall` | Recall throughput + cache stats |
| GET | `/api/observability/pipeline` | Ingest pipeline backlog |
| GET | `/api/observability/overview` | Full observability rollup |
| POST | `/api/observability/reset` | Reset metric counters |
| GET | `/api/systems` | Systems dashboard payload |
| GET | `/api/logs/sources` | Known log sources and file metadata |
| GET | `/api/logs` | Tail log file |
| GET | `/api/settings` | List all settings |
| GET | `/api/settings/{key}` | Get single setting |
| PUT | `/api/settings/{key}` | Update setting |
| GET | `/api/settings/agent-bypass` | Read agent-bypass toggle |
| POST | `/api/settings/agent-bypass` | Set agent-bypass toggle |
| GET | `/api/settings/auto-plan` | Read auto-plan toggle |
| POST | `/api/settings/auto-plan` | Set auto-plan toggle |
| GET | `/api/settings/auto-promote` | Read auto-promote toggle |
| POST | `/api/settings/auto-promote` | Set auto-promote toggle |

**Total: 116 documented routes**
