# CurlyOS Core — status

Last updated: 2026-11-16. `main` at `77de09b`.

CurlyOS is the shared memory layer behind Claude Code, Codex, Hermes and the
web app: one FastAPI service on `127.0.0.1:8643` over Postgres 16 + pgvector,
with Redis for the recall cache. Everything below is running in production on
this machine.

## How to check it is healthy

```bash
curl -s localhost:8643/api/health | jq
curl -s 'localhost:8643/api/events/dispatch?scope=user:usr_hiten' | jq '.handlers'
cd "Tech Projects/CurlyOS-Core" && .venv/bin/python -m pytest -q
```

The dispatcher's `handlers` array must show `lag: 0` and `error_count: 0` for
both `graph_projection` and `metrics`. A non-zero `event.graph_projection.discrepancies`
in the same response means an event payload and its stored row disagree.

## Current state

| | |
|---|---|
| Episodes | 10,111 |
| Memories | 33,076 |
| Identity facts | 177 |
| Knowledge entities | 26,238 |
| Knowledge edges | 44,063 |
| Tests | 242 unit, 19 integration, ruff clean |
| Engines | 13 registered, registry-driven scheduler |
| Dispatcher | `graph_projection`, `metrics` — lag 0, errors 0 |

## What the system does now

**Ingestion** splits long text on boundary-aware chunks (4,000 chars, 200
overlap), dedups on a content hash, embeds on write, and marks duplicate
episodes closed instead of re-summarising them.

**Retrieval** blends dense, sparse, entity and graph candidates with RRF, then
re-ranks by cosine, recency, lexical overlap and working-set salience. Stage
timings are returned with every response. Golden eval: hit@k 0.90 over 21
queries.

**Cognition** keeps a salience-ranked working set that boosts recall,
proposes contradictions for approval rather than resolving them silently,
surfaces decisions as intentions, and asks curiosity questions from grounded
gaps.

**Event sourcing** writes every graph change as an event in the same
transaction as the row. `apply_event` is the only implementation that writes
graph rows; the dispatcher replays the log at-least-once as a repair path, and
the shadow verifier compares stored rows against payloads on every dispatch.

## Open items

1. **Jun–Sep 2026 graph deletions are unattributed.** 3,867 edges were deleted
   with no deleting code in this repo's history for that period. All of them
   are now tracked as invalidated; the cause is unknown and deliberately not
   guessed.
2. **The API token gate is dormant.** `CURLYOS_API_TOKEN` is unset; the API
   binds 127.0.0.1. Arming it requires the variable on the server *and* on
   every client — server-first leaves clients failing closed with 401.

## Not a problem (checked, listed so it is not re-raised)

- **Sync routes do not block the event loop.** FastAPI runs `def` endpoints in
  a threadpool; the 21 sync routes are fine. The three `async` routes that
  touch the blocking connection (`/api/cognition/attention`,
  `/api/cognition/narrative/compose`, `/api/attention/scan`) all wrap it in
  `asyncio.to_thread` or use the async pool.
- **`theme_chapter_links`** was dropped as dead by migration 0021. The
  narrative module's DDL still re-created it; that declaration was removed on
  2026-11-16.
- **`densify_kg.py` and `bridge_kg_components.py`** now stage
  `knowledge.edge.created` events in the same transaction as their edges, via
  `deploy/edge_events.py`. Their rows are replayable. The helper refuses an
  async cursor: `AsyncCursor.executemany` returns a coroutine, so an un-awaited
  call writes nothing and still looks successful.

## Operational notes learned the hard way

- A typed dispatcher handler's `lag` is only honest for the event types in its
  own type list. Adding a new type without adding it there reports `lag: 0`
  while events sit unprocessed.
- `deploy/rebuild_projections.py --apply` on the graph is only safe once every
  deleted id has an invalidation event; before that it re-inserts the duplicate
  edges and merged entities that graph hygiene deliberately removed.
- `deploy/shadow_exercise.py` proves the shadow gate end to end: a clean run
  must apply every event with zero discrepancies, and an injected corruption
  must raise exactly one.
