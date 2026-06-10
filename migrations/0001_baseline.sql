-- 0001_baseline.sql — authoritative snapshot of the full CurlyOS-Core schema
-- as of 2026-06-10 (curlyos-final Phase F.1). Generated from the module DDL
-- constants (memory.stores ALL_DDL + knowledge.graph GRAPH_DDL + cognition
-- REFLECTION/METACOG/ATTENTION/NARRATIVE DDL) so module code and migrations
-- cannot drift. Everything is IF NOT EXISTS-guarded: applying this to the
-- live database is a no-op; applying to a fresh database bootstraps it.
-- HNSW params are m=32/ef_construction=200 (Spike-02 CONCERN-A resolution).

CREATE EXTENSION IF NOT EXISTS vector;

-- ── from memory.stores ─────────────────────────────────────────────
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
  WITH (m=32, ef_construction=200);

CREATE INDEX IF NOT EXISTS idx_episodes_scope_time ON episodes (scope, created_at);

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
  WITH (m=32, ef_construction=200);

CREATE INDEX IF NOT EXISTS idx_memories_scope_current ON memories (scope) WHERE valid_to IS NULL;

CREATE INDEX IF NOT EXISTS idx_memories_bitemporal ON memories (scope, valid_from, valid_to);

CREATE INDEX IF NOT EXISTS idx_memories_epistemic ON memories (scope, epistemic_status) WHERE valid_to IS NULL;

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

CREATE TABLE IF NOT EXISTS events (
  id           text        PRIMARY KEY,
  type         text        NOT NULL,
  subject      text,
  scope        text        NOT NULL,
  data         jsonb       NOT NULL,
  seq          bigserial   UNIQUE,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS projection_watermarks (
  projection  text        NOT NULL,
  scope       text        NOT NULL,
  last_seq    bigint      NOT NULL DEFAULT 0,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (projection, scope)
);

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

CREATE TABLE IF NOT EXISTS studio_links (
  id            text        PRIMARY KEY,
  src_sketch_id text        NOT NULL,
  dst_sketch_id text        NOT NULL,
  rel_type      text        NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_studio_links_src ON studio_links (src_sketch_id);
CREATE INDEX IF NOT EXISTS idx_studio_links_dst ON studio_links (dst_sketch_id);

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

CREATE TABLE IF NOT EXISTS evaluation_runs (
  id           text        PRIMARY KEY,
  candidate_ref text       NOT NULL,
  dataset_ids  jsonb       NOT NULL,
  scorers      jsonb       NOT NULL,
  pass_rate    real        NOT NULL,
  decision     text        NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE memories ADD COLUMN IF NOT EXISTS search_tsv tsvector;
CREATE INDEX IF NOT EXISTS idx_memories_tsv ON memories USING GIN(search_tsv);
CREATE OR REPLACE FUNCTION memories_tsv_trigger() RETURNS trigger AS $$
BEGIN NEW.search_tsv := to_tsvector('english', COALESCE(NEW.statement, '')); RETURN NEW;
END$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_memories_tsv ON memories;
CREATE TRIGGER trg_memories_tsv BEFORE INSERT OR UPDATE ON memories FOR EACH ROW EXECUTE FUNCTION memories_tsv_trigger();

ALTER TABLE episodes ADD COLUMN IF NOT EXISTS search_tsv tsvector;
CREATE INDEX IF NOT EXISTS idx_episodes_tsv ON episodes USING GIN(search_tsv);
CREATE OR REPLACE FUNCTION episodes_tsv_trigger() RETURNS trigger AS $$
BEGIN NEW.search_tsv := to_tsvector('english', COALESCE(NEW.content, '')); RETURN NEW;
END$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_episodes_tsv ON episodes;
CREATE TRIGGER trg_episodes_tsv BEFORE INSERT OR UPDATE ON episodes FOR EACH ROW EXECUTE FUNCTION episodes_tsv_trigger();

-- ── from knowledge.graph ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS knowledge_entities (
  id               text        PRIMARY KEY,
  scope            text        NOT NULL,
  name             text        NOT NULL,
  label            text        NOT NULL DEFAULT 'Entity',
  properties       jsonb       NOT NULL DEFAULT '{}',
  embedding        vector(1024),
  epistemic_status text        NOT NULL DEFAULT 'canonical',
  valid_from       timestamptz NOT NULL DEFAULT now(),
  valid_to         timestamptz,
  ingested_at      timestamptz NOT NULL DEFAULT now(),
  source_episode_id text,
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS knowledge_edges (
  id               text        PRIMARY KEY,
  src_entity_id    text        NOT NULL REFERENCES knowledge_entities(id),
  dst_entity_id    text        NOT NULL REFERENCES knowledge_entities(id),
  rel_type         text        NOT NULL,
  properties       jsonb       NOT NULL DEFAULT '{}',
  valid_from       timestamptz NOT NULL DEFAULT now(),
  valid_to         timestamptz,
  source_episode_id text,
  created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ke_scope_label ON knowledge_entities (scope, label) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_ke_name ON knowledge_entities (name) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_ke_hnsw ON knowledge_entities
  USING hnsw (embedding vector_cosine_ops) WITH (m=32, ef_construction=200);
CREATE INDEX IF NOT EXISTS idx_kedge_src ON knowledge_edges (src_entity_id) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_kedge_dst ON knowledge_edges (dst_entity_id) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_kedge_rel ON knowledge_edges (rel_type) WHERE valid_to IS NULL;

-- ── from cognition.reflection ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS reflection_reports (
  id                text        PRIMARY KEY,
  scope             text        NOT NULL,
  report_type       text        NOT NULL DEFAULT 'weekly',  -- weekly | monthly | manual
  time_window_start timestamptz NOT NULL,
  time_window_end   timestamptz NOT NULL,
  episodes_scanned  integer     NOT NULL DEFAULT 0,
  findings          jsonb       NOT NULL DEFAULT '[]',
  goal_deltas       jsonb       NOT NULL DEFAULT '[]',
  identity_candidates jsonb     NOT NULL DEFAULT '[]',
  summary           text,
  created_at        timestamptz NOT NULL DEFAULT now()
);

-- ── from cognition.meta ────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS assumptions (
  id                text        PRIMARY KEY,
  scope             text        NOT NULL,
  statement         text        NOT NULL,
  domain            text        NOT NULL DEFAULT 'general',
  confidence        real        NOT NULL DEFAULT 0.5,
  epistemic_status  text        NOT NULL DEFAULT 'hypothesis',
  valid_from        timestamptz NOT NULL DEFAULT now(),
  valid_to          timestamptz,
  source_episode_id text,
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS assumption_edges (
  id                text        PRIMARY KEY,
  src_assumption_id text        NOT NULL REFERENCES assumptions(id),
  dst_assumption_id text        NOT NULL REFERENCES assumptions(id),
  rel_type          text        NOT NULL,  -- rests_on | contradicts | derived_from | audited_by
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS mental_models (
  id                text        PRIMARY KEY,
  scope             text        NOT NULL,
  name              text        NOT NULL,
  domain            text        NOT NULL DEFAULT 'general',
  description       text        NOT NULL,
  confidence        real        NOT NULL DEFAULT 0.5,
  version           integer     NOT NULL DEFAULT 1,
  valid_from        timestamptz NOT NULL DEFAULT now(),
  valid_to          timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS decision_audits (
  id                text        PRIMARY KEY,
  scope             text        NOT NULL,
  decision          text        NOT NULL,
  domain            text        NOT NULL DEFAULT 'general',
  predicted_outcome text,
  actual_outcome    text,
  quality_score     real,  -- 0.0-1.0
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS principles (
  id                text        PRIMARY KEY,
  scope             text        NOT NULL,
  statement         text        NOT NULL,
  domain            text        NOT NULL DEFAULT 'general',
  epistemic_status  text        NOT NULL DEFAULT 'hypothesis',
  valid_from        timestamptz NOT NULL DEFAULT now(),
  valid_to          timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_assumptions_scope ON assumptions (scope, domain) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_principles_scope ON principles (scope, domain) WHERE valid_to IS NULL;

-- ── from cognition.attention ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS alignment_signals (
  id                text        PRIMARY KEY,
  scope             text        NOT NULL,
  signal_type       text        NOT NULL,  -- value_action_gap | fulfillment | regret
  description       text        NOT NULL,
  severity          real        NOT NULL DEFAULT 0.5,  -- 0.0-1.0
  epistemic_status  text        NOT NULL DEFAULT 'hypothesis',
  valid_from        timestamptz NOT NULL DEFAULT now(),
  valid_to          timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_aln_scope ON alignment_signals (scope, signal_type) WHERE valid_to IS NULL;

-- ── from cognition.narrative ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS life_chapters (
  id                text        PRIMARY KEY,
  scope             text        NOT NULL,
  title             text        NOT NULL,
  summary           text,
  start_date        timestamptz NOT NULL,
  end_date          timestamptz,
  epistemic_status  text        NOT NULL DEFAULT 'hypothesis',
  valid_from        timestamptz NOT NULL DEFAULT now(),
  valid_to          timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS themes (
  id                text        PRIMARY KEY,
  scope             text        NOT NULL,
  name              text        NOT NULL,
  description       text,
  frequency         integer     NOT NULL DEFAULT 1,
  epistemic_status  text        NOT NULL DEFAULT 'hypothesis',
  valid_from        timestamptz NOT NULL DEFAULT now(),
  valid_to          timestamptz,
  created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS theme_chapter_links (
  theme_id          text        NOT NULL REFERENCES themes(id),
  chapter_id        text        NOT NULL REFERENCES life_chapters(id),
  PRIMARY KEY (theme_id, chapter_id)
);

CREATE INDEX IF NOT EXISTS idx_chapters_scope ON life_chapters (scope);
CREATE INDEX IF NOT EXISTS idx_themes_scope ON themes (scope);

