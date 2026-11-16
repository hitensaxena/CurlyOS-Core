-- Propose each pair once.
--
-- The original index was partial on status = 'open', so dismissing or
-- resolving a pair let the next weekly scan propose it again. The pair is now
-- unique regardless of status: a dismissed contradiction stays dismissed until
-- the underlying rows change (which changes their ids or validity).

DROP INDEX IF EXISTS idx_contradiction_open_pair;

CREATE UNIQUE INDEX IF NOT EXISTS idx_contradiction_pair
    ON contradiction_candidates (scope, kind, left_id, right_id);
