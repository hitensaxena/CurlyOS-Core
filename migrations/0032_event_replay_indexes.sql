-- Event-log access paths.
--
-- Consolidation polls (scope, seq); rebuild and coverage scan by type and
-- scope. The table has only PK/seq indexes, so both were sequential scans.

CREATE INDEX IF NOT EXISTS idx_events_scope_seq
    ON events (scope, seq);

CREATE INDEX IF NOT EXISTS idx_events_type_seq
    ON events (type, seq);
