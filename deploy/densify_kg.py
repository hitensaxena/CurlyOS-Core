"""Densify the knowledge graph so it's properly connected instead of a single
Hiten-centred star (327 of 1168 edges touched Hiten; ~75% of nodes were
degree-1 leaves and 49 were orphans).

Adds cross-links between related entities using HONEST, deterministic,
reversible signals — no LLM-invented relationships:

  1. similarity backbone   embedding cosine top-K neighbours >= SIM_STRONG
                           -> rel_type 'related_to'
  2. co-occurrence + sim    entities sharing a source episode AND cosine
                           >= SIM_COOCC (the bare co-occurrence set is noisy:
                           5053 pairs, none sharing >1 episode) -> 'co_occurs_with'
  3. orphan rescue          any node still at degree 0 links to its single
                           nearest neighbour (whatever the sim) -> 'related_to'

Hiten is excluded from BOTH ends of every new edge (it is already the hub).
Every new edge carries properties.inferred=true (+ method, score), so the whole
pass reverses with one statement:

    UPDATE knowledge_edges SET valid_to = now()
    WHERE valid_to IS NULL AND properties->>'inferred' = 'true';

Usage (fresh process so it doesn't need the running service):

    set -a; . ./.env; set +a
    DRY_RUN=1 .venv/bin/python deploy/densify_kg.py    # preview only, writes nothing
    .venv/bin/python deploy/densify_kg.py              # apply
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

SIM_STRONG = float(os.environ.get("SIM_STRONG", "0.70"))  # similarity backbone
SIM_COOCC = float(os.environ.get("SIM_COOCC", "0.60"))    # co-occurrence floor
TOPK = int(os.environ.get("TOPK", "5"))                   # neighbours per node


def log(m):
    print(m, flush=True)


def norm(a, b):
    return (a, b) if a < b else (b, a)


def main():
    conn = psycopg.connect(DSN, autocommit=True)

    hiten = {r[0] for r in conn.execute(
        "SELECT id FROM knowledge_entities WHERE valid_to IS NULL AND lower(name) = 'hiten'"
    ).fetchall()}
    log(f"scope={SCOPE} dry_run={DRY_RUN} sim_strong={SIM_STRONG} sim_coocc={SIM_COOCC} "
        f"topk={TOPK} hiten_ids={len(hiten)}")

    # current degree of every valid entity
    deg = defaultdict(int)
    all_ids = set()
    for (eid,) in conn.execute(
        "SELECT id FROM knowledge_entities WHERE valid_to IS NULL AND scope = %s", (SCOPE,)
    ).fetchall():
        all_ids.add(eid)
        deg[eid] = 0
    for a, b in conn.execute(
        "SELECT src_entity_id, dst_entity_id FROM knowledge_edges WHERE valid_to IS NULL"
    ).fetchall():
        deg[a] += 1
        deg[b] += 1

    existing = set()
    for a, b in conn.execute(
        "SELECT src_entity_id, dst_entity_id FROM knowledge_edges WHERE valid_to IS NULL"
    ).fetchall():
        existing.add(norm(a, b))

    orphans_before = sum(1 for i in all_ids if deg[i] == 0)
    deg1_before = sum(1 for i in all_ids if deg[i] == 1)
    log(f"before: {len(all_ids)} entities, {len(existing)} edges, "
        f"{orphans_before} orphans, {deg1_before} degree-1")

    # ── 1. similarity top-K neighbours (and nearest neighbour for orphan rescue) ──
    hfilter = ""
    params: dict = {"scope": SCOPE, "topk": TOPK}
    if hiten:
        hfilter = "AND e.id NOT IN (SELECT id FROM knowledge_entities WHERE lower(name)='hiten')"
    rows = conn.execute(
        f"""
        WITH ents AS (
          SELECT id, embedding FROM knowledge_entities
          WHERE valid_to IS NULL AND scope = %(scope)s AND embedding IS NOT NULL
            AND lower(name) <> 'hiten'
        )
        SELECT s.id AS a, n.id AS b, (1 - (s.embedding <=> n.embedding)) AS sim
        FROM ents s
        CROSS JOIN LATERAL (
          SELECT e.id, e.embedding FROM knowledge_entities e
          WHERE e.valid_to IS NULL AND e.embedding IS NOT NULL AND e.id <> s.id {hfilter}
          ORDER BY s.embedding <=> e.embedding
          LIMIT %(topk)s
        ) n
        """,
        params,
    ).fetchall()

    nearest = {}  # entity -> (neighbour, sim) for the closest neighbour (rank 1)
    sim_pairs = {}  # norm(a,b) -> sim, for sim >= SIM_STRONG
    for a, b, sim in rows:
        if a not in nearest or sim > nearest[a][1]:
            nearest[a] = (b, sim)
        if sim >= SIM_STRONG:
            key = norm(a, b)
            sim_pairs[key] = max(sim, sim_pairs.get(key, 0.0))

    # ── 2. co-occurrence pairs that are also semantically similar ─────────────
    cooc_rows = conn.execute(
        """
        WITH ents AS (
          SELECT id, source_episode_id sep, embedding FROM knowledge_entities
          WHERE valid_to IS NULL AND scope = %(scope)s
            AND source_episode_id IS NOT NULL AND embedding IS NOT NULL
            AND lower(name) <> 'hiten'
        )
        SELECT a.id, b.id, (1 - (a.embedding <=> b.embedding)) sim, a.sep
        FROM ents a JOIN ents b ON a.sep = b.sep AND a.id < b.id
        """,
        {"scope": SCOPE},
    ).fetchall()
    cooc_pairs = {}  # norm(a,b) -> (sim, episode)
    for a, b, sim, sep in cooc_rows:
        if sim >= SIM_COOCC:
            key = norm(a, b)
            if key not in cooc_pairs or sim > cooc_pairs[key][0]:
                cooc_pairs[key] = (sim, sep)

    # ── merge candidates: co-occurrence (richer) > similarity ─────────────────
    candidates = {}  # norm(a,b) -> (rel_type, props)
    for (a, b), (sim, sep) in cooc_pairs.items():
        if (a, b) in existing:
            continue
        candidates[(a, b)] = ("co_occurs_with",
                              {"inferred": True, "method": "cooccurrence",
                               "score": round(float(sim), 3), "episode": sep})
    for (a, b), sim in sim_pairs.items():
        if (a, b) in existing or (a, b) in candidates:
            continue
        candidates[(a, b)] = ("related_to",
                              {"inferred": True, "method": "similarity",
                               "score": round(float(sim), 3)})

    # provisional degrees after similarity+cooccurrence
    prov = dict(deg)
    for (a, b) in candidates:
        prov[a] += 1
        prov[b] += 1

    # ── 3. orphan rescue: anything still at degree 0 -> nearest neighbour ─────
    rescued = 0
    for i in all_ids:
        if prov.get(i, 0) != 0 or i in hiten:
            continue
        nb = nearest.get(i)
        if not nb:
            continue
        b, sim = nb
        key = norm(i, b)
        if key in existing or key in candidates:
            continue
        candidates[key] = ("related_to",
                           {"inferred": True, "method": "orphan_rescue",
                            "score": round(float(sim), 3)})
        prov[key[0]] += 1
        prov[key[1]] += 1
        rescued += 1

    # ── projected impact ──────────────────────────────────────────────────────
    by_method = defaultdict(int)
    for _, (_, props) in candidates.items():
        by_method[props["method"]] += 1
    final = dict(deg)
    for (a, b) in candidates:
        final[a] += 1
        final[b] += 1
    orphans_after = sum(1 for i in all_ids if final.get(i, 0) == 0)
    deg1_after = sum(1 for i in all_ids if final.get(i, 0) == 1)

    log(f"candidate new edges: {len(candidates)} "
        f"(similarity={by_method['similarity']} cooccurrence={by_method['cooccurrence']} "
        f"orphan_rescue={by_method['orphan_rescue']})")
    log(f"projected: edges {len(existing)} -> {len(existing) + len(candidates)}, "
        f"orphans {orphans_before} -> {orphans_after}, degree-1 {deg1_before} -> {deg1_after}")

    if DRY_RUN:
        log("DRY_RUN — nothing written.")
        conn.close()
        return

    # ── apply ─────────────────────────────────────────────────────────────────
    payload = [(mint("cor"), a, b, rel, json.dumps(props))
               for (a, b), (rel, props) in candidates.items()]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO knowledge_edges (id, src_entity_id, dst_entity_id, rel_type, properties) "
            "VALUES (%s, %s, %s, %s, %s)",
            payload,
        )
    log(f"inserted {len(payload)} edges")
    conn.close()
    log("DONE")


if __name__ == "__main__":
    main()
