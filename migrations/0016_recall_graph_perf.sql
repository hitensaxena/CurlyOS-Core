-- 0016 — recall/graph performance indexes.
--
-- Reconstructed 2026-11-11. The original file was applied on 2026-06-16 11:15 UTC
-- (it is in the schema_migrations ledger) but was never committed and survives in
-- no git history. The body below is derived from the live database: these are the
-- only two objects that exist in `curlyos` and are defined by no other migration.
-- Both are already present in the current database, so applying this file there is
-- a no-op; it exists so a fresh install reaches the same schema.
--
-- Both indexes cover the recall join that walks memories -> knowledge_entities
-- through the shared source episode. Without them each recall fans out to a
-- sequential scan of both tables.

CREATE INDEX IF NOT EXISTS idx_memories_source_episode
  ON memories (source_episode_id);

CREATE INDEX IF NOT EXISTS idx_ke_source_episode_full
  ON knowledge_entities (source_episode_id);
