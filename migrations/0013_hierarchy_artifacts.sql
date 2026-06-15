-- 0013_hierarchy_artifacts.sql — the autonomous-OS execution hierarchy.
--
-- Goals (0004) and their execution plans (0009/0012) had no physical home: a
-- worker wrote files wherever it liked and nothing tracked the deliverables. This
-- migration gives the hierarchy a real spine and a tangible output surface:
--
--   workspace ──< project ──< goal ──< plan ──< task ──> run ──> ARTIFACTS
--
-- A workspace is a top-level life-area container; a project is a high-level goal
-- (a "north star") that clubs several related goals; each project gets a real
-- directory on disk (`~/curlyos/workspaces/<ws>/<prj>/{studio,src}`) where agents
-- write tangible output. The new `artifacts` table records every deliverable an
-- agent produces (file/doc/pdf/image/code/deploy/link) so the studio view can
-- show "what actually got made."
--
-- Additive only. Existing idioms: text <prefix>_<ulid> ids, scope on every row,
-- jsonb extensibility seam, CHECK-constrained enums.

-- 1) Workspaces gain identity (slug), a physical home (path), a one-liner, and a
--    lifecycle status. `kind`/`properties` already exist (0001 baseline).
ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS slug    text;
ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS path    text;
ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS summary text;
ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS status  text NOT NULL DEFAULT 'active';
DO $$ BEGIN
  ALTER TABLE workspaces ADD CONSTRAINT workspaces_status_check
    CHECK (status IN ('active','archived'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
CREATE UNIQUE INDEX IF NOT EXISTS uq_workspaces_scope_slug
  ON workspaces (scope, slug) WHERE slug IS NOT NULL;

-- 2) Projects gain a scope, identity (slug), a physical home (path), a one-liner,
--    an `updated_at`, and a link to the goal that defines them (the "north star").
ALTER TABLE projects ADD COLUMN IF NOT EXISTS scope              text;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS slug               text;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS path               text;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS summary            text;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS north_star_goal_id text REFERENCES goals(id);
ALTER TABLE projects ADD COLUMN IF NOT EXISTS updated_at         timestamptz NOT NULL DEFAULT now();
CREATE UNIQUE INDEX IF NOT EXISTS uq_projects_ws_slug
  ON projects (workspace_id, slug) WHERE slug IS NOT NULL;

-- 3) Goals get placed into a project (nullable: a goal may not be placed yet).
ALTER TABLE goals ADD COLUMN IF NOT EXISTS project_id text REFERENCES projects(id);
CREATE INDEX IF NOT EXISTS idx_goals_project ON goals (project_id) WHERE project_id IS NOT NULL;

-- 4) The sketch-studio (0001) gets an optional project link (forward-compat: a
--    project may later mount a graduation studio). The user-facing "studio view"
--    is the project's artifact surface below, not this sketch table.
ALTER TABLE studios ADD COLUMN IF NOT EXISTS project_id text REFERENCES projects(id);

-- 5) Artifacts — every tangible thing an agent produces toward a goal. A file on
--    disk, a generated PDF/image, a committed code change, or a live deploy/link.
CREATE TABLE IF NOT EXISTS artifacts (
  id          text PRIMARY KEY,                 -- art_<ulid>
  scope       text NOT NULL,
  project_id  text REFERENCES projects(id),
  goal_id     text REFERENCES goals(id),
  run_id      text,                             -- agent_runs.id that produced it
  task_id     text,                             -- goal_tasks.id that produced it
  kind        text NOT NULL DEFAULT 'file'
              CHECK (kind IN ('file','doc','pdf','image','code','deploy','link','data')),
  title       text NOT NULL,
  path        text,                             -- absolute fs path (file-backed kinds)
  url         text,                             -- external/deploy/link kinds
  bytes       bigint,
  status      text NOT NULL DEFAULT 'created'
              CHECK (status IN ('created','updated','published','archived')),
  summary     text,                             -- what it is / why it matters
  meta        jsonb NOT NULL DEFAULT '{}',
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_artifacts_project ON artifacts (project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_artifacts_goal    ON artifacts (goal_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_run     ON artifacts (run_id) WHERE run_id IS NOT NULL;
