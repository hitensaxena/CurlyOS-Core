-- Prospective memory: things the system should bring back at a future time.
--
-- An intention is surfaced once (status pending -> surfaced, event emitted).
-- It stays visible in the due endpoint until completed, dismissed or snoozed.
-- Decision reviews are materialized from decisions.review_at, which until now
-- stored a reminder that nothing ever fired.

CREATE TABLE IF NOT EXISTS intentions (
    id           text PRIMARY KEY,
    scope        text NOT NULL,
    text         text NOT NULL,
    due_at       timestamptz,
    context      text,
    status       text NOT NULL DEFAULT 'pending',
    source       text NOT NULL DEFAULT 'manual',
    source_ref   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    surfaced_at  timestamptz,
    completed_at timestamptz,
    snooze_until timestamptz,
    CONSTRAINT intention_status_check
        CHECK (status IN ('pending', 'surfaced', 'done', 'dismissed')),
    CONSTRAINT intention_source_check
        CHECK (source IN ('manual', 'decision_review', 'reflection'))
);

-- One intention per decision (or other source record). Manual intentions have
-- no source_ref and are never deduplicated.
CREATE UNIQUE INDEX IF NOT EXISTS idx_intention_source_ref
    ON intentions (scope, source, source_ref)
    WHERE source_ref IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_intention_due
    ON intentions (scope, status, due_at);
