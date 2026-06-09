"""Memory stores — physical layout and DDL for the four memory tiers.

Postgres SoR for episodes + memories + identity_facts.
Redis for working memory + read models + locks.
pgvector HNSW for dense ANN.
"""
from __future__ import annotations


# ── SQL DDL ──────────────────────────────────────────────────────────────────

EPISODES_DDL = """
CREATE TABLE IF NOT EXISTS episodes (
  id               text        PRIMARY KEY,
  scope            text        NOT NULL,
  content          text        NOT NULL,
  source_ref       text,
  modality         text        NOT NULL DEFAULT 'text',
  embedding        vector(1024),
  ingested_at      timestamptz NOT NULL DEFAULT now(),
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_episodes_hnsw ON episodes
  USING hnsw (embedding vector_cosine_ops)
  WITH (m=16, ef_construction=64);

CREATE INDEX IF NOT EXISTS idx_episodes_scope_time ON episodes (scope, created_at);
"""

MEMORIES_DDL = """
CREATE TABLE IF NOT EXISTS memories (
  id                text        PRIMARY KEY,
  scope             text        NOT NULL,
  statement         text        NOT NULL,
  statement_key     text        NOT NULL,
  kind              text        NOT NULL DEFAULT 'fact',
  tier              text        NOT NULL DEFAULT 'semantic',
  embedding         vector(1024),
  epistemic_status  text        NOT NULL DEFAULT 'canonical',
  valid_from        timestamptz NOT NULL,
  valid_to          timestamptz,
  ingested_at       timestamptz NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  source_episode_id text        NOT NULL REFERENCES episodes(id),
  superseded_by     text        REFERENCES memories(id)
);

CREATE INDEX IF NOT EXISTS idx_memories_hnsw ON memories
  USING hnsw (embedding vector_cosine_ops)
  WITH (m=16, ef_construction=64);

CREATE INDEX IF NOT EXISTS idx_memories_scope_current ON memories (scope) WHERE valid_to IS NULL;

CREATE INDEX IF NOT EXISTS idx_memories_bitemporal ON memories (scope, valid_from, valid_to);

CREATE INDEX IF NOT EXISTS idx_memories_epistemic ON memories (scope, epistemic_status) WHERE valid_to IS NULL;
"""

IDENTITY_FACTS_DDL = """
CREATE TABLE IF NOT EXISTS identity_facts (
  id                text        PRIMARY KEY,
  scope             text        NOT NULL,
  predicate         text        NOT NULL,
  object            text        NOT NULL,
  confidence        real        NOT NULL,
  epistemic_status  text        NOT NULL DEFAULT 'canonical',
  valid_from        timestamptz NOT NULL,
  valid_to          timestamptz,
  ingested_at       timestamptz NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),
  source_episode_id text        NOT NULL REFERENCES episodes(id),
  superseded_by     text        REFERENCES identity_facts(id)
);

CREATE INDEX IF NOT EXISTS idx_idf_scope_predicate_current
  ON identity_facts (scope, predicate) WHERE valid_to IS NULL;

CREATE INDEX IF NOT EXISTS idx_idf_bitemporal
  ON identity_facts (scope, predicate, valid_from, valid_to);
"""

EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS events (
  id           text        PRIMARY KEY,
  type         text        NOT NULL,
  subject      text,
  scope        text        NOT NULL,
  data         jsonb       NOT NULL,
  seq          bigserial   UNIQUE,
  created_at   timestamptz NOT NULL DEFAULT now()
);
"""

PROJECTION_WATERMARKS_DDL = """
CREATE TABLE IF NOT EXISTS projection_watermarks (
  projection  text        NOT NULL,
  scope       text        NOT NULL,
  last_seq    bigint      NOT NULL DEFAULT 0,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (projection, scope)
);
"""

STUDIOS_DDL = """
CREATE TABLE IF NOT EXISTS studios (
  id          text        PRIMARY KEY,
  scope       text        NOT NULL,
  title       text        NOT NULL,
  status      text        NOT NULL DEFAULT 'active',
  properties  jsonb       NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_studios_scope ON studios (scope);
CREATE INDEX IF NOT EXISTS idx_studios_status ON studios (status);
"""

STUDIO_SKETCHES_DDL = """
CREATE TABLE IF NOT EXISTS studio_sketches (
  id                text        PRIMARY KEY,
  studio_id         text        NOT NULL,
  content           text        NOT NULL,
  kind              text        NOT NULL DEFAULT 'text',
  epistemic_status  text        NOT NULL DEFAULT 'seed',
  properties        jsonb       NOT NULL DEFAULT '{}',
  embedding         vector(1024),
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  CHECK (epistemic_status IN ('seed', 'conjecture', 'hypothesis'))
);

CREATE INDEX IF NOT EXISTS idx_studio_sketches_studio ON studio_sketches (studio_id);
CREATE INDEX IF NOT EXISTS idx_studio_sketches_epistemic ON studio_sketches (epistemic_status);
"""

STUDIO_LINKS_DDL = """
CREATE TABLE IF NOT EXISTS studio_links (
  id            text        PRIMARY KEY,
  src_sketch_id text        NOT NULL,
  dst_sketch_id text        NOT NULL,
  rel_type      text        NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_studio_links_src ON studio_links (src_sketch_id);
CREATE INDEX IF NOT EXISTS idx_studio_links_dst ON studio_links (dst_sketch_id);
"""

SIMULATION_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS simulation_runs (
  id                   text        PRIMARY KEY,
  scope                text        NOT NULL,
  question             text        NOT NULL,
  world_model_id       text,
  status               text        NOT NULL DEFAULT 'created',
  epistemic_status     text        NOT NULL DEFAULT 'possible_world',
  outcome_distribution jsonb       NOT NULL DEFAULT '{}',
  parameters           jsonb       NOT NULL DEFAULT '{}',
  created_at           timestamptz NOT NULL DEFAULT now(),
  completed_at         timestamptz
);

CREATE INDEX IF NOT EXISTS idx_sim_runs_scope ON simulation_runs (scope);
CREATE INDEX IF NOT EXISTS idx_sim_runs_status ON simulation_runs (status);
"""

SIMULATION_SCENARIOS_DDL = """
CREATE TABLE IF NOT EXISTS simulation_scenarios (
  id              text        PRIMARY KEY,
  run_id          text        NOT NULL,
  description     text,
  assumptions     jsonb       NOT NULL DEFAULT '[]',
  probability     real,
  outcome         text,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sim_scenarios_run ON simulation_scenarios (run_id);
"""

GOLDEN_DATASETS_DDL = """
CREATE TABLE IF NOT EXISTS golden_datasets (
  id            text        PRIMARY KEY,
  name          text        NOT NULL,
  content_hash  text        NOT NULL,
  data          jsonb       NOT NULL,
  metadata      jsonb       NOT NULL DEFAULT '{}',
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_golden_datasets_name ON golden_datasets (name);
CREATE INDEX IF NOT EXISTS idx_golden_datasets_hash ON golden_datasets (content_hash);
"""

WORKSPACES_DDL = """
CREATE TABLE IF NOT EXISTS workspaces (
  id          text        PRIMARY KEY,
  scope       text        NOT NULL,
  name        text        NOT NULL,
  kind        text        NOT NULL DEFAULT 'project',
  properties  jsonb       NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_workspaces_scope ON workspaces (scope);
"""

PROJECTS_DDL = """
CREATE TABLE IF NOT EXISTS projects (
  id            text        PRIMARY KEY,
  workspace_id  text        NOT NULL REFERENCES workspaces(id),
  name          text        NOT NULL,
  status        text        NOT NULL DEFAULT 'active',
  properties    jsonb       NOT NULL DEFAULT '{}',
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_projects_workspace ON projects (workspace_id);
CREATE INDEX IF NOT EXISTS idx_projects_status ON projects (status);
"""

TASKS_DDL = """
CREATE TABLE IF NOT EXISTS tasks (
  id            text        PRIMARY KEY,
  project_id    text        NOT NULL REFERENCES projects(id),
  title         text        NOT NULL,
  priority      text        NOT NULL DEFAULT 'medium',
  status        text        NOT NULL DEFAULT 'pending',
  depends_on    jsonb       NOT NULL DEFAULT '[]',
  created_at    timestamptz NOT NULL DEFAULT now(),
  completed_at  timestamptz
);

CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks (project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks (status);
CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks (priority);
"""

# ── Tsvector search for memories ──────────────────────────────────────────────

MEMORIES_TSV_DDL = """
ALTER TABLE memories ADD COLUMN IF NOT EXISTS search_tsv tsvector;
CREATE INDEX IF NOT EXISTS idx_memories_tsv ON memories USING GIN(search_tsv);
CREATE OR REPLACE FUNCTION memories_tsv_trigger() RETURNS trigger AS $$
BEGIN NEW.search_tsv := to_tsvector('english', COALESCE(NEW.statement, '')); RETURN NEW;
END$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_memories_tsv ON memories;
CREATE TRIGGER trg_memories_tsv BEFORE INSERT OR UPDATE ON memories FOR EACH ROW EXECUTE FUNCTION memories_tsv_trigger();
"""

# ── Tsvector search for episodes ──────────────────────────────────────────────

EPISODES_TSV_DDL = """
ALTER TABLE episodes ADD COLUMN IF NOT EXISTS search_tsv tsvector;
CREATE INDEX IF NOT EXISTS idx_episodes_tsv ON episodes USING GIN(search_tsv);
CREATE OR REPLACE FUNCTION episodes_tsv_trigger() RETURNS trigger AS $$
BEGIN NEW.search_tsv := to_tsvector('english', COALESCE(NEW.content, '')); RETURN NEW;
END$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_episodes_tsv ON episodes;
CREATE TRIGGER trg_episodes_tsv BEFORE INSERT OR UPDATE ON episodes FOR EACH ROW EXECUTE FUNCTION episodes_tsv_trigger();
"""

EVALUATION_RUNS_DDL = """
CREATE TABLE IF NOT EXISTS evaluation_runs (
  id           text        PRIMARY KEY,
  candidate_ref text       NOT NULL,
  dataset_ids  jsonb       NOT NULL,
  scorers      jsonb       NOT NULL,
  pass_rate    real        NOT NULL,
  decision     text        NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);
"""

ALL_DDL = (
    EPISODES_DDL
    + MEMORIES_DDL
    + IDENTITY_FACTS_DDL
    + EVENTS_DDL
    + PROJECTION_WATERMARKS_DDL
    + STUDIOS_DDL
    + STUDIO_SKETCHES_DDL
    + STUDIO_LINKS_DDL
    + SIMULATION_RUNS_DDL
    + SIMULATION_SCENARIOS_DDL
    + GOLDEN_DATASETS_DDL
    + WORKSPACES_DDL
    + PROJECTS_DDL
    + TASKS_DDL
    + EVALUATION_RUNS_DDL
    + MEMORIES_TSV_DDL
    + EPISODES_TSV_DDL
)
