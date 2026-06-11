"""Local zero-shot entity typing via bge-m3 (no API, no rate limits).

Classifies an entity by cosine similarity between its STORED embedding and a
per-label prototype embedding. Two modes:
  default     — VALIDATE: predict labels for entities the LLM already typed and
                report agreement (is the local method good enough?).
  APPLY=1     — write predicted labels onto untyped entities (label Entity/Other).

    set -a; . ./.env; set +a
    .venv/bin/python deploy/classify_entities_local.py        # validate
    APPLY=1 .venv/bin/python deploy/classify_entities_local.py # apply to untyped
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg

DSN = os.environ["CURLYOS_DATABASE_URL"]
SCOPE = os.environ.get("CURLYOS_SCOPE", "user:usr_hiten")
APPLY = os.environ.get("APPLY") == "1"

# Rich prototype text per label — the entity is assigned the nearest one.
PROTOTYPES = {
    "Person": "a specific named individual person, a human being's name",
    "Organization": "a company, startup, school, team, brand, or institution",
    "Project": "a software project, product, app, or initiative being built",
    "Tool": "a software tool, app, programming language, library, framework, device or technology",
    "Skill": "a skill, ability, competency, craft, or area of expertise",
    "Concept": "an abstract concept, idea, theory, field of study, emotion, or philosophical topic",
    "Place": "a physical location, city, country, region, building, or room",
    "Event": "a specific event, meeting, trip, or happening at a point in time",
    "Health": "a medical or health metric, measurement, biomarker, condition, or symptom",
    "Media": "a book, song, album, film, game, artwork, creative work, or an artist",
    "Activity": "a hobby, practice, routine, exercise, sport, or recreational activity",
    "Other": "a miscellaneous thing that does not fit any specific category",
}


def log(m): print(m, flush=True)


def cosine(a, b):
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def main():
    from shared.embeddings.implementations import LocalBgeM3
    import asyncio
    embedder = LocalBgeM3()
    labels = list(PROTOTYPES)
    protos = asyncio.run(embedder.embed(list(PROTOTYPES.values())))
    log(f"embedded {len(protos)} label prototypes")

    conn = psycopg.connect(DSN, autocommit=True)
    if APPLY:
        where = "label IN ('Entity', 'Other')"
        log("APPLY mode: typing untyped entities")
    else:
        where = "label NOT IN ('Entity', 'Other')"
        log("VALIDATE mode: predicting over LLM-typed entities")
    rows = conn.execute(
        f"SELECT id, name, label, embedding::text FROM knowledge_entities "
        f"WHERE scope = %s AND valid_to IS NULL AND embedding IS NOT NULL AND {where}",
        [SCOPE],
    ).fetchall()
    log(f"entities: {len(rows)}")

    agree = 0
    applied = 0
    confusion = {}
    for eid, name, cur_label, emb_text in rows:
        vec = json.loads(emb_text)
        pred = max(labels, key=lambda i: cosine(vec, protos[labels.index(i)]))
        if APPLY:
            conn.execute("UPDATE knowledge_entities SET label = %s WHERE id = %s", [pred, eid])
            applied += 1
        else:
            if pred == cur_label:
                agree += 1
            else:
                confusion[(cur_label, pred)] = confusion.get((cur_label, pred), 0) + 1
    conn.close()

    if APPLY:
        log(f"DONE: {applied} entities typed locally")
    else:
        pct = 100.0 * agree / len(rows) if rows else 0
        log(f"AGREEMENT with LLM labels: {agree}/{len(rows)} = {pct:.1f}%")
        log("top disagreements (llm -> predicted):")
        for (a, b), c in sorted(confusion.items(), key=lambda x: -x[1])[:12]:
            log(f"  {a} -> {b}: {c}")


if __name__ == "__main__":
    main()
