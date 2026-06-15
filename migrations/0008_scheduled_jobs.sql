-- 0008_scheduled_jobs.sql — user-defined autonomous jobs + their delivery inbox.
--
-- The scheduler (orchestration/scheduler.py) has so far driven only a fixed,
-- CODE-defined job table (consolidation, reflection, narrative, …). This
-- migration adds a USER-defined layer: jobs described in natural language, on a
-- chosen cadence, each firing routed through the Executive agent
-- (Runner.start_run) exactly like an interactive run. The agent's synthesized
-- output is delivered as an inbox_items row — the webapp's "delivery method".
--
-- Additive only. Follows existing idioms: text <prefix>_<ulid> ids, scope on
-- every row, jsonb extensibility seam (cadence_json), CHECK-constrained enums.

-- 1) The user-defined job table. One row per job the user creates in the webapp.
--    cadence_type + cadence_json map 1:1 onto the scheduler's cadence dataclasses
--    (Every / DailyAt / WeeklyAt / MonthlyAt) — see orchestration/user_jobs.py.
CREATE TABLE IF NOT EXISTS scheduled_jobs (
  id            text PRIMARY KEY,                 -- sjob_<ulid>
  scope         text NOT NULL,
  name          text NOT NULL,                    -- display name, unique per scope
  task          text NOT NULL,                    -- the natural-language task description
  cadence_type  text NOT NULL
                CHECK (cadence_type IN ('every','daily_at','weekly_at','monthly_at')),
  cadence_json  jsonb NOT NULL DEFAULT '{}',      -- {"minutes":60} | {"hhmm":"09:00"}
                                                  -- | {"weekdays":[0,2],"hhmm":"09:00"} | {"day":1,"hhmm":"09:00"}
  delivery      text NOT NULL DEFAULT 'inbox'
                CHECK (delivery IN ('inbox')),     -- only the webapp inbox for now
  enabled       boolean NOT NULL DEFAULT true,
  last_fired    timestamptz,
  last_status   text NOT NULL DEFAULT 'never',     -- never|running|completed|failed|parked|skipped
  last_run_id   text,                              -- -> agent_runs.id of the most recent firing
  last_error    text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (scope, name)
);

CREATE INDEX IF NOT EXISTS scheduled_jobs_scope_idx   ON scheduled_jobs (scope);
CREATE INDEX IF NOT EXISTS scheduled_jobs_enabled_idx ON scheduled_jobs (scope, enabled);

-- 2) The delivery inbox. Each completed firing drops one item here; the webapp
--    Inbox surface lists them (unread first) and links back to the run trace.
--    job_id is nullable + SET NULL so deleting a job preserves its past outputs.
CREATE TABLE IF NOT EXISTS inbox_items (
  id          text PRIMARY KEY,                    -- inb_<ulid>
  scope       text NOT NULL,
  job_id      text REFERENCES scheduled_jobs(id) ON DELETE SET NULL,
  run_id      text,                                -- -> agent_runs.id (plain ref; runs may be pruned)
  title       text NOT NULL,                       -- usually the job name + when
  body        text NOT NULL,                       -- the agent's synthesized output
  meta        jsonb NOT NULL DEFAULT '{}',         -- {status, steps, parked, ...} extensibility seam
  read_at     timestamptz,                         -- null = unread
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS inbox_items_scope_created_idx ON inbox_items (scope, created_at DESC);
CREATE INDEX IF NOT EXISTS inbox_items_unread_idx        ON inbox_items (scope) WHERE read_at IS NULL;
CREATE INDEX IF NOT EXISTS inbox_items_job_idx           ON inbox_items (job_id);
