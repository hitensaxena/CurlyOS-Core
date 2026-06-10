-- 0003_hnsw_reparam.sql — rebuild HNSW indexes at m=32/ef_construction=200
-- (curlyos-final Phase F.5). Spike-02 measured recall@10 = 0.844 at the old
-- m=16/ef_construction=64 (below the 0.95 gate) and 0.963 at these params.
-- An explicit DROP+CREATE is required: 0001's IF NOT EXISTS silently no-ops
-- on the live database's existing wrong-param indexes. This is a tracked
-- index rebuild, not data DDL — destroys nothing, old code reads the new
-- index transparently. At current data size (~10^2..10^3 rows) it's seconds.
-- Set hnsw.ef_search=128 at query time (retrieval config, not DDL).

DROP INDEX IF EXISTS idx_episodes_hnsw;
CREATE INDEX idx_episodes_hnsw ON episodes
  USING hnsw (embedding vector_cosine_ops)
  WITH (m=32, ef_construction=200);

DROP INDEX IF EXISTS idx_memories_hnsw;
CREATE INDEX idx_memories_hnsw ON memories
  USING hnsw (embedding vector_cosine_ops)
  WITH (m=32, ef_construction=200);

DROP INDEX IF EXISTS idx_ke_hnsw;
CREATE INDEX idx_ke_hnsw ON knowledge_entities
  USING hnsw (embedding vector_cosine_ops)
  WITH (m=32, ef_construction=200);
