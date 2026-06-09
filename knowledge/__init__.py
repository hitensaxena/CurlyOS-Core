"""Knowledge engine — derive, resolve, and link entities/relationships from the
episodic memory stream into a bi-temporal knowledge graph.

Components:
  extraction  — LLM-based entity + relation extraction from episodes
  resolution  — ANN blocking → cross-encoder → merge/mint/ambiguous
  graph       — Neo4j projection, GraphRAG, speculative graph

Key APIs:
  Consumes: memory.episode.recorded, memory.fact.stored (from HITENOS_MEMORY)
  Emits: knowledge.entity.resolved, knowledge.edge.created

Dependencies: memory (02a/02b/02c), evaluation (11), reflection (13)

See: ~/hitenos-architecture/03-knowledge-engine.md
"""
