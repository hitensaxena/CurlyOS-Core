-- Second merge pass: fold surface-form variants + explicit alias merges, drop
-- chat-role/pronoun noise. Bi-temporal soft-delete (valid_to), never DELETE.
\set ON_ERROR_STOP on
BEGIN;

-- Current degree per valid entity (+ alphanumeric-only key for variant grouping).
CREATE TEMP TABLE _deg ON COMMIT DROP AS
SELECT e.id, e.scope, e.name, e.created_at,
       (SELECT count(*) FROM knowledge_edges k
        WHERE (k.src_entity_id = e.id OR k.dst_entity_id = e.id) AND k.valid_to IS NULL) AS deg,
       regexp_replace(lower(e.name), '[^a-z0-9]', '', 'g') AS akey
FROM knowledge_entities e WHERE e.valid_to IS NULL;

-- A. Surface-form variants: canonical = most-connected (tie → oldest).
CREATE TEMP TABLE _map ON COMMIT DROP AS
SELECT id AS dup_id, canonical_id FROM (
  SELECT id,
         first_value(id) OVER (PARTITION BY scope, akey
                               ORDER BY deg DESC, created_at ASC, id ASC) AS canonical_id
  FROM _deg WHERE akey <> ''
) s WHERE id <> canonical_id;

-- B. Explicit alias merges → the canonical 'Hiten' node.
INSERT INTO _map (dup_id, canonical_id)
SELECT d.id, (SELECT id FROM _deg WHERE lower(name)='hiten' ORDER BY deg DESC, created_at ASC LIMIT 1)
FROM _deg d
WHERE lower(d.name) IN ('the user','hiten saxena')
  AND d.id NOT IN (SELECT dup_id FROM _map)
  AND d.id <> (SELECT id FROM _deg WHERE lower(name)='hiten' ORDER BY deg DESC, created_at ASC LIMIT 1);

SELECT count(*) AS entities_to_merge FROM _map;

UPDATE knowledge_edges e SET src_entity_id = m.canonical_id FROM _map m WHERE e.src_entity_id = m.dup_id;
UPDATE knowledge_edges e SET dst_entity_id = m.canonical_id FROM _map m WHERE e.dst_entity_id = m.dup_id;
UPDATE knowledge_entities e
SET valid_to = now(), properties = e.properties || jsonb_build_object('merged_into', m.canonical_id)
FROM _map m WHERE e.id = m.dup_id AND e.valid_to IS NULL;

-- C. Drop chat-role / pronoun pseudo-entities (transcript noise) + their edges.
CREATE TEMP TABLE _drop ON COMMIT DROP AS
SELECT id FROM knowledge_entities
WHERE valid_to IS NULL
  AND lower(name) IN ('assistant','the assistant','you','me','i','the human','human','user');
SELECT count(*) AS noise_entities_dropped FROM _drop;
UPDATE knowledge_edges SET valid_to = now()
WHERE valid_to IS NULL AND (src_entity_id IN (SELECT id FROM _drop) OR dst_entity_id IN (SELECT id FROM _drop));
UPDATE knowledge_entities SET valid_to = now() WHERE valid_to IS NULL AND id IN (SELECT id FROM _drop);

-- Cleanup: self-loops from the merge + parallel edges (keep oldest).
UPDATE knowledge_edges SET valid_to = now() WHERE valid_to IS NULL AND src_entity_id = dst_entity_id;
WITH ranked AS (
  SELECT id, row_number() OVER (PARTITION BY src_entity_id, dst_entity_id, rel_type
                                ORDER BY created_at ASC, id ASC) AS rn
  FROM knowledge_edges WHERE valid_to IS NULL
)
UPDATE knowledge_edges SET valid_to = now() WHERE id IN (SELECT id FROM ranked WHERE rn > 1);

COMMIT;
