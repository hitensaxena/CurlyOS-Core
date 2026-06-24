-- 0017: Cognition enhancements: mood_log table + mental_models embedding
--
-- Adds:
--   1. mood_log table (attention engine — mood/energy tracking)
--   2. embedding column on mental_models (meta-cognition — semantic search)
--   3. HNSW index for mental_models embedding search

BEGIN;

-- 1. Mood log table
CREATE TABLE IF NOT EXISTS mood_log (
  id                text        PRIMARY KEY,
  scope             text        NOT NULL,
  mood              text        NOT NULL,
  valence           real        NOT NULL DEFAULT 0.0,
  energy            real        NOT NULL DEFAULT 0.5,
  context           text,
  source            text        NOT NULL DEFAULT 'inference',
  source_episode_id text,
  logged_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_mood_scope_time ON mood_log (scope, logged_at DESC);

-- 2. Mental models embedding column + HNSW index
ALTER TABLE mental_models ADD COLUMN IF NOT EXISTS embedding vector(1024);

CREATE INDEX IF NOT EXISTS idx_mdl_hnsw ON mental_models
  USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=100);

COMMIT;
