# CurlyOS Core

> **A cognitive operating system for AI agents — bi-temporal memory, a self-organizing knowledge graph, a stable identity model, and a metacognition layer that reflects on its own thinking.**

Most AI "memory" is a flat file or a pile of vectors: you dump text in, you cosine-search it back out, and nothing ever connects, ages, or changes its mind. CurlyOS Core is built on the opposite premise — that durable intelligence needs **structure, time, provenance, and self-revision**, not just recall.

It is a standalone Python service (FastAPI, PostgreSQL + pgvector, Redis) that gives an agent a real cognitive substrate: every experience is recorded with provenance, distilled into time-aware facts, projected into a connected knowledge graph, folded into a stable self-model, and periodically re-examined by a reflection loop that distills principles, surfaces life themes, and flags its own stale assumptions. It ships as a drop-in [Hermes Agent](https://hermes-agent.nousresearch.com) memory plugin and powers a companion web UI for exploring the whole mind visually.

---

## Why CurlyOS Core is different

| | Flat-file / scratchpad memory | Naive RAG (vector store) | **CurlyOS Core** |
|---|---|---|---|
| **Structure** | Unstructured notes | Opaque chunks | Episodes → facts → **entity/relation graph** |
| **Time** | Overwrites or appends | No notion of time | **Bi-temporal** (`valid_from`/`valid_to` + `ingested_at`) — time-travel queries |
| **Truth changes** | Lost or duplicated | Stale chunks linger | **Invalidate-not-delete** — superseded facts kept with full history |
| **Provenance** | None | Chunk → source, maybe | **Every fact cites a `source_episode_id`** — no ungrounded claims |
| **Confidence** | None | None | **Epistemic axis** — `hypothesis` / `belief` / `canonical` cross-cuts every fact |
| **Retrieval** | grep | Vector top-k | **Hybrid**: BM25 + dense vectors + **graph expansion** + rerank |
| **Self-revision** | None | None | **Consolidation + reflection** — dedup, conflict-resolve, distill principles, regenerate themes |
| **Identity** | None | None | A **stable self-model** that survives context resets |

The short version: a vector store remembers *what was said*. CurlyOS Core remembers *what is true, when it was true, why you believe it, and how confident you are* — and it keeps cleaning that up while you sleep.

---

## What it does

CurlyOS Core is a layered stack. Each layer is a real, queryable subsystem with its own tables and REST endpoints.

### 🧠 Multi-tier memory
Four tiers, each with a single authoritative store and a clear write discipline (fast add on the hot path; heavy work deferred to an async "sleep" consolidation job).

| Tier | Store | Role |
|------|-------|------|
| **Working** | Redis `wm:{session}` | Volatile scratch, TTL 2h |
| **Episodic** | Postgres `episodes` | Append-only, ground-truth provenance |
| **Semantic** | Postgres `memories` + pgvector HNSW | Distilled, bi-temporal facts |
| **Procedural** | Postgres `skills` | Versioned skills / workflows |

### 🕸️ Self-organizing knowledge graph
An LLM extracts `(subject, predicate, object)` triples from episodes; an entity-resolution stage dedups and merges them; the result is a typed graph (Person, Project, Tool, Concept, Health, …) with bi-temporal edges. A densification pass adds *reversible, never-invented* inferred edges (embedding similarity + co-occurrence) so the graph stays one connected, navigable structure instead of a dust cloud of islands.

### 🪪 Identity
A bi-temporal self-model (`identity_facts`) — name, role, preferences, values — distilled from reflection and resilient to context compression. The agent can answer "who am I and what do I care about" consistently across sessions.

### 🔍 Metacognition (the part that thinks about thinking)
This is what makes it an *operating system* rather than a database:
- **Reflection** — periodic weekly/monthly passes that read the clean graph and produce findings, **distilled principles**, and identity/goal updates.
- **Narrative** — surfaces recurring **themes** and composes **life chapters** from your own episodes (grounded only in real material, never hallucinated).
- **Attention** — tracks focus areas, neglected entities, cognitive load, and **alignment gaps** between stated goals and actual activity.
- **Meta** — generates working **assumptions** and **mental models**, audits past decisions.

### 🎨 Studio · 🌍 Simulation · ✅ Evaluation · 🎯 Goals
An infinite canvas for raw ideas that can *graduate* into projects; a world-modeling layer for tracked assumptions and what-if scenarios; gate-checks and scorers for quality; and goal/task containers that the reflection loop keeps honest.

### At a glance (a live instance)

```
52 REST endpoints   ·   43 Postgres tables
~1.6k episodes  →  ~20k facts  →  3.2k entities / 8.7k edges  →  37 identity facts
single fully-connected knowledge graph · 0 orphans
```

---

## Architecture

### Key concepts

- **Bi-temporal validity** — every fact carries `valid_from` / `valid_to` + `ingested_at`, so you can ask "what did I believe on date X" and "what is true now" independently.
- **Invalidate-not-delete** — superseded facts get `valid_to = now()`, never a `DELETE`. History and provenance are preserved; every change is reversible.
- **Episodic provenance** — every distilled fact MUST cite a `source_episode_id`. No fact exists without the experience that produced it.
- **Epistemic axis** — `hypothesis → belief → canonical` (plus producer-specific `seed`/`conjecture`/`possible_world`). Confidence is a first-class dimension, classified by an LLM at capture time.
- **Capture hygiene** — ingestion strips harness/tooling scaffolding (system reminders, task notifications, command wrappers) so only genuine content becomes memory.
- **Event sourcing** — writes emit CloudEvents; projections (like the graph) are rebuildable from the episodic log.

### LLM backend — tiered routing
Every LLM call goes through a **failover chain** (`shared/models.py`), and work is routed to one of three **task tiers** so the right model does the right job:

| Tier | Used by | Default backend |
|------|---------|-----------------|
| **fast** | per-ingest epistemic classification, KG extraction, memory distillation (high-volume) | OmniRoute / cheap fast model (`CURLYOS_LLM_*`) |
| **agentic** | the orchestrator's ReAct runner + agent runs | `CURLYOS_AGENTIC_*` (e.g. Azure Kimi) |
| **deep** | heavy "thinking" — reflection, meta-cognition, narrative | `CURLYOS_DEEP_*` (e.g. Azure gpt-oss-120b) |

Each tier resolves its own `base_url` / key / model chain and degrades gracefully to the fast config when a tier is unset. Per-tier usage (calls, errors, fallbacks, latency) is exposed live at `/api/observability/llm`.

### Service map

```
~/curlyos-core/
├── api_server.py          # FastAPI app — REST API for all CurlyOS data (port 8643)
├── start_api_server.py    # Daemon start/stop helper
├── curlyos_setup.py       # Interactive setup wizard + health checks + migrations
├── migrate.py             # Ordered SQL migration runner (schema_migrations ledger)
│
├── memory/                # 4-tier memory
│   ├── governance/        # Episode recording, fact CRUD, invalidation
│   ├── consolidation/     # DEDUP · CONFLICT-RESOLVE · SUMMARIZE · DECAY · RECOMBINE ("sleep")
│   ├── retrieval/         # Hybrid recall: BM25 + vector + graph expansion + rerank
│   └── stores/            # DDL, Postgres pool, embedding backends
│
├── knowledge/             # Entity + relation extraction → resolution → graph projection
├── identity/              # Bi-temporal self-model (identity_facts triples)
├── cognition/             # Metacognition: attention · introspection · meta · narrative · reflection
├── studio/                # Idea canvas: sketches → graduation pipeline
├── simulation/            # Assumptions · scenarios · outcomes
├── evaluation/            # Gate checks · replay · scorers
├── goals/  workspace/     # Goal + task/project containers
│
├── shared/                # Embedders, events, LLM model chain, epistemic classifier
├── hermes_integration/    # Hermes MemoryProvider plugin (hooks + tool schemas)
└── deploy/                # docker-compose (Postgres+pgvector + Redis), ops scripts, migrations
```

A separate Next.js app (`curly-os`) is the optional companion UI — it renders the knowledge graph, memory, episodes, identity, cognition, journal, and studio through this API.

---

## Prerequisites

| Component | Minimum | Purpose |
|-----------|---------|---------|
| Python | 3.11+ | Runtime |
| PostgreSQL | 16.4+ | Episodic + semantic store |
| pgvector | 0.7.4+ | Vector similarity in Postgres |
| Redis | 7.4+ | Working memory + cache + locks |
| Docker + Compose | any | Local Postgres + Redis stack |
| uv | optional | Fast Python package manager |

## Quick Start

### 1. Clone and install

```bash
git clone https://github.com/hitensaxena/CurlyOS-Core.git
cd CurlyOS-Core

python3 -m venv .venv
source .venv/bin/activate

# Install with all optional dependencies (postgres, redis, embeddings, llm, …)
pip install -e ".[all]"      # or:  uv pip install -e ".[all]"
```

### 2. Start the data stores

```bash
cd deploy
cp .env.example .env          # then set POSTGRES_PASSWORD
docker compose up -d

# Verify
docker compose ps
docker exec curlyos-pg psql -U curlyos -d curlyos -c "SELECT 1"
docker exec curlyos-redis redis-cli ping
```

### 3. Run the setup wizard

```bash
cd ..
source .venv/bin/activate
python3 curlyos_setup.py
```

The wizard tests Postgres + Redis, applies all DDL migrations (`migrations/*.sql` via `migrate.py`, tracked in a `schema_migrations` ledger), configures your embedder (`fake` for tests, `bge-m3` local, or `openai`), and writes `~/.hermes/curlyos.yaml`.

### 4. Verify

```bash
python3 curlyos_setup.py --check
```

```json
{
  "postgres": {"status": "ok"},
  "redis":    {"status": "ok"},
  "tables":   {"episodes": 0, "memories": 0, "...": 0}
}
```

### 5. Start the API server

```bash
# Dev (foreground, auto-reload):
uvicorn api_server:app --host 127.0.0.1 --port 8643 --reload

# Production (background daemon):
python3 start_api_server.py
python3 start_api_server.py --status
python3 start_api_server.py --stop
```

API is at `http://127.0.0.1:8643`.

### 6. Smoke test

```bash
curl http://127.0.0.1:8643/api/health | python3 -m json.tool
curl http://127.0.0.1:8643/api/stats  | python3 -m json.tool
```

---

## Hermes Agent integration

CurlyOS ships a **MemoryProvider plugin** that replaces Hermes's flat-file memory with the full knowledge graph. Once enabled, Hermes records every turn as an episode, injects relevant memories into context, and gains five direct tools.

```bash
# Copy the plugin and enable it
cp -r hermes_integration/ ~/.hermes/plugins/curlyos/
hermes config set plugins.enabled '["curlyos"]'
hermes config set memory.provider curlyos
# Ensure ~/.hermes/.env has CURLYOS_DATABASE_URL and CURLYOS_REDIS_URL
```

| Hook | What happens |
|------|-------------|
| `prefetch()` | Recall relevant memories → inject into the system prompt |
| `sync_turn()` | Record each turn as an episode (scaffolding stripped) |
| `on_session_end()` | Summarize session, extract facts, propose identity updates |
| `on_pre_compress()` | Save insights from messages about to be discarded |
| `on_memory_write()` | Mirror Hermes's built-in memory writes into CurlyOS |

| Tool | Description |
|------|-------------|
| `curlyos_recall` | Semantic + graph retrieval over the knowledge base |
| `curlyos_add_fact` | Store a durable, grounded, bi-temporal fact |
| `curlyos_add_note` | Store a longer note / reference |
| `curlyos_invalidate` | Soft-invalidate an outdated fact (never deleted) |
| `curlyos_identity` | Query the stable self-model |

---

## API reference (selected — 60+ endpoints total)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/health` · `/api/stats` | GET | Connectivity/embedder status · row counts |
| `/api/ingest` | POST | Record raw text → episode (+ memory, + async extraction); strips scaffolding |
| `/api/episodes` | GET/POST | Append-only provenance log |
| `/api/memories` | GET/POST | Distilled bi-temporal facts |
| `/api/memories/{id}/invalidate` | POST | Soft-invalidate (history preserved) |
| `/api/search?q=` | GET | Full-text (BM25) over facts |
| `/api/recall` | POST | Hybrid recall: BM25 + vector + graph + rerank |
| `/api/graph?limit=` | GET | Knowledge-graph nodes + edges (degree-ranked) |
| `/api/graph/{id}/expand?k=` | GET | Expand a node's neighbourhood (k hops) |
| `/api/identity` | GET/POST | Stable self-model |
| `/api/cognition/narrative/compose` | POST | Compose a grounded first-person narrative for a query |
| `/api/reflection/*` · `/api/meta/*` · `/api/attention/*` | POST | Run reflection, generate assumptions/models, scan attention |
| `/api/observability/llm` | GET | Per-tier LLM health: provider, model, calls, errors, fallbacks, latency |
| `/api/observability/recall` | GET | Recall throughput + cache hit-rate + cold/warm latency |
| `/api/observability/pipeline` | GET | Ingest pipeline backlog (embed/distill), ingest rate, KG size |
| `/api/observability/overview` | GET | One-call rollup (health + counts + LLM + recall + pipeline + scheduler) |
| `/api/settings` · `/api/settings/{key}` | GET/PUT | Typed, validated runtime knobs (cache TTL, ingest toggles, autonomy) |

### Observability & runtime settings

A lightweight in-process metrics layer (`shared/metrics.py`, since-boot counters) instruments the single LLM choke point (`FallbackClient`) and the recall path, surfaced through the `/api/observability/*` endpoints above — per-tier LLM usage, recall cache hit-rate, and the write→embed→distill→graph backlog.

Runtime behaviour is tunable **without a restart** via a typed settings registry (`shared/settings.py`). `GET /api/settings` lists every knob with its type/default/description; `PUT /api/settings/{key}` validates and applies it live. Wired knobs include `recall_cache_enabled`, `recall_cache_ttl_seconds`, `recall_fast_followups`, `epistemic_classify_enabled`, `kg_extraction_enabled`, plus the `auto_promote` / `auto_plan` autonomy toggles.

---

## Configuration

Resolved in priority order: **env vars → `~/.hermes/curlyos.yaml` → `~/.hermes/.env`**.

| Variable | Description | Default |
|----------|-------------|---------|
| `CURLYOS_DATABASE_URL` | PostgreSQL DSN | *(required)* |
| `CURLYOS_REDIS_URL` | Redis URL | *(required)* |
| `CURLYOS_API_PORT` | API server port | `8643` |
| `CURLYOS_SCOPE` | Default scope for facts | `user:usr_hiten` |
| `OPENROUTER_API_KEY` | LLM key for extraction/cognition | *(optional)* |
| `CURLYOS_LLM_BASE_URL` · `CURLYOS_LLM_API_KEY` · `CURLYOS_MODEL_CHAIN` | **fast** tier endpoint/key/chain | OpenRouter default |
| `CURLYOS_AGENTIC_BASE_URL` · `_API_KEY` · `_MODEL` | **agentic** tier (orchestrator/agents) | falls back to fast |
| `CURLYOS_DEEP_BASE_URL` · `_API_KEY` · `_MODEL` | **deep** tier (reflection/meta/narrative) | falls back to fast |
| `CURLYOS_EMBED_DEVICE` | Force embedder device: `cpu` (default) · `mps` · `cuda` | `cpu` |
| `REFRESH_BACKEND` | `chain` (OpenRouter) or `hermes` (Claude via hermes-bridge) | `chain` |

```yaml
# ~/.hermes/curlyos.yaml
database_url: "postgresql://curlyos:PASSWORD@localhost:54321/curlyos"
redis_url:    "redis://localhost:6379/0"
embedder:     "bge-m3"   # fake | bge-m3 | openai
```

---

## Backups

```bash
# Logical backup
docker exec curlyos-pg pg_dump -U curlyos -d curlyos -Fc > backup.dump

# Restore
cat backup.dump | docker exec -i curlyos-pg pg_restore -U curlyos -d curlyos --clean --if-exists

# Daily cron
0 3 * * * docker exec curlyos-pg pg_dump -U curlyos -d curlyos -Fc > ~/curlyos-core/deploy/backups/curlyos-$(date +\%F).dump
```

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -q            # add -n auto for parallelism
ruff check .
mypy memory/ knowledge/ identity/ cognition/ shared/
```

## Data safety

- Named Docker volumes (`curlyos_pgdata`, `curlyos_redisdata`) survive `docker compose down`.
- Facts are **never deleted** — only invalidated (`valid_to = now()`).
- Append-only episode log — full provenance chain, rebuildable projections.

## License

MIT — see [LICENSE](LICENSE).
