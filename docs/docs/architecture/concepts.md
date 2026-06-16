---
title: Key Concepts
description: The cross-cutting ideas that define CurlyOS Core.
---

These principles recur in every subsystem. Understanding them once makes the rest of the documentation read quickly.

## Bi-temporal validity

Every fact carries two independent time axes:

- **Valid time** — `valid_from` / `valid_to`: the window during which the fact is *true in the world*.
- **Transaction time** — `ingested_at`: when the system *learned* it.

This lets you ask two genuinely different questions:

```sql
-- What is true now?
SELECT * FROM memories WHERE valid_to IS NULL;

-- What did I believe on a past date X (time-travel)?
SELECT * FROM memories WHERE ingested_at <= 'X' AND (valid_to IS NULL OR valid_to > 'X');
```

## Invalidate-not-delete

Superseded facts are never `DELETE`d. When a fact stops being true, its `valid_to` is set to `now()` and a new row is written. History and provenance are preserved, and every change is reversible. The kill switch, consolidation conflict-resolution, and the `/api/memories/{id}/invalidate` endpoint all follow this rule.

## Episodic provenance

Every distilled fact MUST cite a `source_episode_id`. No fact exists without the experience that produced it. Episodes form an append-only log; the knowledge graph and other projections are *rebuildable* from that log.

## Epistemic axis

Confidence is a first-class dimension, classified by an LLM at capture time:

```
hypothesis  →  belief  →  canonical
```

Plus producer-specific values used by the simulation/idea layers: `seed`, `conjecture`, `possible_world`. The classifier lives in `shared/epistemic.py`; it cross-cuts every fact and can be toggled with the `epistemic_classify_enabled` setting.

## Capture hygiene

Ingestion strips harness/tooling scaffolding — system reminders, task notifications, command wrappers — so only genuine content becomes memory. This happens on the `/api/ingest` hot path and in the Hermes `sync_turn()` hook.

## Event sourcing

Writes emit **CloudEvents** (`shared/events`) into an event log. Projections — most importantly the knowledge graph — are derived from, and rebuildable from, that log. The full event catalog is documented in [Shared Infrastructure](../subsystems/shared-infrastructure.md).

## The hot path vs the "sleep" path

CurlyOS deliberately splits work:

- **Hot path (synchronous, fast):** record the episode, add a fact, return. Cheap, low-latency, runs on every ingest.
- **Sleep path (asynchronous, deferred):** embedding, distillation, KG extraction, dedup, conflict-resolution, decay, reflection. Heavy LLM work runs in background consolidation jobs on a schedule owned by the [orchestration scheduler](../subsystems/orchestration.md).

## Tiered LLM routing

Three task tiers (`fast`, `agentic`, `deep`) each resolve their own endpoint/key/model and share a single `FallbackClient` failover chain. High-volume classification uses cheap fast models; the agent loop uses an agentic model; reflection and narrative use a deep "thinking" model. Details in [Shared Infrastructure](../subsystems/shared-infrastructure.md).

## ULIDs and ID prefixes

Identifiers are ULIDs with a short type prefix (e.g. `out_`, `les_`, `dec_`) minted by `shared/types/ulid.py`. The prefix tells you what kind of object an id refers to at a glance. The full prefix table is in [Shared Infrastructure](../subsystems/shared-infrastructure.md).

## Autonomy toggles

Autonomous behaviour is gated by typed settings (`shared/settings.py`) so it can be turned up or down live, without a restart:

- `auto_promote` — let reflection promote opportunities into goals.
- `auto_plan` — let the system plan goals into tasks.
- `auto_execute` — let planned tasks dispatch to agent runs.

These compose with the [safety](../subsystems/safety-and-governance.md) PDP, budget and kill switch. See the [settings registry](../subsystems/shared-infrastructure.md) for every knob.
