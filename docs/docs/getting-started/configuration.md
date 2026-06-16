---
title: Configuration
description: Environment variables, LLM tiers, embedders, and live runtime settings.
---

CurlyOS Core is configured in two layers: **boot-time** configuration (environment variables and YAML, read at startup) and **runtime** settings (a typed registry you can change live without a restart).

## Resolution order

Boot configuration is resolved in priority order:

```
env vars  →  ~/.hermes/curlyos.yaml  →  ~/.hermes/.env
```

## Core environment variables

| Variable | Description | Default |
|----------|-------------|---------|
| `CURLYOS_DATABASE_URL` | PostgreSQL DSN | *(required)* |
| `CURLYOS_REDIS_URL` | Redis URL | *(required)* |
| `CURLYOS_API_PORT` | API server port | `8643` |
| `CURLYOS_SCOPE` | Default scope for facts | `user:usr_hiten` |
| `CURLYOS_EMBED_DEVICE` | Force embedder device: `cpu` · `mps` · `cuda` | `cpu` |
| `OPENROUTER_API_KEY` | LLM key for extraction/cognition | *(optional)* |
| `REFRESH_BACKEND` | `chain` (OpenRouter) or `hermes` (Claude via hermes-bridge) | `chain` |

## LLM tier variables

Each of the three tiers resolves its own endpoint, key and model chain, and falls back to the **fast** tier config when unset.

| Tier | Variables | Used by |
|------|-----------|---------|
| **fast** | `CURLYOS_LLM_BASE_URL` · `CURLYOS_LLM_API_KEY` · `CURLYOS_MODEL_CHAIN` | epistemic classification, KG extraction, distillation |
| **agentic** | `CURLYOS_AGENTIC_BASE_URL` · `CURLYOS_AGENTIC_API_KEY` · `CURLYOS_AGENTIC_MODEL` | orchestrator ReAct runner, agent runs |
| **deep** | `CURLYOS_DEEP_BASE_URL` · `CURLYOS_DEEP_API_KEY` · `CURLYOS_DEEP_MODEL` | reflection, meta-cognition, narrative |

The complete env-var list (17 variables) and the failover mechanics are documented in **[Shared Infrastructure](../subsystems/shared-infrastructure.md)**.

## YAML config

```yaml
# ~/.hermes/curlyos.yaml
database_url: "postgresql://curlyos:PASSWORD@localhost:54321/curlyos"
redis_url:    "redis://localhost:6379/0"
embedder:     "bge-m3"   # fake | bge-m3 | openai
```

Embedder choices:

- `fake` — deterministic hash embedder for tests (not semantic).
- `bge-m3` — local sentence-transformers model (the default for real use).
- `openai` — OpenAI embeddings API.

## Runtime settings (no restart)

Runtime behaviour is tunable live via a typed settings registry (`shared/settings.py`):

```bash
# List every knob with its type, default and description
curl http://127.0.0.1:8643/api/settings | python3 -m json.tool

# Update one (validated, applied live)
curl -X PUT http://127.0.0.1:8643/api/settings/recall_cache_ttl_seconds \
  -H 'Content-Type: application/json' -d '{"value": 120}'
```

Wired knobs include:

| Key | Effect |
|-----|--------|
| `recall_cache_enabled` | Toggle the recall result cache |
| `recall_cache_ttl_seconds` | Recall cache TTL |
| `recall_fast_followups` | Cheaper follow-up recalls |
| `epistemic_classify_enabled` | Toggle per-ingest epistemic classification |
| `kg_extraction_enabled` | Toggle knowledge-graph triple extraction |
| `auto_promote` | Reflection may promote opportunities → goals |
| `auto_plan` | System may plan goals → tasks |

See the full table in **[Shared Infrastructure](../subsystems/shared-infrastructure.md)** and how autonomy toggles compose with the [safety layer](../subsystems/safety-and-governance.md).

## Backups

```bash
# Logical backup
docker exec curlyos-pg pg_dump -U curlyos -d curlyos -Fc > backup.dump

# Restore
cat backup.dump | docker exec -i curlyos-pg pg_restore -U curlyos -d curlyos --clean --if-exists
```

More in [Deployment & Operations](../operations/deployment-and-operations.md).
