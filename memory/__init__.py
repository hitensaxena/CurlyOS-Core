"""Memory engine — four-tier cognitive memory system.

Tiers:
  WORKING   — Redis, session-scoped, volatile (TTL 2h)
  EPISODIC  — Postgres episodes, append-only provenance ground-truth
  SEMANTIC  — Postgres memories + pgvector HNSW, bi-temporal facts
  PROCEDURAL — MinIO blobs + Postgres memories row (kind=procedure)

Write discipline (graphiti / mem0 pattern):
  Hot path: add() + record_episode() — append-only, no dedup
  Async sleep: consolidation worker — dedup, merge, conflict-resolve, summarize, decay

See: ~/hitenos-architecture/02a-memory-stores.md, 02b-memory-governance.md, 02c-retrieval.md
"""
