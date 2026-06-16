---
title: Introduction
description: CurlyOS Core — a cognitive operating system for AI agents.
slug: /
---

# CurlyOS Core

> A cognitive operating system for AI agents — bi-temporal memory, a self-organizing knowledge graph, a stable identity model, and a metacognition layer that reflects on its own thinking.

Most AI "memory" is a flat file or a pile of vectors: you dump text in, you cosine-search it back out, and nothing ever connects, ages, or changes its mind. **CurlyOS Core is built on the opposite premise** — that durable intelligence needs *structure, time, provenance, and self-revision*, not just recall.

It is a standalone Python service (FastAPI, PostgreSQL + pgvector, Redis) that gives an agent a real cognitive substrate: every experience is recorded with provenance, distilled into time-aware facts, projected into a connected knowledge graph, folded into a stable self-model, and periodically re-examined by a reflection loop that distills principles, surfaces life themes, and flags its own stale assumptions. It ships as a drop-in [Hermes Agent](https://hermes-agent.nousresearch.com) memory plugin and powers a companion web UI for exploring the whole mind visually.

## Why CurlyOS Core is different

| | Flat-file / scratchpad | Naive RAG (vector store) | **CurlyOS Core** |
|---|---|---|---|
| **Structure** | Unstructured notes | Opaque chunks | Episodes → facts → entity/relation graph |
| **Time** | Overwrites or appends | No notion of time | Bi-temporal (`valid_from`/`valid_to` + `ingested_at`) |
| **Truth changes** | Lost or duplicated | Stale chunks linger | Invalidate-not-delete — full history kept |
| **Provenance** | None | Chunk → source, maybe | Every fact cites a `source_episode_id` |
| **Confidence** | None | None | Epistemic axis — `hypothesis` / `belief` / `canonical` |
| **Retrieval** | grep | Vector top-k | Hybrid: BM25 + dense vectors + graph expansion + rerank |
| **Self-revision** | None | None | Consolidation + reflection (dedup, conflict-resolve, distill) |
| **Identity** | None | None | A stable self-model that survives context resets |

The short version: a vector store remembers *what was said*. CurlyOS Core remembers *what is true, when it was true, why you believe it, and how confident you are* — and it keeps cleaning that up while you sleep.

## The layered stack

CurlyOS Core is a set of real, queryable subsystems, each with its own tables and REST endpoints:

- **[Memory](subsystems/memory.md)** — four tiers (working, episodic, semantic, procedural) with a fast hot path and deferred "sleep" consolidation.
- **[Knowledge Graph](subsystems/knowledge-graph.md)** — LLM-extracted triples, entity resolution, and a densified, fully-connected typed graph.
- **[Identity](subsystems/identity.md)** — a bi-temporal self-model distilled from reflection.
- **[Cognition](subsystems/cognition.md)** — reflection, narrative, attention and meta faculties that think about the thinking.
- **[Orchestration](subsystems/orchestration.md)** — the autonomous agent loop: opportunity → goal → agent → verify → repeat.
- **[Safety & Governance](subsystems/safety-and-governance.md)** — policy gate, budget, kill switch, approvals, hash-chained audit.
- **[Goals & Workspace](subsystems/goals-and-workspace.md)**, **[Studio / Simulation / Evaluation](subsystems/studio-simulation-evaluation.md)**, and **[Shared Infrastructure](subsystems/shared-infrastructure.md)**.

## Where to go next

- New here? Start with **[Architecture Overview](architecture/overview.md)** and the **[Key Concepts](architecture/concepts.md)**.
- Want it running? Follow **[Installation](getting-started/installation.md)** → **[Quickstart](getting-started/quickstart.md)**.
- Building against it? See the **[REST API Reference](reference/api-reference.md)** and **[Database Schema](reference/database-schema.md)**.
- Plugging into an agent? See **[Hermes & MCP Integration](integrations/hermes-and-mcp.md)**.

## At a glance (a live instance)

```
116 REST routes   ·   43 Postgres tables   ·   17 MCP tools
~1.6k episodes  →  ~20k facts  →  3.2k entities / 8.7k edges  →  37 identity facts
single fully-connected knowledge graph · 0 orphans
```

> **License:** MIT. **Repository:** [hitensaxena/CurlyOS-Core](https://github.com/hitensaxena/CurlyOS-Core).
