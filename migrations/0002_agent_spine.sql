-- 0002_agent_spine.sql — execution-control + safety tables (curlyos-final Phase F.2)
-- Adapted from the build repo's validated migrations 0005+0006 (spec
-- 12-phase1-build-spec/schemas.md §1.7–1.8). Recorded adaptations, per
-- curlyos-final/04-domain-models.md §1:
--   * approvals.run_id is NULLABLE + origin column: human-initiated approvals
--     (e.g. webapp forget(hard)) need no synthetic agent run; scope checks for
--     those rows use approvals.scope directly.
--   * approvals gains scope / payload / decided_at for the Mission Control
--     approval queue and the human path.
--   * agent_runs gains task / result / error / finished_at and a 'parked'
--     status (LangGraph interrupt parking), since scheduled workflows and the
--     Executive both record their outcome here.
-- The autonomy CHECK keeps the Phase-A ceiling (suggest_only|confirm_each);
-- widening it later is a deliberate schema migration, not a config toggle.
-- This also fixes the latent defect: memory.governance.forget() references
-- approvals, which had no DDL anywhere in curlyos-core.

CREATE TABLE IF NOT EXISTS agent_runs (
  id                text PRIMARY KEY,              -- 'run_...'
  agent             text NOT NULL,                 -- 'Executive' | 'workflow:<name>'
  scope             text NOT NULL,
  task              text,                          -- what was asked
  status            text NOT NULL DEFAULT 'running'
                      CHECK (status IN ('running','parked','completed','failed','cancelled')),
  autonomy_level    text NOT NULL DEFAULT 'confirm_each'
                      CHECK (autonomy_level IN ('suggest_only','confirm_each')),  -- Phase-A ceiling
  parent_run_id     text REFERENCES agent_runs(id),
  source_episode_id text REFERENCES episodes(id),
  result            jsonb,                         -- synthesized outcome
  error             text,
  created_at        timestamptz NOT NULL DEFAULT now(),
  finished_at       timestamptz
);

CREATE TABLE IF NOT EXISTS actions (
  id          text PRIMARY KEY,                    -- 'act_...'
  run_id      text NOT NULL REFERENCES agent_runs(id),
  kind        text NOT NULL,                       -- action_class
  payload     jsonb NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS observations (
  id          text PRIMARY KEY,                    -- 'obs_...'
  action_id   text NOT NULL UNIQUE REFERENCES actions(id),
  result      jsonb NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tool_calls (            -- hash-chained audit
  id          text PRIMARY KEY,                    -- 'tcl_...'
  action_id   text REFERENCES actions(id),
  tool        text NOT NULL,
  args        jsonb NOT NULL,
  result_hash bytea,
  prev_hash   bytea,
  entry_hash  bytea NOT NULL,                      -- entry_hash = H(prev_hash || canonical_row)
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS approvals (             -- 'apv_'
  id            text PRIMARY KEY,
  run_id        text REFERENCES agent_runs(id),    -- NULL for human-originated approvals
  origin        text NOT NULL DEFAULT 'agent'
                  CHECK (origin IN ('agent','human')),
  scope         text NOT NULL,
  action_class  text NOT NULL,
  payload       jsonb,                             -- what is being approved (display + audit)
  state         text NOT NULL DEFAULT 'pending'
                  CHECK (state IN ('pending','granted','denied','expired')),
  expires_at    timestamptz NOT NULL,
  decided_at    timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  CHECK (origin = 'human' OR run_id IS NOT NULL)   -- agent-originated approvals must carry a run
);

CREATE TABLE IF NOT EXISTS capability_grants (     -- 'cap_' (deny-by-default; minimal shape)
  id          text PRIMARY KEY,
  run_id      text NOT NULL REFERENCES agent_runs(id),
  tool        text NOT NULL,                       -- e.g. 'read' | 'memory_write'
  scope       text NOT NULL,
  expires_at  timestamptz NOT NULL,                -- time-boxed
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS budget_ledger (         -- 'bgt_' (daily reconciliation of Redis counters)
  id        text PRIMARY KEY,
  scope     text NOT NULL,                         -- 'per_run' | 'per_agent_day' | 'per_user_day'
  dims      jsonb NOT NULL,                        -- {tokens, tool_actions, usd_spend, wall_clock_seconds}
  consumed  jsonb NOT NULL,
  "limit"   jsonb NOT NULL,
  day       date NOT NULL,
  UNIQUE (scope, day)
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_status  ON agent_runs (status);
CREATE INDEX IF NOT EXISTS idx_agent_runs_created ON agent_runs (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_actions_run        ON actions (run_id);
CREATE INDEX IF NOT EXISTS idx_toolcalls_action   ON tool_calls (action_id);
CREATE INDEX IF NOT EXISTS idx_approvals_pending  ON approvals (state) WHERE state = 'pending';
CREATE INDEX IF NOT EXISTS idx_capgrants_run      ON capability_grants (run_id);
