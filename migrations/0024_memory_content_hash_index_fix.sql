-- 0023 created the active-memory unique index with the predicate
-- `valid_to IS NULL AND content_hash IS NOT NULL`. PostgreSQL's ON CONFLICT
-- inference requires the writer's predicate to imply the index predicate, and
-- `valid_to IS NULL` alone does not imply `content_hash IS NOT NULL`, so
-- `ON CONFLICT (scope, content_hash) WHERE valid_to IS NULL` could not infer
-- the index.
--
-- NULLs are distinct in a unique index, so the `content_hash IS NOT NULL`
-- clause was unnecessary: historical rows with NULL hashes still coexist.
-- Recreate the index with the simpler predicate the writers use.

DROP INDEX IF EXISTS idx_memories_active_content_hash;

CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_active_content_hash
    ON memories (scope, content_hash)
    WHERE valid_to IS NULL;
