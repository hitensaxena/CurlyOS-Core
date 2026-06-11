"""Bridge the remaining disconnected islands into the main component so the
knowledge graph is a single connected whole.

After densify_kg.py the graph is 0-orphan and ~95% one component, but a handful
of small clusters (entities most-similar to each other yet below the backbone
threshold to anything in the main graph) sit apart. For each such island this
adds ONE bridge edge: the island node ↔ its nearest neighbour in the main
component (highest cosine over the whole island). Bridges are tagged
properties.inferred=true + method='bridge', so they reverse with the same
statement as the rest of the densification pass.

    set -a; . ./.env; set +a
    DRY_RUN=1 .venv/bin/python deploy/bridge_kg_components.py
    .venv/bin/python deploy/bridge_kg_components.py
"""
import json
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg
from shared.types.ulid import mint

DSN = os.environ["CURLYOS_DATABASE_URL"]
SCOPE = os.environ.get("CURLYOS_SCOPE", "user:usr_hiten")
DRY_RUN = os.environ.get("DRY_RUN", "") not in ("", "0", "false", "False")


def log(m):
    print(m, flush=True)


def components(ids, edges):
    parent = {i: i for i in ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        if a in parent and b in parent:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb
    comp = defaultdict(list)
    for i in ids:
        comp[find(i)].append(i)
    return list(comp.values())


def main():
    conn = psycopg.connect(DSN, autocommit=True)
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM knowledge_entities WHERE valid_to IS NULL AND scope = %s", (SCOPE,)
    ).fetchall()]
    edges = conn.execute(
        "SELECT src_entity_id, dst_entity_id FROM knowledge_edges WHERE valid_to IS NULL"
    ).fetchall()

    comps = sorted(components(ids, edges), key=len, reverse=True)
    main_comp = set(comps[0])
    islands = comps[1:]
    log(f"dry_run={DRY_RUN} components={len(comps)} main={len(main_comp)} "
        f"islands={len(islands)} nodes_outside={sum(len(c) for c in islands)}")

    main_ids = list(main_comp)
    new = []
    for island in islands:
        best = None  # (island_node, main_node, sim)
        for node in island:
            row = conn.execute(
                """
                SELECT e.id, 1 - (s.embedding <=> e.embedding) AS sim
                FROM knowledge_entities s, knowledge_entities e
                WHERE s.id = %s AND e.id = ANY(%s)
                  AND e.valid_to IS NULL AND e.embedding IS NOT NULL
                ORDER BY s.embedding <=> e.embedding
                LIMIT 1
                """,
                (node, main_ids),
            ).fetchone()
            if row and (best is None or row[1] > best[2]):
                best = (node, row[0], float(row[1]))
        if best:
            a, b, sim = best
            new.append((a, b, round(sim, 3)))

    log(f"bridge edges to add: {len(new)}")
    for a, b, sim in new[:30]:
        log(f"  {a} -> {b}  sim={sim}")

    if DRY_RUN:
        log("DRY_RUN — nothing written.")
        conn.close()
        return

    payload = [(mint("cor"), a, b, "related_to",
                json.dumps({"inferred": True, "method": "bridge", "score": sim}))
               for a, b, sim in new]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO knowledge_edges (id, src_entity_id, dst_entity_id, rel_type, properties) "
            "VALUES (%s, %s, %s, %s, %s)",
            payload,
        )
    log(f"inserted {len(payload)} bridge edges")

    # verify single component
    edges2 = conn.execute(
        "SELECT src_entity_id, dst_entity_id FROM knowledge_edges WHERE valid_to IS NULL"
    ).fetchall()
    comps2 = sorted(components(ids, edges2), key=len, reverse=True)
    log(f"after: components={len(comps2)} largest={len(comps2[0])} "
        f"({100*len(comps2[0])/len(ids):.1f}%)")
    conn.close()
    log("DONE")


if __name__ == "__main__":
    main()
