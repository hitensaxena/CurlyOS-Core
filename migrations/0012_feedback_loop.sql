-- 0012_feedback_loop.sql
-- Goals are achieved IN REALITY through a verify-and-iterate loop: a worker run
-- producing real artifacts (files, commits) is no longer "done" just because it
-- finished — it is VERIFIED against the task's success criteria, and re-dispatched
-- with the critique as context until it passes or attempts are exhausted.
--
--   attempt        — how many times this task has been (re)dispatched (0-based)
--   max_attempts   — retry ceiling before the task is marked failed
--   verify         — how to check this task succeeded (the success criteria the
--                    verifier judges against; emitted by the decomposer)
--   verdict        — the latest verification result {passed, critique, evidence, at}

ALTER TABLE goal_tasks ADD COLUMN IF NOT EXISTS attempt      int  NOT NULL DEFAULT 0;
ALTER TABLE goal_tasks ADD COLUMN IF NOT EXISTS max_attempts int  NOT NULL DEFAULT 2;
ALTER TABLE goal_tasks ADD COLUMN IF NOT EXISTS verify       text;
ALTER TABLE goal_tasks ADD COLUMN IF NOT EXISTS verdict      jsonb;

-- A task being checked sits in 'verifying' between the run finishing and the
-- verdict landing; a rejected task goes back to 'running' on re-dispatch.
ALTER TABLE goal_tasks DROP CONSTRAINT IF EXISTS goal_tasks_status_check;
ALTER TABLE goal_tasks ADD  CONSTRAINT goal_tasks_status_check
  CHECK (status IN ('pending','dispatched','running','parked','verifying',
                    'completed','failed','skipped'));
