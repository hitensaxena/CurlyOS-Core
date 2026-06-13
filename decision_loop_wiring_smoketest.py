"""Decision-loop *wiring* smoketest — exercises the live async integration:

    goals.record_decision (prediction capture)
      → goals.review_decision (structured outcome + Brier surprise + lesson + KG-mirror)
        → cognition.decision_loop.retrieve_lessons_async (the hydration feedback read)

Runs the real goals functions against a single async connection wrapped in a
tiny no-commit "pool" shim, then rolls back — so it tests the integration with
zero residue and without standing up the full pool/publisher/embedder infra.

    cd ~/curlyos-core
    .venv/bin/python3 decision_loop_wiring_smoketest.py
"""

import asyncio
import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from goals import record_decision, review_decision
from cognition.decision_loop import retrieve_lessons_async
from shared.embeddings.implementations import HashEmbedder
from shared.types.ulid import mint

SCOPE = "test:decision_loop_wiring"

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


class _SharedConnPool:
    """Hands every `async with pool.connection()` the same open connection and
    never commits/closes it — so the whole test rolls back as one transaction."""

    def __init__(self, conn):
        self._conn = conn

    def connection(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self_):
                return conn

            async def __aexit__(self_, *exc):
                return False  # no commit, no close

        return _Ctx()


class _NoopPublisher:
    async def stage(self, ev, conn):
        return None


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
    embedder = HashEmbedder()
    conn = await psycopg.AsyncConnection.connect(_dsn(), autocommit=False)
    pool = _SharedConnPool(conn)
    pub = _NoopPublisher()
    try:
        # Seed a KG entity the lesson can attach to.
        ent_target = mint("ent")
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO knowledge_entities (id, scope, name, label) "
                "VALUES (%s, %s, %s, 'Project')",
                (ent_target, SCOPE, "Self-hosted EC2 box"),
            )

        print("\n[1] record_decision captures the prediction")
        dec = await record_decision(
            pool, pub, SCOPE,
            title="Run CurlyOS on a single EC2 box",
            chosen="single m7i-flex.large",
            rationale="cheapest path to always-on Postgres+Redis",
            reversibility="reversible",
            predicted_outcome="stays cheap and low-maintenance",
            prediction_confidence=0.8,
        )
        dec_id = dec["id"]
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT predicted_outcome, prediction_confidence FROM decisions WHERE id=%s",
                (dec_id,),
            )
            drow = await cur.fetchone()
        check("prediction persisted", drow and drow[0] and abs(drow[1] - 0.8) < 1e-9, drow)

        print("\n[2] review_decision → structured outcome + lesson + KG-mirror")
        res = await review_decision(
            pool, pub, SCOPE, dec_id,
            outcome="Box cost ~$58/mo; had to stop it and delete the volume",
            valence="failure",
            matched_prediction=False,
            lesson="A stopped EC2's EBS still bills; prefer scale-to-zero (Neon) for personal infra.",
            applies_to_entities=[ent_target],
            embedder=embedder,
        )
        check("outcome recorded", bool(res.get("outcome_id")), res)
        check("lesson created", res.get("lesson_action") == "created", res)
        check("lesson mirrored to KG", bool(res.get("lesson_entity_id")), res)

        async with conn.cursor() as cur:
            await cur.execute("SELECT surprise, valence FROM outcomes WHERE id=%s",
                              (res["outcome_id"],))
            orow = await cur.fetchone()
        check("surprise = Brier(0.8, miss) = 0.64", orow and abs(orow[0] - 0.64) < 1e-9,
              orow[0] if orow else None)

        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT outcome_id, reviewed_at FROM decisions WHERE id=%s", (dec_id,))
            drow2 = await cur.fetchone()
        check("decision.outcome_id backfilled", drow2 and drow2[0] == res["outcome_id"], drow2)
        check("decision.reviewed_at stamped", drow2 and drow2[1] is not None)

        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT label FROM knowledge_entities WHERE id=%s", (res["lesson_entity_id"],))
            erow = await cur.fetchone()
            await cur.execute(
                "SELECT rel_type FROM knowledge_edges WHERE src_entity_id=%s AND dst_entity_id=%s",
                (res["lesson_entity_id"], ent_target))
            edge = await cur.fetchone()
        check("Lesson entity in KG", erow and erow[0] == "Lesson", erow)
        check("applies_to edge to target entity", edge and edge[0] == "applies_to", edge)

        print("\n[3] hydration feedback: retrieve_lessons_async finds it")
        q = (await embedder.embed([
            "A stopped EC2's EBS still bills; prefer scale-to-zero (Neon) for personal infra."]))[0]
        hits = await retrieve_lessons_async(conn, scope=SCOPE, query_embedding=q, limit=5)
        check("lesson retrieved for relevant query", any(h["id"] == res["lesson_id"] for h in hits),
              [h["id"] for h in hits])
        top = next((h for h in hits if h["id"] == res["lesson_id"]), None)
        check("similarity high for same-text query", top and top["similarity"] > 0.99,
              top["similarity"] if top else None)

    finally:
        await conn.rollback()  # leave no residue
        await conn.close()

    print(f"\n{'='*48}\n  passed={passed}  failed={failed}\n{'='*48}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
