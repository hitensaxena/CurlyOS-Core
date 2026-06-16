---
title: Development
description: Running tests, linting, type-checking, and the project layout.
---

## Setup

```bash
pip install -e ".[dev]"
```

The `dev` extra adds `pytest`, `pytest-asyncio`, `pytest-xdist`, `ruff`, and `mypy`.

## Tests

```bash
pytest tests/ -q            # add -n auto for parallelism (pytest-xdist)
```

`asyncio_mode = "auto"` is set in `pyproject.toml`, so `async def test_*` functions run without explicit markers. There are also standalone smoke tests at the repo root you can run directly:

| Script | Checks |
|--------|--------|
| `smoketest.py` | End-to-end ingest → recall sanity |
| `hermes_smoketest.py` | Hermes integration plugin |
| `knowledge_smoketest.py` | KG extraction → resolution → projection |
| `decision_loop_smoketest.py` · `decision_loop_wiring_smoketest.py` | Decision loop and its wiring |
| `observability_smoketest.py` | `/api/observability/*` endpoints |

## Lint & types

```bash
ruff check .
mypy memory/ knowledge/ identity/ cognition/ shared/
```

`ruff` targets Python 3.11 with a 120-char line length (`pyproject.toml`).

## Continuous integration

`.github/workflows/ci.yml` runs the lint / type-check / test pipeline on push. See [Deployment & Operations](operations/deployment-and-operations.md#ci-github-actions) for the job breakdown.

## Project layout

The packaged modules (from `pyproject.toml` `[tool.hatch.build.targets.wheel]`):

```
memory, knowledge, identity, cognition, studio, simulation, evaluation,
workspace, shared, hermes_integration, safety, agent, goals, orchestration
```

Each subsystem is documented under **Subsystems** in this site. The data layer is documented in the **[Database Schema](reference/database-schema.md)** reference, and every HTTP route in the **[REST API Reference](reference/api-reference.md)**.

## Conventions to preserve

- **Facts are never deleted** — only invalidated (`valid_to = now()`).
- **Every fact cites a `source_episode_id`** — no ungrounded claims.
- **Heavy LLM work is deferred** to the consolidation "sleep" path, not the ingest hot path.
- **New schema goes in a numbered migration** (`migrations/00NN_*.sql`) and is applied via `migrate.py`; the `schema_migrations` ledger enforces ordering and idempotency.
- **Runtime-tunable behaviour belongs in the settings registry** (`shared/settings.py`), not hard-coded constants.
