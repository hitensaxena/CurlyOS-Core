---
title: Installation
description: Install CurlyOS Core and bring up its data stores.
---

CurlyOS Core is a Python service backed by PostgreSQL (+ pgvector) and Redis. The fastest path is the bundled Docker Compose stack for the data stores plus a local virtualenv for the service.

## Prerequisites

| Component | Minimum | Purpose |
|-----------|---------|---------|
| Python | 3.11+ | Runtime |
| PostgreSQL | 16.4+ | Episodic + semantic store |
| pgvector | 0.7.4+ | Vector similarity in Postgres |
| Redis | 7.4+ | Working memory + cache + locks |
| Docker + Compose | any | Local Postgres + Redis stack |
| uv | optional | Fast Python package manager |

## 1. Clone and install

```bash
git clone https://github.com/hitensaxena/CurlyOS-Core.git
cd CurlyOS-Core

python3 -m venv .venv
source .venv/bin/activate

# Install with all optional dependencies (postgres, redis, embeddings, llm, …)
pip install -e ".[all]"      # or:  uv pip install -e ".[all]"
```

The optional dependency groups (from `pyproject.toml`) are: `postgres`, `redis`, `embeddings`, `reranker`, `llm`, `orchestration`, and `dev`. `all` pulls in everything except `dev`.

## 2. Start the data stores

```bash
cd deploy
cp .env.example .env          # then set POSTGRES_PASSWORD
docker compose up -d

# Verify
docker compose ps
docker exec curlyos-pg psql -U curlyos -d curlyos -c "SELECT 1"
docker exec curlyos-redis redis-cli ping
```

The compose stack uses **named volumes** (`curlyos_pgdata`, `curlyos_redisdata`) that survive `docker compose down`. See [Deployment & Operations](../operations/deployment-and-operations.md) for the full stack details.

## 3. Run the setup wizard

```bash
cd ..
source .venv/bin/activate
python3 curlyos_setup.py
```

The wizard tests Postgres + Redis, applies all DDL migrations (`migrations/*.sql` via `migrate.py`, tracked in a `schema_migrations` ledger), configures your embedder (`fake` for tests, `bge-m3` local, or `openai`), and writes `~/.hermes/curlyos.yaml`.

## 4. Verify

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

If this fails, the most common cause is that Docker/OrbStack (and therefore Postgres + Redis) isn't running — the API will crash-loop without them. See [Troubleshooting](../operations/deployment-and-operations.md).

Next: **[Quickstart](quickstart.md)** to start the server and ingest your first memory.
