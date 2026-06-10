# CurlyOS Core

> **The cognitive architecture from [HitenOS](https://github.com/hitendev/hitenos-architecture) — implemented as a standalone Python service with a Hermes Agent memory plugin.**

CurlyOS Core is a bi-temporal knowledge graph + multi-tier memory system that gives AI agents persistent, queryable, self-evolving memory. It replaces the default flat-file memory in [Hermes Agent](https://hermes-agent.nousresearch.com) with a real knowledge store backed by PostgreSQL+pgvector and Redis.

## Architecture

Four memory tiers, each with a single authoritative store:

| Tier | Store | Pattern |
|------|-------|---------|
| **Working** | Redis `wm:{session}:scratch` | Volatile, TTL 2h |
| **Episodic** | Postgres `episodes` | Append-only provenance ground-truth |
| **Semantic** | Postgres `memories` + pgvector HNSW | Distilled bi-temporal facts |
| **Procedural** | Postgres `skills` | Versioned skills/workflows |

Write discipline: **add-mostly hot path + async consolidation ("sleep") job**.

### Service Map

```
~/curlyos-core/
├── api_server.py          # FastAPI app — REST API for all CurlyOS data (port 8643)
├── start_api_server.py    # Daemon start/stop helper
├── curlyos_setup.py       # Interactive setup wizard + health checks + migrations
├── pyproject.toml         # Python project config (hatchling build)
│
├── memory/                # 4-tier memory: governance, consolidation, retrieval
│   ├── governance/        # Episode recording, fact CRUD, invalidation
│   ├── consolidation/     # DEDUP, CONFLICT-RESOLVE, SUMMARIZE, DECAY, RECOMBINE
│   ├── retrieval/         # Hybrid search: BM25 + vector + graph expansion
│   └── stores/            # DDL, Postgres connection pool, embedding backends
│
├── knowledge/             # Entity extraction, resolution, graph projection
│   ├── extraction/        # NER + relation extraction from episodes
│   ├── resolution/        # Entity dedup + merging
│   └── graph/             # Knowledge graph tables + queries
│
├── identity/              # Bi-temporal self-model (identity_facts triples)
│   ├── pipeline/          # Identity fact extraction from episodes
│   └── predicates/        # Core identity attributes (name, role, preferences)
│
├── cognition/             # Meta-cognition layers
│   ├── attention/         # Attention tracking + focus management
│   ├── introspection/     # Self-monitoring + decision audits
│   ├── meta/              # Meta-cognitive assessments
│   ├── narrative/         # Life chapters + themes + narrative construction
│   └── reflection/        # Periodic reflection reports + principle distillation
│
├── studio/                # Infinite canvas of ideas
│   ├── sketches/          # Raw idea capture
│   └── graduation/        # Sketch → project promotion pipeline
│
├── simulation/            # World modeling
│   ├── assumptions/       # Tracked assumptions
│   ├── scenarios/         # What-if scenario definitions
│   └── outcomes/          # Simulation results
│
├── evaluation/            # Quality assurance
│   ├── gate/              # Gate checks (go/no-go decisions)
│   ├── replay/            # Episode replay for testing
│   └── scorers/           # Evaluation metrics
│
├── workspace/             # Project/task/goal containers
│   ├── goals/             # Goal tracking
│   └── tasks/             # Task management
│
├── shared/                # Cross-cutting contracts
│   ├── embeddings/        # Embedder interface (fake, bge-m3, openai)
│   ├── events/            # CloudEvents + NATS publishing
│   └── types/             # Shared Pydantic types
│
├── hermes_integration/    # Hermes MemoryProvider plugin
│   ├── __init__.py        # Plugin doc + imports
│   ├── plugin.yaml        # Plugin manifest
│   └── provider.py        # MemoryProvider implementation (tool schemas, hooks)
│
├── deploy/                # Docker infrastructure
│   ├── docker-compose.yml # Postgres+pgvector + Redis stack (named volumes)
│   ├── .env               # POSTGRES_PASSWORD (gitignored, chmod 600)
│   ├── migrate-to-named-volumes.sh  # Migration from anonymous → named volumes
│   └── backups/           # pg_dump snapshots (gitignored)
│
└── tests/                 # Pytest suite
```

## Key Concepts

- **Bi-temporal validity**: Every fact carries `valid_from`/`valid_to` + `ingested_at` — time-travel queries work
- **Invalidate-not-delete**: Superseded facts get `valid_to=now()`, never deleted — provenance preserved
- **Episodic provenance**: Every fact MUST cite a `source_episode_id` — no ungrounded facts
- **Epistemic axis**: `seed → conjecture → hypothesis → belief → canonical` — confidence cross-cuts all tiers
- **Event sourcing**: All writes emit CloudEvents to `HITENOS_MEMORY` — projections are rebuildable

## Prerequisites

| Component | Minimum Version | Purpose |
|-----------|----------------|---------|
| Python | 3.11+ | Runtime |
| PostgreSQL | 16.4+ | Episodic + semantic memory store |
| pgvector | 0.7.4+ | Vector similarity search in Postgres |
| Redis | 7.4+ | Working memory + cache + locks |
| uv (optional) | any | Fast Python package manager |
| Docker + Compose | any | Running Postgres+Redis locally |

## Quick Start

### 1. Clone and Install

```bash
git clone git@github.com:hitendev/curlyos-core.git
cd curlyos-core

# Create venv (uses uv if available, falls back to venv)
python3 -m venv .venv
source .venv/bin/activate

# Install with all optional dependencies
pip install -e ".[all]"

# Or with uv:
uv pip install -e ".[all]"
```

### 2. Start Data Stores

```bash
cd deploy

# Set Postgres password
cp .env.example .env   # then edit .env with your password
# Or just: echo 'POSTGRES_PASSWORD=yourpassword' > .env

# Create named volumes and start
docker compose up -d

# Verify
docker compose ps
docker exec curlyos-pg psql -U curlyos -d curlyos -c "SELECT 1"
docker exec curlyos-redis redis-cli ping
```

### 3. Run the Setup Wizard

```bash
# From the repo root:
source .venv/bin/activate
python3 curlyos_setup.py
```

This interactive wizard will:
1. Test your PostgreSQL connection
2. Test your Redis connection
3. Apply all DDL migrations via `python3 migrate.py` — an ordered runner that applies `migrations/*.sql` files and tracks each one in a `schema_migrations` ledger; `curlyos_setup.py --migrate` calls the same runner
4. Configure your embedder (fake for testing, bge-m3 for local, OpenAI for API)
5. Write `~/.hermes/curlyos.yaml` and update `~/.hermes/.env`

### 4. Verify Installation

```bash
python3 curlyos_setup.py --check
```

Expected output:
```json
{
  "postgres": {"status": "ok", "detail": "Postgres OK (...)"},
  "redis": {"status": "ok", "detail": "Redis OK (...)"},
  "tables": {
    "episodes": 0,
    "memories": 0,
    ...
  }
}
```

### 5. Start the API Server

```bash
# Foreground (dev):
source .venv/bin/activate
uvicorn api_server:app --host 127.0.0.1 --port 8643 --reload

# Background daemon (production):
python3 start_api_server.py
python3 start_api_server.py --status   # check it's running
python3 start_api_server.py --stop     # stop it
```

The API is available at `http://127.0.0.1:8643`.

### 6. Test the API

```bash
# Health check
curl http://127.0.0.1:8643/api/health | python3 -m json.tool

# Stats
curl http://127.0.0.1:8643/api/stats | python3 -m json.tool
```

## Hermes Agent Integration

CurlyOS Core includes a **MemoryProvider plugin** that replaces Hermes's default flat-file memory with the full CurlyOS knowledge graph. Once integrated, Hermes automatically records every conversation turn as an episode, injects relevant memories into each turn's context, and gives you 5 new tools for direct knowledge graph interaction.

### Option A: Plugin Install (Recommended)

```bash
# 1. Copy the plugin to Hermes's plugin directory
cp -r hermes_integration/ ~/.hermes/plugins/curlyos/

# 2. Add to ~/.hermes/config.yaml:
# plugins:
#   enabled:
#     - curlyos
# memory:
#   provider: curlyos

# 3. Or use the Hermes CLI:
hermes config set plugins.enabled '["curlyos"]'
hermes config set memory.provider curlyos
```

### Option B: Manual Config Edit

Edit `~/.hermes/config.yaml`:

```yaml
memory:
  provider: curlyos

plugins:
  enabled:
    - curlyos

# Make sure these env vars are in ~/.hermes/.env:
# CURLYOS_DATABASE_URL=postgresql://curlyos:YOURPASSWORD@localhost:54321/curlyos
# CURLYOS_REDIS_URL=redis://localhost:6379/0
```

### Verify Hermes Integration

Start a Hermes session and check the system prompt — you should see the `curlyos_*` tools listed:

```bash
hermes
# In the session, the system prompt should include:
# curlyos_recall, curlyos_add_fact, curlyos_add_note, curlyos_invalidate, curlyos_identity
```

### What the Plugin Does

| Hook | What Happens |
|------|-------------|
| `prefetch()` | Before each turn, recalls relevant memories and injects them into the system prompt |
| `sync_turn()` | After each turn, records it as an episode linked to the conversation |
| `on_session_end()` | Summarizes the session, extracts key facts, proposes identity updates |
| `on_pre_compress()` | Before context compression, saves insights from messages about to be discarded |
| `on_memory_write()` | When Hermes's built-in memory tool fires, mirrors the write to CurlyOS |

### Available Tools (in Hermes)

| Tool | Description |
|------|-------------|
| `curlyos_recall` | Semantic + graph retrieval over the knowledge base |
| `curlyos_add_fact` | Store a durable, grounded fact with bi-temporal validity |
| `curlyos_add_note` | Store a longer note / reference material |
| `curlyos_invalidate` | Soft-invalidate outdated facts (never deleted) |
| `curlyos_identity` | Query Hiten's stable self-model |

## API Reference

### Health & Stats

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/health` | GET | Database + Redis connectivity + embedder status |
| `/api/stats` | GET | Row counts for all tables |

### Memory

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/episodes` | GET | List episodes (paginated) |
| `/api/episodes` | POST | Record a new episode |
| `/api/memories` | GET | List memories (paginated) |
| `/api/search?q=&mode=&limit=` | GET | Full-text search over memories (BM25) |
| `/api/recall` | POST | Hybrid recall (BM25 + vector + graph) |
| `/api/memories` | POST | Store a new memory/fact |
| `/api/memories/{id}/invalidate` | POST | Invalidate a memory (soft-invalidation; records are never deleted) |

### Identity

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/identity` | GET | Get all identity facts |
| `/api/identity` | POST | Add/update identity facts |

### Knowledge Graph

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/graph` | GET | List knowledge graph entities |
| `/api/graph/{id}/expand?k=` | GET | Expand a node's neighbourhood (k hops) |

## Configuration

CurlyOS reads configuration from (in order of priority):

1. Environment variables (`CURLYOS_DATABASE_URL`, `CURLYOS_REDIS_URL`)
2. `~/.hermes/curlyos.yaml`
3. `~/.hermes/.env`

### `~/.hermes/curlyos.yaml`

```yaml
database_url: "postgresql://curlyos:PASSWORD@localhost:54321/curlyos"
redis_url: "redis://localhost:6379/0"
embedder: "bge-m3"   # fake | bge-m3 | openai
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `CURLYOS_DATABASE_URL` | PostgreSQL DSN | (required) |
| `CURLYOS_REDIS_URL` | Redis URL | (required) |
| `CURLYOS_API_PORT` | API server port | `8643` |
| `CURLYOS_SCOPE` | Default scope for facts | `user:usr_hiten` |

## Backups

```bash
# Create a logical backup
docker exec curlyos-pg pg_dump -U curlyos -d curlyos -Fc > backup.dump

# Restore from backup
cat backup.dump | docker exec -i curlyos-pg pg_restore -U curlyos -d curlyos --clean --if-exists

# Set up daily backups via cron
0 3 * * * docker exec curlyos-pg pg_dump -U curlyos -d curlyos -Fc > ~/curlyos-core/deploy/backups/curlyos-$(date +\%F).dump
```

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/ -q

# Run tests with parallelism
pytest tests/ -q -n auto

# Lint
ruff check .

# Type check
mypy memory/ knowledge/ identity/ cognition/ shared/
```

## Data Safety

- Named Docker volumes (`curlyos_pgdata`, `curlyos_redisdata`) survive `docker compose down`
- Even `docker compose down -v` preserves them (volumes are `external: true`)
- Facts are **never deleted** — only invalidated (`valid_to` set to now)
- Append-only episode log — full provenance chain

## License

MIT — see [LICENSE](LICENSE).
