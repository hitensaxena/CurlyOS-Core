---
title: Deployment & Operations
description: End-to-end guide for deploying, running, maintaining, and recovering CurlyOS Core (FastAPI + Postgres/pgvector + Redis).
---

CurlyOS Core is a Python/FastAPI service that depends on Postgres 16 with the pgvector extension and Redis 7. The canonical deployment topology runs all three processes on a single host: Docker Compose manages the data stores, and the API server runs as a supervised daemon (systemd or `start_api_server.py`). The `deploy/` directory contains every script needed to bootstrap, maintain, and recover the system.

## Prerequisites

| Requirement | Minimum version | Notes |
|-------------|----------------|-------|
| Python | 3.11+ | Managed by `uv`; a `.python-version` file pins the exact version |
| Postgres | 16.4+ | Must be the `pgvector/pgvector:pg16` image when running via Docker |
| pgvector | 0.7.4+ | Bundled in the image above; required for `vector` column type and ANN indexes |
| Redis | 7.4+ | Alpine image used in Compose; plain `redis:7.4-alpine` |
| Docker | Any recent stable | Used to run Postgres and Redis; OrbStack works on macOS |
| uv | Latest | Package manager; install with `curl -LsSf https://astral.sh/uv/install.sh | sh` |

> **OrbStack dependency (macOS):** The API server crash-loops if Docker (and therefore Postgres/Redis) is not running. After a reboot, start OrbStack (or Docker Desktop) before the API server. See [Troubleshooting](#troubleshooting) for the recovery sequence.

## Local stack (Docker Compose)

All data-store config lives in `deploy/`. The project name is pinned to `curlyos` so volume names are stable regardless of the working directory.

### Services

| Service | Container name | Image | Host port | Internal port | Volume |
|---------|---------------|-------|-----------|---------------|--------|
| Postgres + pgvector | `curlyos-pg` | `pgvector/pgvector:pg16` | `127.0.0.1:54321` | `5432` | `curlyos_pgdata` |
| Redis | `curlyos-redis` | `redis:7.4-alpine` | `127.0.0.1:6379` | `6379` | `curlyos_redisdata` |

Memory limits: Postgres is capped at 1500 MB; Redis at 512 MB. Both containers have `restart: unless-stopped` and healthchecks.

### Named volumes

Both volumes are declared `external: true`. Compose references them but **never creates or deletes them**. This means `docker compose down -v` cannot wipe the knowledge graph. Volumes must be created out-of-band (done once by `migrate-to-named-volumes.sh`).

```
curlyos_pgdata     — Postgres data directory (/var/lib/postgresql/data)
curlyos_redisdata  — Redis RDB snapshot (/data)
```

### Environment

Copy `.env.example` to `deploy/.env` and set a real password:

```bash
cp deploy/.env.example deploy/.env
chmod 600 deploy/.env
# Edit: POSTGRES_PASSWORD=<strong-random-password>
```

The file is gitignored. The API server connects via:

```
CURLYOS_DATABASE_URL=postgresql://curlyos:<password>@localhost:54321/curlyos
CURLYOS_REDIS_URL=redis://localhost:6379/0
```

### Bringing the stack up

```bash
# First-time only: create named volumes (or run migrate-to-named-volumes.sh)
docker volume create curlyos_pgdata
docker volume create curlyos_redisdata

cd ~/curlyos-core/deploy

docker compose up -d        # start detached
docker compose ps           # status + health
docker compose logs -f      # tail logs
docker compose down         # stop (named volumes PERSIST)
```

> `docker compose down -v` is safe — because volumes are `external: true` they survive even this flag. To permanently delete data you must explicitly run `docker volume rm curlyos_pgdata curlyos_redisdata`.

### Redis durability note

Redis uses RDB snapshotting by default (`save 3600 1`, `save 300 100`, `save 60 10000`). To switch to append-only durability for the consolidation queue, change the `command:` in `deploy/docker-compose.yml` to:

```yaml
command: ["redis-server", "--appendonly", "yes"]
```

Port 6379 may conflict with other services (authentik, Caddy) on the same host. If there is a bind conflict on reboot, remap the host-side port in `docker-compose.yml` **and** update `CURLYOS_REDIS_URL` in the environment.

## Setup wizard (`curlyos_setup.py`)

The setup wizard at `curlyos_setup.py` (invoked as a module) handles first-run configuration and health verification.

### Flags

```bash
python3 -m curlyos.setup           # Interactive setup (default)
python3 -m curlyos.setup --check   # Health check — exits 0 if Postgres OK, 1 otherwise
python3 -m curlyos.setup --status  # Print current config as JSON
python3 -m curlyos.setup --migrate # Apply migrations only (no interactive prompts)
```

### Interactive setup phases

1. **Postgres connection** — prompts for a DSN (default: `postgresql://curlyos:***@localhost:54321/curlyos`), tests the connection with `pg_isready`, then offers to apply all pending DDL migrations via `migrate.py`.
2. **Redis connection** — prompts for a Redis URL (default: `redis://localhost:6379/0`), verifies with a `PING`.
3. **Embedder selection** — choose `fake` (no-op, for development), `bge-m3` (local BAAI model, ~1 GB RAM), or `openai` (requires `OPENAI_API_KEY`).
4. **Config write** — persists all settings to `~/.hermes/curlyos.yaml` (the canonical runtime config file). It also writes `CURLYOS_REDIS_URL` into the project `.env`.

### `--check` in CI / health probes

`--check` runs the full `health_check()` function (tests Postgres and Redis), prints JSON results, and exits with a non-zero code if Postgres is not reachable. Suitable as a readiness probe or post-deploy smoke test.

## Running the API server

### Development (with auto-reload)

```bash
cd ~/curlyos-core
uv run uvicorn api_server:app --host 127.0.0.1 --port 8643 --reload
```

The `--reload` flag watches for source changes and restarts the worker. Do not use `--reload` in production.

### Production daemon (`start_api_server.py`)

`start_api_server.py` manages a background `uvicorn` process via a PID file at `/tmp/curlyos-api.pid`.

```bash
python3 start_api_server.py          # start (default command)
python3 start_api_server.py --status # print PID + /api/health response
python3 start_api_server.py --stop   # send SIGTERM to the daemon
```

The daemon binds to `127.0.0.1:8643`. The port can be overridden with the environment variable `CURLYOS_API_PORT`. On start, it polls `/api/health` for up to ~5 seconds and prints the PID if the server responds.

### systemd service + override

A systemd unit can supervise the daemon. The drop-in `deploy/curlyos-api.override.conf` adds memory limits and restart policy:

```
MemoryHigh=2200M   — kernel throttles if RSS reaches 2.2 GB
MemoryMax=2800M    — OOM-kills just the API (not Postgres/Redis) at 2.8 GB
Restart=on-failure
RestartSec=5
```

Normal RSS with the `bge-m3` model loaded is ~1.0–1.1 GB; the limits give generous headroom.

**Install the override:**

```bash
sudo mkdir -p /etc/systemd/system/curlyos-api.service.d
sudo cp deploy/curlyos-api.override.conf \
       /etc/systemd/system/curlyos-api.service.d/override.conf
sudo systemctl daemon-reload
sudo systemctl restart curlyos-api
```

## CI (GitHub Actions)

The workflow at `.github/workflows/ci.yml` runs on every push to `main` and on all pull requests.

**Job: `test` (ubuntu-latest)**

| Step | Command |
|------|---------|
| Checkout | `actions/checkout@v4` |
| Install uv | `astral-sh/setup-uv@v5` |
| Sync dependencies | `uv sync --extra dev --extra postgres --extra redis` |
| Lint | `uv run ruff check api_server.py` |
| Tests | `uv run pytest -q` |

There are **no service containers** in CI. Tests mock their DB pools with `AsyncMock`, so no live Postgres or Redis is required. Heavy extras (embeddings/torch, neo4j, nats) are deliberately excluded from the CI sync.

## Backups and restore

### `backup.sh` (recommended nightly cron)

`deploy/backup.sh` creates:
- A `pg_dump` (`-Fc` custom format) of the pgvector store.
- A tarball of `~/mind` (graph.sqlite + ChromaDB).

Rotation: 7 daily backups and 4 Sunday weekly backups are kept.

**Install as a cron:**

```bash
# Example: run at 03:30 daily
30 3 * * * /bin/bash ~/curlyos-core/deploy/backup.sh
```

### Manual pg_dump

```bash
docker exec curlyos-pg pg_dump -U curlyos -d curlyos -Fc \
  > ~/curlyos-core/deploy/backups/curlyos-$(date +%F).dump
```

### Restore into a running stack

```bash
docker compose up -d
cat backups/curlyos-YYYY-MM-DD.dump | \
  docker exec -i curlyos-pg pg_restore -U curlyos -d curlyos --clean --if-exists
```

### Restore drill procedure

Run after any schema change to verify recoverability (last verified 2026-06-10, PASSED):

1. `CREATE DATABASE curlyos_scratch` (drop first if present).
2. `gunzip -c deploy/backups/daily/curlyos-<date>.sql.gz | psql "$SCRATCH_DSN"` — one `ERROR: unrecognized configuration parameter "transaction_timeout"` is expected noise (pg17 client dump preamble vs pg16 server) and harmless.
3. `.venv/bin/python3 migrate.py --dsn "$SCRATCH_DSN"` — applies any migrations the dump predates; no-ops the rest.
4. Verify row counts on `memories`/`episodes`, check `schema_migrations` lists all migrations, run smoketests.
5. `DROP DATABASE curlyos_scratch`.

### Rollback to pre-migration containers

```bash
docker compose -p curlyos down
docker rename curlyos-pg-old curlyos-pg && docker start curlyos-pg
docker rename curlyos-redis-old curlyos-redis && docker start curlyos-redis
```

> **Open item:** All backups currently share the database's disk. An off-host encrypted backup copy (e.g. restic/age to S3 or a second box) has not been configured yet. This is a known gap requiring a decision on destination.

## Operational scripts

All scripts live in `deploy/`. Run them from the repo root with the environment sourced (`. ./.env` or `set -a; . ./.env; set +a`), except where noted.

### Embedding and backfill

| Script | What it does | When to run |
|--------|-------------|-------------|
| `embed_memories.py` | Embeds valid memories that have no vector yet using bge-m3 (local). | After any bulk insert (e.g. `refine_blob_memories.py`) to make new facts dense-recallable without waiting for the startup sweep. |
| `embed_mind_priority.py` | Targeted, resumable embed of high-value `~/mind` chunk-memories (life narrative, journals, etc.) using local bge-m3. Skips the 18k ChatGPT-archive chunks. | After importing the `~/mind` vault when CPU-bound embedding needs to prioritize high-value content. |
| `load_archive_vectors.py` | Loads `archive_vectors.tsv` (`<mem_id>\t[<float>,...]`, bge-m3 1024-dim from the AWS box) into pgvector via a temp-table COPY + UPDATE. Idempotent. | After an AWS embedding offload job completes and the TSV has been downloaded locally. |
| `backfill_kg_extract.py` | Embeds entities missing vectors and re-extracts graph edges from episodes that never produced them, using the fixed deduplication resolution path. | After schema or extraction logic changes leave the KG with gaps. |

### Knowledge-graph maintenance

| Script | What it does | When to run |
|--------|-------------|-------------|
| `densify_kg.py` | Adds cross-links between related entities using deterministic cosine similarity, turning a Hiten-centred star into a properly connected graph. | After a large ingest leaves many degree-1 leaf nodes or orphans. |
| `bridge_kg_components.py` | Connects remaining disconnected island clusters into the main component so the KG is a single connected whole. | After `densify_kg.py` when a handful of small clusters remain outside the main component. |
| `backfill_kg_merge.sql` | First merge pass: folds duplicate `knowledge_entities` (same scope + lowercased name) into the oldest canonical node, repoints edges, drops self-loops and parallel edges. Bi-temporal soft-delete (`valid_to`). | One-off cleanup after initial bulk import or entity extractor changes. |
| `backfill_kg_merge2.sql` | Second merge pass: folds surface-form variants and explicit alias merges; drops chat-role/pronoun noise. Bi-temporal soft-delete. | After `backfill_kg_merge.sql` when surface-form duplicates remain. |
| `backfill_kg_merge3.py` | Third merge pass: folds `user` into `Hiten` (now carrying real edges), folds additional variants, dedupes self-loops and parallel edges. | After the second SQL pass when `user` entity still appears separately. |

### Cognition refresh

| Script | What it does | When to run |
|--------|-------------|-------------|
| `refresh_cognition.py` | Clears stale cognition outputs and re-runs reflection (identity/goal sync), narrative (themes + chapters), and attention, using the API server's own helpers so the output matches endpoint behavior exactly. | After major KG or memory changes to bring derived cognition in sync with the new data. |
| `clean_cognition.sql` | Soft-deletes stale themes, life chapters, and identity facts (`valid_to`); hard-deletes reflection reports. Leaves identity occupation conflicts flagged for human review. | Before running `refresh_cognition.py` to avoid double-counting stale entries. |
| `clean_memory_episodes.sql` | Removes test/dev junk, brain-stub docs, "Session ended" lifecycle markers; deduplicates episodes and memory statements; fixes malformed `kind` values. Episodes are hard-deleted; FK refs are repointed or cascaded. Episodes with real downstream data (identity facts, goals, decisions, agent runs) are protected. | One-off cleanup after development sessions pollute the store. |
| `consolidate_principles.py` | Consolidates redundant principles into a canonical set via hermes-bridge (Claude). Merges near-duplicates into crisp, well-domained statements; supersedes old ones with `valid_to`. | After the distiller produces many rephrased duplicates of the same core principles. |
| `remirror_principles.py` | Invalidates stale principle-mirror memories and re-mirrors the current canonical principles as fresh atomic, embedded memories with provenance. | After `consolidate_principles.py` to keep the recall store in sync with the canonical principle set. |

### Entity and epistemic classification

| Script | What it does | When to run |
|--------|-------------|-------------|
| `classify_entities.py` | Classifies existing `knowledge_entities` into the type taxonomy (Person/Project/Tool/Skill/Concept/...) via LLM, using relationship types as context hints. Set `ONLY_UNTYPED=1` to skip already-typed entities. | After a bulk import leaves entities with label `Entity` or `Other`. |
| `classify_entities_local.py` | Zero-shot entity typing via bge-m3 cosine similarity against per-label prototype embeddings — no API, no rate limits. Default mode validates LLM-typed entities; `--fix` mode applies corrections. | As a fast, offline validation pass after `classify_entities.py`. |
| `reclassify_entities.py` | Re-classifies and prunes the `Concept`/`Other` entity buckets via LLM. Generates an audit JSON in `deploy/` for review. | Periodically to reduce the catch-all buckets and improve KG precision. |
| `reclassify_epistemic.py` | Re-classifies existing memories into the epistemic spectrum (`canonical`/`belief`/`hypothesis`) via the configured model chain. Idempotent. | After bulk imports that leave all memories as flat `canonical`. |
| `refine_blob_memories.py` | Refines document-blob memories (long raw-doc dumps) into atomic facts via hermes-bridge (Claude Max). Each extracted fact becomes a proper atomic memory; embeddings are backfilled by a later pass or restart. | After `ingest_brain.py` or similar imports that produce oversized memory blobs. |

### AWS embedding offload

| Script | What it does | When to run |
|--------|-------------|-------------|
| `aws_embed_worker.py` | Runs **on the transient AWS box**. Reads `archive_chunks.jsonl` (one `{"id","text"}` per line), embeds with BAAI/bge-m3 (same model as curlyos-core), writes `archive_vectors.tsv`. GPU auto-detected; CPU fallback uses all cores. Resumable: skips IDs already in the output file. | Launched by `aws_ssm_embed.sh` on the remote box; not run locally. |
| `aws_ssm_embed.sh` | Runs on the AL2023 embed box via AWS SSM. Creates a fresh venv, runs `aws_embed_worker.py`, ships the output TSV to S3 bucket `curlyos-embed-627917840429-aps1`, uploads the log, and terminates the box. Sets a 60-minute safety shutdown timer. | Invoke via SSM to start an embedding job on a fresh spot instance. |
| `aws_ssm_resume.sh` | Kills the SSM-bound embed process, relaunches it **detached** (survives the SSM 1-hour timeout), resumes from work already done, uploads vectors + self-terminates the box on completion. Sets a 6-hour backstop shutdown timer. | When the initial SSM session times out during a long embed job. |

### Mind import

| Script | What it does | When to run |
|--------|-------------|-------------|
| `ingest_mind.py` | Ingests the full `~/mind` markdown vault into curlyos-core as episodes + chunked, locally-embedded memories, then optionally mines the knowledge graph via Claude Max hermes-bridge. Supersedes the older `import_mind.py` which only read `knowledge_triples.json`. | One-off or after a major vault update to bring the mind store into curlyos-core. |
| `ingest_brain.py` | Ingests the orphaned brain-service SQLite store (`brain.db` nodes) into curlyos-core as episodes + recallable memories, embeds them, and runs KG extraction via the fixed resolution path. Mirrors the `/api/ingest` pipeline but runs out-of-process. | One-off migration to absorb a legacy `brain.db` into the unified store. |

### Migration and volume ops

| Script | What it does | When to run |
|--------|-------------|-------------|
| `migrate-to-named-volumes.sh` | One-time migration from original anonymous Docker volumes into named volumes (`curlyos_pgdata`, `curlyos_redisdata`), with a `pg_dump` backup first. Keeps originals as `curlyos-pg-old` / `curlyos-redis-old` for rollback. | Run once when upgrading from the original bare `docker run` setup to the Compose stack. |
| `install-hermes-plugin.sh` | Deploys the CurlyOS Hermes plugin from its canonical source (`hermes_integration/`) into the live Hermes runtime (`~/.hermes/plugins/curlyos`). The repo is canonical; the installed copy is a build artifact — never edit it directly. | After any change to `hermes_integration/` to push the update into the live runtime. |

## AWS embedding offload

For large-scale embedding of the ChatGPT archive or similar corpora, curlyos-core offloads to a transient AWS EC2 instance:

1. **Prepare chunks locally:** produce `deploy/archive_chunks.jsonl` with one `{"id": "...", "text": "..."}` object per line.
2. **Launch:** send `deploy/aws_ssm_embed.sh` to the box via AWS SSM (`aws ssm send-command ...`). The script creates a venv, installs torch + sentence-transformers, and starts `aws_embed_worker.py`.
3. **Resume if needed:** if the SSM session times out during a long job (the 1-hour SSM limit), run `aws_ssm_resume.sh` via a new SSM session. It kills the timeout-bound process, relaunches it detached with a 6-hour backstop timer, and resumes from the last completed ID in `archive_vectors.tsv`.
4. **Retrieve results:** the box uploads `archive_vectors.tsv` to S3 bucket `curlyos-embed-627917840429-aps1` on completion and then self-terminates.
5. **Load locally:** download the TSV and run `deploy/load_archive_vectors.py /path/archive_vectors.tsv` to bulk-update `memories.embedding` in Postgres.

The AWS box uses the same `BAAI/bge-m3` model (1024-dim vectors) as the local embedder, so the resulting vectors are compatible with the existing pgvector index without any re-indexing.

## Data safety

- **Named volumes with `external: true`:** Docker Compose cannot delete `curlyos_pgdata` or `curlyos_redisdata` automatically. An explicit `docker volume rm` command is required. This prevents accidental data loss from `docker compose down -v`.
- **Bi-temporal soft-delete:** KG merge scripts use `valid_to` timestamps rather than `DELETE` statements. Historical edges and entities remain queryable; only the current view changes.
- **Append-only episodes:** the episodes table is append-only by design. `clean_memory_episodes.sql` hard-deletes are a one-off maintenance operation, not routine behavior. Any episode with downstream artifacts (identity facts, goals, decisions, agent runs) is protected from deletion.
- **Idempotent scripts:** most `deploy/` scripts check for existing work before re-doing it (e.g. `load_archive_vectors.py` skips IDs already in the target column; `aws_embed_worker.py` skips IDs already in the output TSV).
- **Restore drill:** the restore procedure is documented above and should be run after every schema migration to verify recoverability.

## Troubleshooting

### API crash-loops on startup

**Symptom:** `curlyos-api` (systemd) or `start_api_server.py` exits immediately or fails the health check.

**Cause:** Postgres or Redis is not running (most common after a reboot on macOS with OrbStack).

**Recovery:**
1. Start OrbStack (or Docker Desktop).
2. `cd ~/curlyos-core/deploy && docker compose up -d`
3. `docker compose ps` — verify both containers show `healthy`.
4. `python3 start_api_server.py` (or `sudo systemctl start curlyos-api`).
5. `python3 start_api_server.py --status` — should show `running` with a health response.

### Stale PID file

**Symptom:** `start_api_server.py` reports "already running" but the process is gone.

**Fix:** `rm /tmp/curlyos-api.pid` then start normally.

### Redis port 6379 bind conflict

**Symptom:** `docker compose up` fails with `address already in use` on port 6379.

**Cause:** Another service (authentik, Caddy) is binding `6379` on the host.

**Fix:** Change the host-side port in `deploy/docker-compose.yml`:

```yaml
ports:
  - "127.0.0.1:6380:6379"   # example: remap host side to 6380
```

Then update `CURLYOS_REDIS_URL` in the environment (`.env` and `~/.hermes/curlyos.yaml`) to match.

### Migrations not applied after restore

**Symptom:** After a restore, the schema is behind the current codebase.

**Fix:** Run `python3 migrate.py` (or `python3 -m curlyos.setup --migrate`) against the restored database. The runner is idempotent — already-applied migrations are no-ops.

### `transaction_timeout` error during restore

**Symptom:** `pg_restore` or `psql` emits `ERROR: unrecognized configuration parameter "transaction_timeout"`.

**Cause:** A pg17 client tool produced the dump preamble against a pg16 server. This is harmless — the restore continues successfully.

### Health check exits non-zero

```bash
python3 -m curlyos.setup --check
```

Inspect the JSON output. `postgres.status` and `redis.status` will be `"ok"`, `"error"`, or `"skipped"` with a `detail` message indicating the cause.

## Deployment topology

```mermaid
graph LR
    subgraph Host ["Host (127.0.0.1)"]
        API["curlyos-api\n:8643\nFastAPI / uvicorn"]
        subgraph Docker Compose ["Docker Compose (project: curlyos)"]
            PG["curlyos-pg\nPostgres 16 + pgvector\n:54321 → :5432\nvol: curlyos_pgdata"]
            RD["curlyos-redis\nRedis 7.4\n:6379\nvol: curlyos_redisdata"]
        end
    end

    API -->|"postgresql://\n:54321"| PG
    API -->|"redis://\n:6379"| RD

    UI["curly-os webapp\n:3100 (optional)"] -->|"HTTP /api/*"| API
    Hermes["Hermes agent\n:8642"] -->|"MCP / HTTP"| API

    subgraph AWS ["AWS (transient)"]
        EmbedBox["EC2 embed box\nbge-m3 GPU/CPU"]
    end

    EmbedBox -->|"archive_vectors.tsv → S3"| S3["S3 bucket\ncurlyos-embed-*"]
    S3 -->|"load_archive_vectors.py"| PG
```
