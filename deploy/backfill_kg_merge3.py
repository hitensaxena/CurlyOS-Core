"""Third merge pass (post full-archive mining). Same reversible bi-temporal
pattern as backfill_kg_merge2.sql, with ONE key change: fold 'user' INTO Hiten
(it now carries real edges like 'User --previously_lived_in--> Delhi') instead of
dropping it as noise. Also folds surface-form variants, drops true chat-role
noise (assistant/pronouns), dedupes self-loops + parallel edges.

Soft-delete only (valid_to + properties.merged_into) -> reversible. DRY_RUN=1
runs everything then ROLLBACKs and just prints the counts.

    set -a; . ./.env; set +a
    DRY_RUN=1 .venv/bin/python deploy/backfill_kg_merge3.py   # preview
    .venv/bin/python deploy/backfill_kg_merge3.py             # apply
"""
import os
import psycopg

DRY = os.environ.get("DRY_RUN") == "1"
dsn = [l.split("=", 1)[1].strip().strip('"') for l in open(".env")
       if l.startswith("CURLYOS_DATABASE_URL=")][0]

conn = psycopg.connect(dsn, autocommit=False)
cur = conn.cursor()


def scalar(sql):
    return cur.execute(sql).fetchone()[0]


before_ent = scalar("SELECT count(*) FROM knowledge_entities WHERE valid_to IS NULL")
before_edge = scalar("SELECT count(*) FROM knowledge_edges WHERE valid_to IS NULL")

cur.execute("""
CREATE TEMP TABLE _deg ON COMMIT DROP AS
SELECT e.id, e.scope, e.name, e.created_at,
       (SELECT count(*) FROM knowledge_edges k
        WHERE (k.src_entity_id=e.id OR k.dst_entity_id=e.id) AND k.valid_to IS NULL) AS deg,
       regexp_replace(lower(e.name), '[^a-z0-9]', '', 'g') AS akey
FROM knowledge_entities e WHERE e.valid_to IS NULL;
""")

# A. surface-form variants: canonical = most-connected (tie -> oldest)
cur.execute("""
CREATE TEMP TABLE _map ON COMMIT DROP AS
SELECT id AS dup_id, canonical_id FROM (
  SELECT id, first_value(id) OVER (PARTITION BY scope, akey
            ORDER BY deg DESC, created_at ASC, id ASC) AS canonical_id
  FROM _deg WHERE akey <> ''
) s WHERE id <> canonical_id;
""")

# B. explicit alias merges INTO Hiten — now includes 'user'
cur.execute("""
INSERT INTO _map (dup_id, canonical_id)
SELECT d.id, (SELECT id FROM _deg WHERE lower(name)='hiten' ORDER BY deg DESC, created_at ASC LIMIT 1)
FROM _deg d
WHERE lower(d.name) IN ('the user','hiten saxena','user')
  AND d.id NOT IN (SELECT dup_id FROM _map)
  AND d.id <> (SELECT id FROM _deg WHERE lower(name)='hiten' ORDER BY deg DESC, created_at ASC LIMIT 1);
""")
to_merge = scalar("SELECT count(*) FROM _map")

cur.execute("UPDATE knowledge_edges e SET src_entity_id=m.canonical_id FROM _map m WHERE e.src_entity_id=m.dup_id")
cur.execute("UPDATE knowledge_edges e SET dst_entity_id=m.canonical_id FROM _map m WHERE e.dst_entity_id=m.dup_id")
cur.execute("""UPDATE knowledge_entities e
  SET valid_to=now(), properties=e.properties || jsonb_build_object('merged_into', m.canonical_id)
  FROM _map m WHERE e.id=m.dup_id AND e.valid_to IS NULL""")

# C. drop true chat-role / pronoun noise (NOT 'user' — folded above)
cur.execute("""
CREATE TEMP TABLE _drop ON COMMIT DROP AS
SELECT id FROM knowledge_entities WHERE valid_to IS NULL
  AND lower(name) IN ('assistant','the assistant','you','me','i','the human','human','they','them','someone');
""")
to_drop = scalar("SELECT count(*) FROM _drop")
cur.execute("UPDATE knowledge_edges SET valid_to=now() WHERE valid_to IS NULL AND (src_entity_id IN (SELECT id FROM _drop) OR dst_entity_id IN (SELECT id FROM _drop))")
cur.execute("UPDATE knowledge_entities SET valid_to=now() WHERE valid_to IS NULL AND id IN (SELECT id FROM _drop)")

# self-loops from the merge
selfloops = scalar("SELECT count(*) FROM knowledge_edges WHERE valid_to IS NULL AND src_entity_id=dst_entity_id")
cur.execute("UPDATE knowledge_edges SET valid_to=now() WHERE valid_to IS NULL AND src_entity_id=dst_entity_id")

# parallel edges -> keep oldest
cur.execute("""
WITH ranked AS (SELECT id, row_number() OVER (PARTITION BY src_entity_id,dst_entity_id,rel_type
                ORDER BY created_at ASC, id ASC) AS rn
                FROM knowledge_edges WHERE valid_to IS NULL)
UPDATE knowledge_edges SET valid_to=now() WHERE id IN (SELECT id FROM ranked WHERE rn>1)""")

after_ent = scalar("SELECT count(*) FROM knowledge_entities WHERE valid_to IS NULL")
after_edge = scalar("SELECT count(*) FROM knowledge_edges WHERE valid_to IS NULL")
hiten_deg = scalar("""SELECT count(*) FROM knowledge_edges k JOIN knowledge_entities e ON e.id IN (k.src_entity_id,k.dst_entity_id)
                      WHERE k.valid_to IS NULL AND e.valid_to IS NULL AND lower(e.name)='hiten'""")

print(f"entities merged (variants+aliases): {to_merge}")
print(f"noise entities dropped           : {to_drop}")
print(f"self-loops retired               : {selfloops}")
print(f"entities: {before_ent} -> {after_ent}   edges: {before_edge} -> {after_edge}")
print(f"Hiten degree now                 : {hiten_deg}")

if DRY:
    conn.rollback()
    print("DRY_RUN -> rolled back (no changes)")
else:
    conn.commit()
    print("COMMITTED")
conn.close()
