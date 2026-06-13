-- 0007_decision_loop.sql — close the decision → outcome → lesson loop.
--
-- A cognitive system learns by comparing what it PREDICTED to what HAPPENED,
-- distilling reusable lessons, and revising mental models. Decisions already
-- carry a flat `outcome` text + review_at/reviewed_at (0004); that records
-- *that* something happened but nothing *learns* from it. This migration adds
-- the two missing moves:
--   1. capture the prediction at decision time (so outcomes are scorable);
--   2. structured outcomes + reusable, embedded lessons that feed back into
--      future decisions via similarity retrieval.
--
-- Additive only. Follows existing idioms: text <prefix>_<ulid> ids, scope on
-- every memory-bearing row, jsonb `properties`/`conditions` extensibility seam,
-- bitemporal-lite (invalidate-not-delete via valid_to), 1024-dim embeddings
-- with HNSW (m=32, ef_construction=200), text[] ref arrays (cf.
-- goals.identity_refs / opportunities.evidence_refs).

-- 1) Capture the PREDICTION at decision time — the falsifiable bet we score later.
--    decisions.outcome (0004) is kept as a human-readable summary cache; the
--    structured record lives in the outcomes table below (decisions.outcome_id).
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS predicted_outcome     text;
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS prediction_confidence real
  CHECK (prediction_confidence IS NULL OR (prediction_confidence >= 0 AND prediction_confidence <= 1));
ALTER TABLE decisions ADD COLUMN IF NOT EXISTS outcome_id            text;  -- -> outcomes.id (set at review)

-- 2) Structured OUTCOMES — what actually happened, scored against the prediction.
--    One decision may accrue several review checkpoints over time, hence its own
--    table rather than more columns on decisions.
CREATE TABLE IF NOT EXISTS outcomes (
  id                 text PRIMARY KEY,              -- out_<ulid>
  scope              text NOT NULL,
  decision_id        text NOT NULL REFERENCES decisions(id),
  goal_id            text REFERENCES goals(id),     -- denormalized for goal-delta scoring
  summary            text NOT NULL,                 -- what actually happened, in prose
  valence            text NOT NULL
                     CHECK (valence IN ('success','partial','failure','mixed','too_early')),
  matched_prediction boolean,                       -- did actual match decisions.predicted_outcome?
  surprise           real                           -- Brier: (prediction_confidence - hit)^2
                     CHECK (surprise IS NULL OR (surprise >= 0 AND surprise <= 1)),
  metrics            jsonb NOT NULL DEFAULT '{}',   -- structured measures {revenue, latency_ms, ...}
  evidence_refs      text[] NOT NULL DEFAULT '{}',  -- epi_/mem_/ent_ ids that show the outcome
  embedding          vector(1024),                  -- retrieve similar past outcomes
  epistemic_status   text NOT NULL DEFAULT 'observed',  -- observed | inferred | reported
  source_episode_id  text REFERENCES episodes(id),
  observed_at        timestamptz NOT NULL DEFAULT now(),
  valid_from         timestamptz NOT NULL DEFAULT now(),
  valid_to           timestamptz,                   -- invalidate-not-delete
  ingested_at        timestamptz NOT NULL DEFAULT now()
);

-- 3) LESSONS — generalized, reusable knowledge distilled from outcome(s).
--    This is the payoff: the Executive's hydration retrieves relevant lessons
--    (embedding similarity + condition gate) into the context of future decisions.
CREATE TABLE IF NOT EXISTS lessons (
  id                  text PRIMARY KEY,             -- les_<ulid>
  scope               text NOT NULL,
  statement           text NOT NULL,                -- "When X under Y, prefer Z because…"
  applies_when        text,                         -- applicability / trigger condition (prose)
  conditions          jsonb NOT NULL DEFAULT '{}',  -- structured match {domain, reversibility, ...}
  confidence          real NOT NULL DEFAULT 0.5 CHECK (confidence >= 0 AND confidence <= 1),
  support_count       int  NOT NULL DEFAULT 1,      -- # outcomes supporting it
  contradiction_count int  NOT NULL DEFAULT 0,      -- # outcomes against it
  derived_from_outcomes text[] NOT NULL DEFAULT '{}', -- out_ ids (provenance)
  applies_to_entities   text[] NOT NULL DEFAULT '{}', -- ent_ ids (KG link)
  updates_model       text REFERENCES mental_models(id),  -- the mental model this revises
  status              text NOT NULL DEFAULT 'provisional'
                      CHECK (status IN ('provisional','validated','retired','contradicted')),
  embedding           vector(1024),
  epistemic_status    text NOT NULL DEFAULT 'provisional',
  properties          jsonb NOT NULL DEFAULT '{}',  -- e.g. last_reinforced_at for decay
  source_episode_id   text REFERENCES episodes(id),
  valid_from          timestamptz NOT NULL DEFAULT now(),
  valid_to            timestamptz,                  -- invalidate-not-delete
  ingested_at         timestamptz NOT NULL DEFAULT now(),
  created_at          timestamptz NOT NULL DEFAULT now()
);

-- Indexes -------------------------------------------------------------------
-- Outcome lookups by decision + cross-decision similarity recall.
CREATE INDEX IF NOT EXISTS idx_outcomes_decision ON outcomes (decision_id) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_outcomes_goal     ON outcomes (goal_id)     WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_outcomes_hnsw ON outcomes
  USING hnsw (embedding vector_cosine_ops) WITH (m=32, ef_construction=200);

-- Active lessons by scope/status + semantic retrieval for the feedback step.
CREATE INDEX IF NOT EXISTS idx_lessons_active ON lessons (scope, status) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_lessons_model  ON lessons (updates_model) WHERE valid_to IS NULL;
CREATE INDEX IF NOT EXISTS idx_lessons_hnsw ON lessons
  USING hnsw (embedding vector_cosine_ops) WITH (m=32, ef_construction=200);
