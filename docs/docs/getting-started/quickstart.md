---
title: Quickstart
description: Start the API server and run your first ingest + recall.
---

This assumes you've completed [Installation](installation.md) — the data stores are up and the setup wizard has run.

## 1. Start the API server

```bash
# Dev (foreground, auto-reload):
uvicorn api_server:app --host 127.0.0.1 --port 8643 --reload

# Production (background daemon):
python3 start_api_server.py
python3 start_api_server.py --status
python3 start_api_server.py --stop
```

The API is at `http://127.0.0.1:8643`.

## 2. Smoke test

```bash
curl http://127.0.0.1:8643/api/health | python3 -m json.tool
curl http://127.0.0.1:8643/api/stats  | python3 -m json.tool
```

`/api/health` reports Postgres/Redis connectivity and embedder status; `/api/stats` reports row counts per table.

## 3. Ingest an experience

`/api/ingest` records raw text as an append-only **episode**, then asynchronously embeds it, classifies its epistemic status, distills facts, and extracts knowledge-graph triples. Harness scaffolding is stripped automatically.

```bash
curl -X POST http://127.0.0.1:8643/api/ingest \
  -H 'Content-Type: application/json' \
  -d '{"text": "I started building CurlyOS Core, a cognitive OS for agents.", "scope": "user:usr_hiten"}'
```

## 4. Recall

`/api/recall` runs the full hybrid pipeline — BM25 + dense vectors + graph expansion + rerank:

```bash
curl -X POST http://127.0.0.1:8643/api/recall \
  -H 'Content-Type: application/json' \
  -d '{"query": "what am I building?", "scope": "user:usr_hiten", "k": 5}'
```

For a plain full-text search over facts, use `GET /api/search?q=...`.

## 5. Explore the graph and identity

```bash
# Knowledge-graph nodes + edges (degree-ranked)
curl "http://127.0.0.1:8643/api/graph?limit=50" | python3 -m json.tool

# The stable self-model
curl http://127.0.0.1:8643/api/identity | python3 -m json.tool
```

## 6. Watch it think (optional)

Kick off the metacognition faculties manually instead of waiting for the scheduler:

```bash
# Distill principles + findings from the clean graph
curl -X POST http://127.0.0.1:8643/api/reflection/weekly

# Compose a grounded first-person narrative
curl -X POST http://127.0.0.1:8643/api/cognition/narrative/compose \
  -H 'Content-Type: application/json' \
  -d '{"query": "my work this month"}'
```

## 7. Observe

A one-call rollup of health, counts, LLM tier usage, recall throughput, ingest backlog and scheduler state:

```bash
curl http://127.0.0.1:8643/api/observability/overview | python3 -m json.tool
```

## Where to go next

- The complete **[REST API Reference](../reference/api-reference.md)** (116 routes).
- **[Configuration](configuration.md)** — env vars, LLM tiers, embedders, runtime settings.
- **[Hermes & MCP Integration](../integrations/hermes-and-mcp.md)** — wire CurlyOS into an agent as its memory.
