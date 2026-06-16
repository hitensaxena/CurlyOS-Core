-- 0015: index episodes by (scope, source_ref) for source-prefixed scans.
--
-- The attention engine and narrative compose filter episodes with predicates
-- like `source_ref LIKE 'brain:%'` / `'mind:%'` / `'jrnl:%'` over the recent
-- window, which were seq-scanning the table. text_pattern_ops lets the btree
-- serve left-anchored LIKE prefixes. Partial (source_ref IS NOT NULL) keeps it
-- small. CREATE INDEX IF NOT EXISTS is idempotent; safe to re-run.

CREATE INDEX IF NOT EXISTS idx_episodes_scope_source_ref
  ON episodes (scope, source_ref text_pattern_ops)
  WHERE source_ref IS NOT NULL;
