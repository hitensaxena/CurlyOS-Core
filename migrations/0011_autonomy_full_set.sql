-- 0011_autonomy_full_set.sql — allow the full autonomy ladder on agent_runs.
--
-- 0002 capped agent_runs.autonomy_level to the Phase-A set
-- (suggest_only | confirm_each). The agent "bypass" mode runs at full_auto, so
-- relax the CHECK to the complete ladder. The PDP still enforces per-class
-- floors (self_modify, memory_forget_hard, kill-switch) regardless of level.

ALTER TABLE agent_runs DROP CONSTRAINT IF EXISTS agent_runs_autonomy_level_check;
ALTER TABLE agent_runs ADD CONSTRAINT agent_runs_autonomy_level_check
  CHECK (autonomy_level = ANY (ARRAY[
    'suggest_only'::text, 'confirm_each'::text, 'bounded_auto'::text, 'full_auto'::text
  ]));
