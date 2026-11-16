-- Propose each pair once.
--
-- The original index was partial on status = 'open', so dismissing or
-- resolving a pair let the next weekly scan propose it again. The pair is now
-- unique regardless of status: a dismissed contradiction stays dismissed until
-- the underlying rows change (which changes their ids or validity).

DROP INDEX IF EXISTS idx_contradiction_open_pair;

-- A database that ran 0027 long enough could hold a dismissed row plus a
-- newly re-proposed open row for the same pair. Collapse each group to one
-- row before the unique index, otherwise this migration blocks the ledger.
DELETE FROM contradiction_candidates a
USING contradiction_candidates b
WHERE a.ctid < b.ctid
  AND a.scope = b.scope
  AND a.kind = b.kind
  AND a.left_id = b.left_id
  AND a.right_id = b.right_id;

CREATE UNIQUE INDEX IF NOT EXISTS idx_contradiction_pair
    ON contradiction_candidates (scope, kind, left_id, right_id);
