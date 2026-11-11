-- 0020: knowledge-graph and memory hygiene — the additive half
--
-- Measured 2026-11-11 on the live database before writing this:
--   47,948 edges spread over 8,699 distinct rel_type values. The top 25
--   relations cover 38% of edges; 8,069 edges (17%) use a relation that
--   appears at most twice — 'are_the_only_active_runtime_entities_within',
--   'describes_progression_from_study_to_silence'. Traversal by relation
--   is therefore not possible: every edge carries a private phrase.
--
--   23 groups of entities share a normalised name ('Neti Neti' / 'Neti, Neti'
--   / 'Neti-Neti'; 'curlyos-core' / 'CurlyOS Core'; 'claude code' /
--   'Claude-code'), and the graph's centre is split four ways: Hiten Saxena
--   5,759 edges, User 2,751, assistant 613, CRAZYMAGE 574.
--
--   545 memories are chat scaffolding under 60 characters ('It's live.',
--   'new fact.', 'Pick one or mix.') and sit on the recall surface.
--
-- Nothing here is destructive. Every column and table is additive and
-- idempotent; the backfills live in deploy/graph_hygiene.py so they can be
-- dry-run, and the destructive janitor step needs an explicit --approve.

BEGIN;

-- ── 1. Controlled relation vocabulary ────────────────────────────────────────
-- rel_type keeps the extractor's original phrase; rel_norm is the predicate
-- it maps to (or NULL when nothing fits). Recall and traversal read rel_norm;
-- the original stays readable in rel_type and, for unmatched edges, is what a
-- curator reviews.
ALTER TABLE knowledge_edges ADD COLUMN IF NOT EXISTS rel_norm text;

CREATE INDEX IF NOT EXISTS idx_edges_rel_norm
  ON knowledge_edges (rel_norm);

-- The janitor's duplicate detection scans this triple.
CREATE INDEX IF NOT EXISTS idx_edges_triple
  ON knowledge_edges (src_entity_id, dst_entity_id, rel_type);

-- ── 2. Entity aliases ────────────────────────────────────────────────────────
-- Merge without rewriting edges: the alias table points every spelling at one
-- surviving entity, so nothing is deleted and every historical edge still
-- resolves. Covers both the name collisions above and the identity split
-- (User / HITEN / CRAZYMAGE → Hiten Saxena).
CREATE TABLE IF NOT EXISTS knowledge_entity_aliases (
  alias_id   text PRIMARY KEY,
  entity_id  text NOT NULL REFERENCES knowledge_entities(id) ON DELETE CASCADE,
  alias      text NOT NULL,
  alias_norm text NOT NULL,
  source     text NOT NULL,          -- 'normalised-name' | 'identity-split' | 'manual'
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (alias_norm, entity_id)
);

CREATE INDEX IF NOT EXISTS idx_alias_norm ON knowledge_entity_aliases (alias_norm);
CREATE INDEX IF NOT EXISTS idx_alias_entity ON knowledge_entity_aliases (entity_id);

-- ── 3. Comparable entity names ───────────────────────────────────────────────
-- Case, punctuation and hyphen/space differences are what split the 23 groups
-- above, so matching needs one canonical form. Generated, so it can never
-- drift from `name`.
ALTER TABLE knowledge_entities ADD COLUMN IF NOT EXISTS name_norm text
  GENERATED ALWAYS AS (
    lower(btrim(regexp_replace(
      replace(replace(replace(name, '.', ''), ',', ''), '-', ' '),
      '\s+', ' ', 'g')))
  ) STORED;

CREATE INDEX IF NOT EXISTS idx_entities_name_norm
  ON knowledge_entities (name_norm);

-- ── 4. Chatter flag ──────────────────────────────────────────────────────────
-- A memory is flagged, never deleted: the text stays in episodes and still
-- carries provenance. Default false keeps every existing recall path unchanged
-- until retrieval is taught to skip flagged rows.
ALTER TABLE memories ADD COLUMN IF NOT EXISTS is_chatter boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS idx_memories_chatter
  ON memories (id) WHERE is_chatter;

COMMIT;
