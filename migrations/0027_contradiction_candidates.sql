-- Contradiction candidates: proposals only, never automatic edits.
--
-- The scan finds pairs that cannot both be true (identity predicates with
-- multiple active values, or similar memories the classifier marks as
-- contradictory). A human action applies the resolution; the scan itself
-- never invalidates anything.

CREATE TABLE IF NOT EXISTS contradiction_candidates (
    id              text PRIMARY KEY,
    scope           text NOT NULL,
    kind            text NOT NULL,
    left_id         text NOT NULL,
    right_id        text NOT NULL,
    similarity      double precision,
    reason          text NOT NULL,
    proposed_action text NOT NULL DEFAULT 'review',
    status          text NOT NULL DEFAULT 'open',
    evidence        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now(),
    resolved_at     timestamptz,
    resolved_by     text,
    CONSTRAINT contradiction_kind_check
        CHECK (kind IN ('memory_memory', 'identity_identity')),
    CONSTRAINT contradiction_status_check
        CHECK (status IN ('open', 'dismissed', 'resolved')),
    CONSTRAINT contradiction_action_check
        CHECK (proposed_action IN (
            'supersede_left', 'supersede_right', 'keep_both', 'review'
        ))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_contradiction_open_pair
    ON contradiction_candidates (scope, kind, left_id, right_id)
    WHERE status = 'open';

CREATE INDEX IF NOT EXISTS idx_contradiction_scope_status
    ON contradiction_candidates (scope, status, created_at DESC);
