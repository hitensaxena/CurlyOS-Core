-- 0009_goal_execution.sql — the goal-execution orchestrator.
--
-- Goals (0004) record WHAT the user wants; nothing executes toward them. This
-- migration adds the execution layer: an orchestrator decomposes a goal into a
-- PLAN of concrete tasks, each task is dispatched to a WORKER (an Executive
-- agent_run), and worker outcomes aggregate back into the goal's progress. A
-- command-chat transcript lets the user steer the orchestrator.
--
-- Additive only. Follows existing idioms: text <prefix>_<ulid> ids, scope on
-- every row, jsonb extensibility seam, CHECK-constrained status enums,
-- bi-temporal-lite is NOT used here (plans/tasks are mutable working state).

-- 1) Link a worker run to the goal it executes toward. Nullable: interactive
--    and scheduled-job runs carry no goal.
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS goal_id text;
CREATE INDEX IF NOT EXISTS agent_runs_goal_idx ON agent_runs (goal_id) WHERE goal_id IS NOT NULL;

-- 2) A decomposition of a goal into executable tasks. A goal may be re-planned;
--    the latest non-abandoned plan is the current one.
CREATE TABLE IF NOT EXISTS goal_plans (
  id          text PRIMARY KEY,                 -- gpl_<ulid>
  scope       text NOT NULL,
  goal_id     text NOT NULL REFERENCES goals(id),
  status      text NOT NULL DEFAULT 'proposed'
              CHECK (status IN ('proposed','approved','executing','done','abandoned')),
  rationale   text NOT NULL DEFAULT '',          -- the orchestrator's reasoning for this breakdown
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS goal_plans_goal_idx ON goal_plans (goal_id, created_at DESC);

-- 3) One executable task within a plan, dispatched to a worker (Executive) run.
CREATE TABLE IF NOT EXISTS goal_tasks (
  id             text PRIMARY KEY,               -- gtk_<ulid>
  scope          text NOT NULL,
  plan_id        text NOT NULL REFERENCES goal_plans(id) ON DELETE CASCADE,
  goal_id        text NOT NULL REFERENCES goals(id),   -- denormalized for direct query
  seq            int  NOT NULL DEFAULT 0,         -- execution order within the plan
  title          text NOT NULL,                  -- short label
  task           text NOT NULL,                  -- the natural-language instruction for the worker
  why            text NOT NULL DEFAULT '',        -- how it advances the goal
  status         text NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','dispatched','running','parked',
                                   'completed','failed','skipped')),
  run_id         text,                            -- -> agent_runs.id of the worker
  result_summary text,                            -- the worker's synthesized output
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS goal_tasks_plan_idx ON goal_tasks (plan_id, seq);
CREATE INDEX IF NOT EXISTS goal_tasks_goal_idx ON goal_tasks (goal_id);
CREATE INDEX IF NOT EXISTS goal_tasks_run_idx  ON goal_tasks (run_id) WHERE run_id IS NOT NULL;

-- 4) The orchestrator command-chat transcript (the "tell it what to do" window).
CREATE TABLE IF NOT EXISTS orchestrator_messages (
  id          text PRIMARY KEY,                   -- omsg_<ulid>
  scope       text NOT NULL,
  goal_id     text REFERENCES goals(id),          -- nullable: global vs goal-scoped chat
  role        text NOT NULL CHECK (role IN ('user','orchestrator')),
  content     text NOT NULL,
  meta        jsonb NOT NULL DEFAULT '{}',         -- actions taken, refs (extensibility seam)
  created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS orchestrator_messages_idx
  ON orchestrator_messages (scope, goal_id, created_at);
