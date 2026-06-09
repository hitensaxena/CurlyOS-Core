"""Knowledge engine smoketest."""
import asyncio
import sys
import os
import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from knowledge.extraction import extract_with_patterns
from knowledge.graph import extract_and_project, k_hop_expand, GRAPH_DDL
from knowledge.resolution import resolve_entity, ResolutionDecision
from shared.events.implementations import PgOnlyPublisher

DSN = "postgresql://curlyos:***@localhost:54321/curlyos"
SCOPE = "user:usr_hiten"

passed = 0
failed = 0

def check(label, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✅ {label}")
    else:
        failed += 1
        print(f"  ❌ {label} — {detail}")


async def main():
    # Apply graph DDL
    conn = psycopg.connect(DSN, autocommit=True)
    conn.execute(GRAPH_DDL)
    conn.close()

    pub = PgOnlyPublisher()

    print("\n[1] Pattern extraction")
    triples = extract_with_patterns(
        "Hiten switched from VS Code to Zed for all development work.",
        "epi_test"
    )
    check("extracted triples", len(triples) >= 1, f"got {len(triples)}")
    for t in triples:
        print(f"    ({t.subject}) --[{t.predicate}]--> ({t.object}) conf={t.confidence}")

    triples2 = extract_with_patterns(
        "Mintrix AI is an AI OS for schools and is Hiten primary project.",
        "epi_test2"
    )
    check("extracted triples from second text", len(triples2) >= 1, f"got {len(triples2)}")
    for t in triples2:
        print(f"    ({t.subject}) --[{t.predicate}]--> ({t.object}) conf={t.confidence}")

    print("\n[2] Entity resolution")
    # Before loading entities, all should be MINT
    d1, k1 = await resolve_entity("Hiten")
    check("unknown entity → MINT", d1 == ResolutionDecision.MINT, f"got {d1}")

    print("\n[3] Full extract + project pipeline on real episodes")
    # Get episodes from the DB
    conn = psycopg.connect(DSN, autocommit=True)
    episodes = conn.execute(
        "SELECT id, content FROM episodes WHERE scope = %s ORDER BY created_at DESC LIMIT 5",
        [SCOPE]
    ).fetchall()
    conn.close()
    check("found episodes in DB", len(episodes) >= 1, f"found {len(episodes)}")

    # Reuse smoketest pool adapter
    _g = {"__file__": os.path.join(os.path.dirname(__file__), "smoketest.py")}
    exec(open(_g["__file__"]).read().split("class FakeRedis")[0], _g)
    SyncPool = _g["SyncPool"]
    pool = SyncPool(DSN)
    total_created = 0
    total_edges = 0
    for epi_id, content in episodes:
        result = await extract_and_project(pool, pub, SCOPE, epi_id, content)
        total_created += result["entities_created"]
        total_edges += result["edges_created"]

    check("entities created", total_created >= 1, f"created {total_created}")
    check("edges created", total_edges >= 1, f"created {total_edges}")

    print("\n[4] K-hop graph expansion")
    conn = psycopg.connect(DSN, autocommit=True)
    entities = conn.execute(
        "SELECT id, name, label FROM knowledge_entities LIMIT 3"
    ).fetchall()
    conn.close()

    if entities:
        seed_ids = [e[0] for e in entities]
        expansion = await k_hop_expand(pool, seed_ids, k=1)
        check("graph expansion returned entities", len(expansion["entities"]) >= 1,
              f"got {len(expansion['entities'])}")
        print(f"    entities: {len(expansion['entities'])}, edges: {len(expansion['edges'])}")
    else:
        check("graph expansion (skipped - no entities)", True)

    total = passed + failed
    print(f"\n{'='*60}")
    print(f"KNOWLEDGE ENGINE SMOKE: {passed}/{total} passed, {failed} failed")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
