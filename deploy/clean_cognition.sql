-- Cognition/identity cleanup (deterministic part; principles consolidation is a
-- separate LLM pass). Themes/life_chapters/identity_facts soft-delete (valid_to);
-- reflection_reports hard-delete (no valid_to). Identity occupation conflict is
-- left untouched (flagged for the user).
\set ON_ERROR_STOP on
BEGIN;

-- THEMES: retire stopword / sentence-fragment "themes" (keep real topics).
UPDATE themes SET valid_to = now()
WHERE valid_to IS NULL AND lower(name) IN (
  'all','if','it','no','so','when','each','two','one','none',
  'added','check','fixed','got','live','loading','result','root','tell','try','want','test'
);

-- LIFE_CHAPTERS: dedup exact-duplicate titles (keep oldest).
WITH r AS (
  SELECT id, row_number() OVER (PARTITION BY title ORDER BY created_at ASC, id ASC) rn
  FROM life_chapters WHERE valid_to IS NULL
)
UPDATE life_chapters SET valid_to = now() WHERE id IN (SELECT id FROM r WHERE rn > 1);

-- REFLECTION_REPORTS: drop identical re-runs (same type+window+scan; keep oldest).
WITH r AS (
  SELECT id, row_number() OVER (
    PARTITION BY report_type, time_window_start, time_window_end, episodes_scanned
    ORDER BY created_at ASC, id ASC) rn
  FROM reflection_reports
)
DELETE FROM reflection_reports WHERE id IN (SELECT id FROM r WHERE rn > 1);

-- IDENTITY_FACTS normalization (load-bearing; targeted, conservative):
UPDATE identity_facts SET predicate = lower(predicate)
WHERE valid_to IS NULL AND predicate <> lower(predicate);                       -- Work -> work
UPDATE identity_facts SET valid_to = now()
WHERE valid_to IS NULL AND predicate = 'has_property' AND object ILIKE 'name:%'; -- dup of name=Hiten
UPDATE identity_facts SET valid_to = now()
WHERE valid_to IS NULL AND predicate = 'preference' AND object ILIKE '%zed%editor%'; -- dup of prefers_editor=Zed
UPDATE identity_facts SET predicate = 'music_preference', object = 'techno music'
WHERE valid_to IS NULL AND predicate = 'stated_preference' AND object ILIKE '%techno%';

COMMIT;
