-- One-off, idempotent: fold duplicate knowledge_entities (same scope + lowercased
-- name) into the oldest canonical node, repoint edges, drop self-loops + parallel
-- edges. Bi-temporal soft-delete (valid_to), never DELETE — replay/history stay intact.
\set ON_ERROR_STOP on
BEGIN;

CREATE TEMP TABLE _canon ON COMMIT DROP AS
SELECT id,
       first_value(id) OVER (
         PARTITION BY scope, lower(name) ORDER BY created_at ASC, id ASC
       ) AS canonical_id
FROM knowledge_entities
WHERE valid_to IS NULL;

CREATE TEMP TABLE _map ON COMMIT DROP AS
SELECT id AS dup_id, canonical_id FROM _canon WHERE id <> canonical_id;

SELECT count(*) AS dup_entities_to_merge FROM _map;

-- Repoint edges off the duplicates onto the canonical node.
UPDATE knowledge_edges e SET src_entity_id = m.canonical_id
FROM _map m WHERE e.src_entity_id = m.dup_id;
UPDATE knowledge_edges e SET dst_entity_id = m.canonical_id
FROM _map m WHERE e.dst_entity_id = m.dup_id;

-- Retire the duplicate entity rows (record where they folded to).
UPDATE knowledge_entities e
SET valid_to = now(),
    properties = e.properties || jsonb_build_object('merged_into', m.canonical_id)
FROM _map m WHERE e.id = m.dup_id AND e.valid_to IS NULL;

-- Drop self-loops produced by the merge.
UPDATE knowledge_edges SET valid_to = now()
WHERE valid_to IS NULL AND src_entity_id = dst_entity_id;

-- Collapse parallel edges (same src, dst, rel_type) — keep the oldest.
WITH ranked AS (
  SELECT id, row_number() OVER (
           PARTITION BY src_entity_id, dst_entity_id, rel_type
           ORDER BY created_at ASC, id ASC
         ) AS rn
  FROM knowledge_edges WHERE valid_to IS NULL
)
UPDATE knowledge_edges SET valid_to = now()
WHERE id IN (SELECT id FROM ranked WHERE rn > 1);

COMMIT;
