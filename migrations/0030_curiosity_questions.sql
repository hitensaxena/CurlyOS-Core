-- Curiosity: grounded questions about gaps in the memory graph.
--
-- Questions are proposals for the artist to answer, dismiss or ignore. Nothing
-- here triggers research or writes a fact. Each (scope, basis, subject) is
-- proposed once; a dismissed question does not nag every week.

CREATE TABLE IF NOT EXISTS curiosity_questions (
    id           text PRIMARY KEY,
    scope        text NOT NULL,
    question     text NOT NULL,
    basis        text NOT NULL,
    subject      text NOT NULL,
    evidence     jsonb NOT NULL DEFAULT '{}'::jsonb,
    status       text NOT NULL DEFAULT 'open',
    created_at   timestamptz NOT NULL DEFAULT now(),
    resolved_at  timestamptz,
    resolved_by  text,
    CONSTRAINT curiosity_basis_check
        CHECK (basis IN ('goal_gap', 'entity_gap', 'hypothesis_gap')),
    CONSTRAINT curiosity_status_check
        CHECK (status IN ('open', 'dismissed', 'answered'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_curiosity_subject
    ON curiosity_questions (scope, basis, subject);

CREATE INDEX IF NOT EXISTS idx_curiosity_scope_status
    ON curiosity_questions (scope, status, created_at DESC);
