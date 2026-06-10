-- 0006_evolution.sql — versioned prompts behind the self_modify dual gate
-- (curlyos-final Phase E). The system's LLM-facing prompts become data:
-- candidates are proposed, eval-gated against golden datasets (deterministic
-- constraint scoring — no LLM judge in v1), and activated ONLY with both
-- gates satisfied (eval pass AND granted self_modify approval — the PDP's
-- dual-gate logic, exercised for real). 'held' is the gate refusing a
-- regression — the spec's Phase-4 exit criterion made a row state.

CREATE TABLE IF NOT EXISTS prompt_versions (
  id            text PRIMARY KEY,              -- pmt_<ulid>
  scope         text NOT NULL,
  name          text NOT NULL,                 -- e.g. 'executive.plan'
  version       int  NOT NULL,
  content       text NOT NULL,
  status        text NOT NULL DEFAULT 'candidate'
                CHECK (status IN ('candidate','active','held','retired')),
  eval_run_id   text,                          -- evr_ link (gate 1)
  approval_id   text,                          -- apv_ link (gate 2)
  proposed_by   text NOT NULL DEFAULT 'manual',-- 'manual' | 'meta_cognition'
  notes         text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  activated_at  timestamptz,
  UNIQUE (name, version)
);

CREATE INDEX IF NOT EXISTS idx_prompt_versions_active
  ON prompt_versions (name) WHERE status = 'active';
