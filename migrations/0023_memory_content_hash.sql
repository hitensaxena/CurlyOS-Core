-- Active-memory dedup.
--
-- memories.statement_key is a normalised text key but has no uniqueness
-- constraint, so identical facts can be inserted repeatedly and are only
-- merged later by the 20-minute consolidation pass (which looks at the newest
-- 2,000 rows). content_hash is a SHA-256 of the normalised statement; the
-- partial unique index makes duplicate active facts impossible at write time.
--
-- Existing rows keep content_hash NULL (the index skips NULLs), so this
-- migration never fails on historical duplicates. Consolidation remains the
-- cleanup path for rows written before this migration.

ALTER TABLE memories ADD COLUMN IF NOT EXISTS content_hash text;

CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_active_content_hash
    ON memories (scope, content_hash)
    WHERE valid_to IS NULL AND content_hash IS NOT NULL;
