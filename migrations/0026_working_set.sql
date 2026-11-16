-- Working memory: a small, salience-ranked set of memories kept warm per scope.
--
-- This is not a recall cache. It is the cognitive layer's answer to "what is
-- currently relevant": goals, identity, recency and explicit pins. Recall uses
-- it as a bounded boost; the refresh engine recomputes it every 15 minutes.
--
-- Pinned rows never expire on their own and are never overwritten by a refresh.

CREATE TABLE IF NOT EXISTS working_set (
    scope        text NOT NULL,
    memory_id    text NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    salience     double precision NOT NULL DEFAULT 0.5,
    pinned       boolean NOT NULL DEFAULT false,
    reason       text,
    added_at     timestamptz NOT NULL DEFAULT now(),
    refreshed_at timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz,
    PRIMARY KEY (scope, memory_id),
    CONSTRAINT working_set_salience_range CHECK (salience >= 0 AND salience <= 1)
);

CREATE INDEX IF NOT EXISTS idx_working_set_scope_salience
    ON working_set (scope, salience DESC);

CREATE INDEX IF NOT EXISTS idx_working_set_expiry
    ON working_set (expires_at) WHERE expires_at IS NOT NULL;
