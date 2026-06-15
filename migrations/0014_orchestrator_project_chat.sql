-- 0014_orchestrator_project_chat.sql — project-scoped orchestrator conversations.
--
-- orchestrator_messages (0009) is keyed by goal_id (nullable = global). A project
-- clubs several goals, and the webapp's project view needs its OWN conversation
-- thread — distinct from any single goal's. This adds project_id so a message can
-- be scoped to a goal, a project, or global.
--
-- Additive only.

ALTER TABLE orchestrator_messages
  ADD COLUMN IF NOT EXISTS project_id text REFERENCES projects(id);

CREATE INDEX IF NOT EXISTS idx_omsg_project
  ON orchestrator_messages (project_id, created_at)
  WHERE project_id IS NOT NULL;
