# Hermes Agent Prompt: Install CurlyOS Core

Use this prompt to instruct a Hermes Agent (or any AI agent with shell + file access) to install and configure CurlyOS Core as its memory backend.

---

## The Prompt (copy-paste ready)

```
Install and configure CurlyOS Core as my Hermes Agent memory backend.

## What is CurlyOS Core?

CurlyOS Core is a bi-temporal knowledge graph + multi-tier memory system. It replaces
Hermes's default flat-file memory with a persistent Postgres+pgvector+Redis store.
Every conversation turn becomes a queryable episode. Facts are bi-temporal
(valid_from / valid_to), never deleted (only invalidated), and grounded in provenance.

## Prerequisites Check

Before starting, verify:
- Python 3.11+ is installed
- Docker and Docker Compose are available
- Git is installed
- Ports 54321 (Postgres) and 6379 (Redis) are free

## Installation Steps

### Step 1: Clone the repository

    git clone git@github.com:hitendev/curlyos-core.git ~/curlyos-core
    cd ~/curlyos-core

### Step 2: Create virtual environment and install

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -e ".[all]"

### Step 3: Start data stores (Postgres + Redis via Docker)

    cd ~/curlyos-core/deploy

Create the .env file with a strong password:

   cat > .env << 'ENVEOF'
POSTGRES_PASSWORD=<generate a strong password here>
ENVEOF

Start the stack:

    docker compose up -d

Wait for both containers to be healthy:

    docker compose ps
    # Should show curlyos-pg as "(healthy)" and curlyos-redis as "(healthy)"

If the named volumes don't already exist, create them first:

    docker volume create curlyos_pgdata
    docker volume create curlyos_redisdata

(If they already exist from a prior install, docker compose will skip creation.)

### Step 4: Write the CurlyOS config file

Create ~/.hermes/curlyos.yaml:

    mkdir -p ~/.hermes

    cat > ~/.hermes/curlyos.yaml << 'YAMLEOF'
database_url: "postgresql://curlyos:<THE_PASSWORD_YOU_SET>@localhost:54321/curlyos"
redis_url: "redis://localhost:6379/0"
embedder: "fake"
YAMLEOF

Embedder options:
- "fake"       → zero-config testing, random vectors (no real search)
- "bge-m3"     → local sentence-transformers, real 1024-dim embeddings (recommended)
- "openai"     → uses text-embedding-3-small (requires OPENAI_API_KEY)

### Step 5: Apply database migrations

    cd ~/curlyos-core
    source .venv/bin/activate
    python3 curlyos_setup.py --migrate

This creates all tables (episodes, memories, identity_facts, knowledge_entities, etc.)
and the pgvector extension. It is safe to re-run (idempotent).

### Step 6: Verify the installation

    python3 curlyos_setup.py --check

Expected output: postgres status "ok", redis status "ok" (or "skipped" if Redis
should show "ok"), and a table listing with all tables present.

### Step 7: Copy the Hermes memory plugin

    cp -r ~/curlyos-core/hermes_integration/* ~/.hermes/plugins/curlyos/

### Step 8: Configure Hermes to use CurlyOS

Edit ~/.hermes/config.yaml. Add or merge these keys:

    memory:
      provider: curlyos

    plugins:
      enabled:
        - curlyos

If there is already a plugins.enabled list, append "curlyos" to it.
If there is already a memory.provider set, change it to "curlyos".

Also ensure the environment variables are in ~/.hermes/.env (append if not present):

    echo 'CURLYOS_DATABASE_URL=postgresql://curlyos:<THE_PASSWORD_YOU_SET>@localhost:54321/curlyos' >> ~/.hermes/.env
    echo 'CURLYOS_REDIS_URL=redis://localhost:6379/0' >> ~/.hermes/.env

### Step 9: Start the CurlyOS API server

    cd ~/curlyos-core
    source .venv/bin/activate
    python3 start_api_server.py

This starts uvicorn api_server:app on 127.0.0.1:8643 as a background daemon.
Verify it's running:

    python3 start_api_server.py --status

### Step 10: Restart Hermes

Restart the Hermes gateway / CLI so it picks up the new plugin and config.
On next conversation turn, it should automatically:
- Record the turn as an episode in Postgres
- Inject relevant memories into the context
- Expose curlyos_recall, curlyos_add_fact, curlyos_add_note,
  curlyos_invalidate, and curlyos_identity tools

### Step 11: Quick integration test

Ask Hermens something like: "What do you remember about me?"
It should respond with context retrieved from CurlyOS.
Then try: "Remember that I like green tea" and verify with curlyos_recall.

## Troubleshooting

- **docker compose fails**: Check ports 54321 and 6379 are not already in use.
  Run: ss -tlnp | grep -E '(54321|6379)'
- **pgvector missing**: The setup wizard runs CREATE EXTENSION vector, but if
  the Postgres image doesn't include it, switch the image to pgvector/pgvector:pg16.
- **Plugin not loading**: Check ~/.hermes/plugins/curlyos/__init__.py exists.
  Run: hermes plugins list (or check logs).
- **CurlyOS tools not showing**: Ensure memory.provider=curlyos and the plugin
  is in plugins.enabled. Restart Hermes after config changes.
```

---

## Notes for the Agent

- The `.env` file in `deploy/` contains the Postgres password. Never commit it.
- The `hermes_integration/` dir is the plugin source. Copying it to
  `~/.hermes/plugins/curlyos/` is how Hermes discovers it.
- After initial install, upgrading is: `cd ~/curlyos-core && git pull && pip install -e ".[all]"`
- The API server (port 8643) is used by the Next.js web UI and can also be
  queried directly for debugging.
- Backups: `docker exec curlyos-pg pg_dump -U curlyos -d curlyos -Fc > backup.dump`
