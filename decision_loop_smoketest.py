"""Decision → Outcome → Lesson loop smoketest.

Exercises the full loop end-to-end against the local Postgres, plus the
KG-mirror variant. All data work runs in a single transaction that is rolled
back at the end, so the smoketest leaves no residue (only the migration, which
is idempotent, commits).

    cd ~/curlyos-core
    .venv/bin/python3 decision_loop_smoketest.py
"""

import asyncio
import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from migrate import run_migrations
from shared.embeddings.implementations import HashEmbedder
from shared.types.ulid import mint
from cognition.decision_loop import (
    record_outcome,
    distill_or_reinforce_lesson,
    retrieve_lessons,
    mirror_lesson_to_kg,
    _brier_surprise,
)

SCOPE = "test:decision_loop"

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


def _dsn() -> str:
    dsn = os.environ.get("CURLYOS_DATABASE_URL", "")
    if dsn:
        return dsn
    env = Path(__file__).resolve().parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("CURLYOS_DATABASE_URL"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("CURLYOS_DATABASE_URL not set (env or .env)")


async def main():
    dsn = _dsn()
    embedder = HashEmbedder()

    async def embed(text: str):
        return (await embedder.embed([text]))[0]

    print("\n[0] Apply migrations (idempotent, includes 0007)")
    applied = run_migrations(dsn)
    check("migrations run without error", True, "")
    print(f"    applied this run: {applied or '(none pending)'}")

    # Confirm 0007 objects exist.
    with psycopg.connect(dsn, autocommit=True) as c:
        cols = c.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='decisions' AND column_name IN "
            "('predicted_outcome','prediction_confidence','outcome_id')"
        ).fetchall()
        check("decisions prediction columns exist", len(cols) == 3, f"got {len(cols)}")
        tbls = c.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name IN ('outcomes','lessons')"
        ).fetchall()
        check("outcomes + lessons tables exist", len(tbls) == 2, f"got {len(tbls)}")

    # Pure-function check (no DB).
    print("\n[1] Brier surprise scoring")
    check("confident + wrong → high surprise",
          abs(_brier_surprise(0.8, False) - 0.64) < 1e-9, _brier_surprise(0.8, False))
    check("confident + right → low surprise",
          abs(_brier_surprise(0.8, True) - 0.04) < 1e-9, _brier_surprise(0.8, True))
    check("no prediction → None", _brier_surprise(None, True) is None)

    # Everything below runs in ONE transaction, rolled back at the end.
    conn = psycopg.connect(dsn, autocommit=False)
    try:
        # Seed a KG entity for the lesson to apply to, and a decision with a bet.
        ent_target = mint("ent")
        conn.execute(
            "INSERT INTO knowledge_entities (id, scope, name, label) "
            "VALUES (%s, %s, %s, 'Project')",
            (ent_target, SCOPE, "Self-hosted EC2 box"),
        )
        dec_id = mint("dec")
        conn.execute(
            "INSERT INTO decisions "
            "(id, scope, title, context, chosen, rationale, reversibility, "
            " predicted_outcome, prediction_confidence) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'reversible', %s, 0.8)",
            (dec_id, SCOPE, "Run CurlyOS on a single EC2 box",
             "Need always-on Postgres+Redis", "single m7i-flex.large",
             "cheapest path to always-on", "stays cheap and low-maintenance"),
        )

        print("\n[2] record_outcome (prediction was wrong)")
        out_emb = await embed("the EC2 box was costly and got stopped to save money")
        out_id = record_outcome(
            conn, scope=SCOPE, decision_id=dec_id,
            summary="Box cost ~$58/mo, had to stop it and delete the volume",
            valence="failure", embedding=out_emb,
            matched_prediction=False,
            metrics={"monthly_usd": 58.5},
            evidence_refs=[],
        )
        orow = conn.execute(
            "SELECT surprise, valence, matched_prediction FROM outcomes WHERE id=%s",
            (out_id,),
        ).fetchone()
        check("outcome row created", orow is not None)
        check("surprise = Brier(0.8, miss) = 0.64", orow and abs(orow[0] - 0.64) < 1e-9,
              orow[0] if orow else None)
        drow = conn.execute(
            "SELECT outcome_id, reviewed_at FROM decisions WHERE id=%s", (dec_id,)
        ).fetchone()
        check("decision.outcome_id backfilled", drow and drow[0] == out_id, drow)
        check("decision.reviewed_at stamped", drow and drow[1] is not None)

        print("\n[3] distill_or_reinforce_lesson — first time creates")
        statement = "When choosing always-on infra, a stopped EC2's EBS still bills; prefer scale-to-zero (Neon) for personal projects."
        les_emb = await embed(statement)
        les_id, action = distill_or_reinforce_lesson(
            conn, scope=SCOPE, statement=statement, embedding=les_emb,
            derived_from_outcomes=[out_id], applies_when="personal infra cost decisions",
            conditions={"domain": "infra"}, applies_to_entities=[ent_target],
        )
        check("first distill → created", action == "created", action)
        lrow = conn.execute(
            "SELECT support_count, confidence, status FROM lessons WHERE id=%s", (les_id,)
        ).fetchone()
        check("new lesson support_count = 1", lrow and lrow[0] == 1, lrow)

        print("\n[4] distill again (same embedding) — reinforces, not duplicates")
        out_id2 = record_outcome(
            conn, scope=SCOPE, decision_id=dec_id,
            summary="Confirmed again on a second project", valence="failure",
            embedding=await embed("second confirmation of the EBS billing lesson"),
            matched_prediction=False,
        )
        les_id2, action2 = distill_or_reinforce_lesson(
            conn, scope=SCOPE, statement=statement, embedding=les_emb,
            derived_from_outcomes=[out_id2], conditions={"domain": "infra"},
        )
        check("second distill → reinforced", action2 == "reinforced", action2)
        check("reinforced same lesson id", les_id2 == les_id, f"{les_id2} vs {les_id}")
        lrow2 = conn.execute(
            "SELECT support_count, confidence, array_length(derived_from_outcomes,1) "
            "FROM lessons WHERE id=%s", (les_id,)
        ).fetchone()
        check("support_count bumped to 2", lrow2 and lrow2[0] == 2, lrow2)
        check("confidence increased", lrow2 and lrow2[1] > lrow[1], (lrow2, lrow))
        check("provenance accumulated (2 outcomes)", lrow2 and lrow2[2] == 2, lrow2)

        print("\n[5] retrieve_lessons — the feedback step")
        hits = retrieve_lessons(conn, scope=SCOPE, query_embedding=les_emb,
                                domain="infra", limit=5)
        check("relevant lesson retrieved", any(h["id"] == les_id for h in hits),
              [h["id"] for h in hits])
        top = next((h for h in hits if h["id"] == les_id), None)
        check("similarity is high for same-text query", top and top["similarity"] > 0.99,
              top["similarity"] if top else None)
        gated = retrieve_lessons(conn, scope=SCOPE, query_embedding=les_emb,
                                 domain="cooking", limit=5)
        check("domain gate excludes non-matching domain", all(h["id"] != les_id for h in gated),
              [h["id"] for h in gated])

        print("\n[6] mirror_lesson_to_kg — KG-mirror variant")
        ent_id = mirror_lesson_to_kg(conn, scope=SCOPE, lesson_id=les_id)
        erow = conn.execute(
            "SELECT label, properties->>'lesson_id' FROM knowledge_entities WHERE id=%s",
            (ent_id,),
        ).fetchone()
        check("Lesson entity created", erow and erow[0] == "Lesson", erow)
        check("entity back-references lesson", erow and erow[1] == les_id, erow)
        edge = conn.execute(
            "SELECT rel_type FROM knowledge_edges WHERE src_entity_id=%s AND dst_entity_id=%s",
            (ent_id, ent_target),
        ).fetchone()
        check("applies_to edge to target entity", edge and edge[0] == "applies_to", edge)

        print("\n[7] mirror again — idempotent")
        ent_id2 = mirror_lesson_to_kg(conn, scope=SCOPE, lesson_id=les_id)
        check("same entity id returned", ent_id2 == ent_id, f"{ent_id2} vs {ent_id}")
        edge_count = conn.execute(
            "SELECT count(*) FROM knowledge_edges WHERE src_entity_id=%s", (ent_id,)
        ).fetchone()[0]
        check("no duplicate edges on re-mirror", edge_count == 1, edge_count)

    finally:
        conn.rollback()  # leave no residue
        conn.close()

    print(f"\n{'='*48}\n  passed={passed}  failed={failed}\n{'='*48}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
