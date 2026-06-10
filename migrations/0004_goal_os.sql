-- 0004_goal_os.sql — the Goal Operating System (curlyos-final Phase G).
-- Three tables, no new engine (curlyos-final/04 §1): opportunities are written
-- by a scheduled workflow (Phase X) or manually, goals are read by the
-- Executive's hydration and scored by reflection's goal-delta logic, decisions
-- get a review nudge from the scheduler when review_at passes.
-- Goals are bi-temporal-lite: invalidate-not-delete via valid_to.
-- (`properties` on goals is the extensibility seam — reflection writes
-- last_reflection there; momentum/notes accrue without schema churn.)

CREATE TABLE IF NOT EXISTS goals (
  id            text PRIMARY KEY,              -- goal_<ulid>
  scope         text NOT NULL,                 -- every memory-bearing row carries scope (04 §2)
  parent_id     text REFERENCES goals(id),     -- hierarchy: direction → goal → subgoal
  title         text NOT NULL,
  description   text,
  horizon       text CHECK (horizon IN ('life','year','quarter','month')),
  status        text NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','paused','achieved','abandoned')),
  priority      int  NOT NULL DEFAULT 0,
  identity_refs text[] NOT NULL DEFAULT '{}',  -- idf_ ids this goal serves (alignment edges)
  project_refs  text[] NOT NULL DEFAULT '{}',  -- prj_ ids executing it
  success_criteria text,
  progress      real NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 1),
  properties    jsonb NOT NULL DEFAULT '{}',
  source_episode_id text REFERENCES episodes(id),
  valid_from    timestamptz NOT NULL DEFAULT now(),
  valid_to      timestamptz,                   -- invalidate-not-delete
  ingested_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS decisions (
  id            text PRIMARY KEY,              -- dec_<ulid>
  scope         text NOT NULL,
  title         text NOT NULL,
  context       text,                          -- situation at decision time
  options_considered jsonb NOT NULL DEFAULT '[]',
  chosen        text NOT NULL,
  rationale     text NOT NULL,
  reversibility text CHECK (reversibility IN ('reversible','costly','one_way')),
  goal_id       text REFERENCES goals(id),
  review_at     timestamptz,                   -- when to check the outcome
  outcome       text,                          -- filled at review
  audit_id      text,                          -- dau_ link once meta-cognition reviews it
  decided_at    timestamptz NOT NULL DEFAULT now(),
  reviewed_at   timestamptz,
  source_episode_id text REFERENCES episodes(id)
);

CREATE TABLE IF NOT EXISTS opportunities (
  id            text PRIMARY KEY,              -- opp_<ulid>
  scope         text NOT NULL,
  title         text NOT NULL,
  description   text NOT NULL,
  source        text NOT NULL,                 -- 'discovery_scan' | 'manual' | 'reflection'
  evidence_refs text[] NOT NULL DEFAULT '{}',  -- mem_/epi_/ent_ ids that triggered it
  novelty       real, value_est real, feasibility real,  -- 0..1 each
  score         real,                          -- combined, set by scorer
  status        text NOT NULL DEFAULT 'detected'
                CHECK (status IN ('detected','scored','accepted','rejected','expired')),
  resolution    text,                          -- goal_id/prj_id if accepted; reason if rejected
  detected_at   timestamptz NOT NULL DEFAULT now(),
  resolved_at   timestamptz
);

CREATE INDEX IF NOT EXISTS idx_goals_active   ON goals (scope, status) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_goals_parent   ON goals (parent_id) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_decisions_review ON decisions (review_at)
  WHERE outcome IS NULL AND review_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_opportunities_status ON opportunities (status, detected_at DESC);
