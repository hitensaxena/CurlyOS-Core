"""Import knowledge triples from ~/mind/ into CurlyOS memory + knowledge graph.

Reads the extracted triples JSON and:
  1. Creates episodes for each source file (provenance)
  2. Adds facts grounded in those episodes
  3. Creates identity_facts for high-confidence personal traits
  4. Projects entities + edges into the knowledge graph
"""
import asyncio
import json
import os
import sys
from pathlib import Path

import psycopg
import psycopg.rows
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

DSN = os.environ.get("CURLYOS_DATABASE_URL")
SCOPE = "user:usr_hiten"
MIND_DIR = Path.home() / "mind"


def normalize_predicate(pred: str) -> str:
    """Convert various predicate formats to snake_case."""
    pred = pred.strip().lower()
    pred = pred.replace(" ", "_").replace("-", "_")
    pred = pred.replace("has_", "").replace("is_", "")
    # Map common patterns
    mapping = {
        "full_name": "name",
        "age": "age",
        "nationality": "nationality",
        "location": "location",
        "role": "role",
        "occupation": "occupation",
        "diagnosis": "diagnosis",
        "vitamin_d_level": "vitamin_d",
        "liver_enzymes_alt": "liver_alt",
        "liver_enzymes_ast": "liver_ast",
        "bmi": "bmi",
        "iq": "iq",
        "preferred_editor": "prefers_editor",
        "primary_project": "primary_project",
        "creative_medium": "creative_medium",
        "music_genre": "music_genre",
        "philosophy": "philosophy",
        "spiritual_practice": "spiritual_practice",
        "relationship_status": "relationship_status",
        "friendship_pattern": "friendship_pattern",
        "communication_style": "communication_style",
        "work_hours": "work_hours",
        "sleep_time": "sleep_time",
        "wake_time": "wake_time",
        "peak_cognitive_window": "peak_cognitive_window",
        "substance_use": "substance_use",
        "medication": "medication",
        "health_condition": "health_condition",
        "skill_level": "skill_level",
        "tool_proficiency": "tool_proficiency",
        "aesthetic_style": "aesthetic_style",
        "typography_preference": "typography_preference",
        "visual_style": "visual_style",
        "emotional_pattern": "emotional_pattern",
        "cognitive_pattern": "cognitive_pattern",
        "decision_framework": "decision_framework",
        "life_phase": "life_phase",
        "project_status": "project_status",
    }
    return mapping.get(pred, pred)


def classify_triple(triple: dict) -> str:
    """Classify a triple into: identity, fact, relationship, project, health, skill, other.

    The predicate is normalised first: the sets below are written in normalised
    vocabulary (`prefers_editor`), while triples carry raw forms
    (`preferred_editor`). Comparing raw values silently classified most identity
    triples as plain facts, so they never reached identity_facts or the graph.
    """
    pred = normalize_predicate(triple.get("predicate", ""))

    identity_preds = {"name", "age", "nationality", "location", "role", "occupation",
                      "diagnosis", "relationship_status", "friendship_pattern",
                      "communication_style", "philosophy", "spiritual_practice",
                      "aesthetic_style", "visual_style", "typography_preference",
                      "emotional_pattern", "cognitive_pattern", "decision_framework",
                      "substance_use", "medication", "health_condition", "bmi", "iq",
                      "vitamin_d", "liver_alt", "liver_ast", "cholesterol", "hemoglobin",
                      "prefers_editor", "primary_project", "creative_medium", "music_genre",
                      "work_hours", "sleep_time", "wake_time", "peak_cognitive_window",
                      "exercise", "weekends", "skill_level", "tool_proficiency"}

    if pred in identity_preds:
        return "identity"
    if "project" in pred or "builds" in pred or "works_on" in pred:
        return "project"
    if "relationship" in pred or "friend" in pred or "partner" in pred or "family" in pred:
        return "relationship"
    if "health" in pred or "medical" in pred or "hospitalized" in pred:
        return "health"
    if "skill" in pred or "tool" in pred or "proficiency" in pred:
        return "skill"
    return "fact"


async def _resolve_or_create(
    pool, pub, scope: str, name: str, *, label: str = "Entity",
) -> tuple[str, bool]:
    """Reuse an existing node for this mention, or mint one.

    A bulk import re-run must not mint a second copy of every entity; without
    this the graph doubles on the second pass. `resolve_entity` matches on the
    same normalised name the graph indexes on, plus known spelling variants.

    Returns (entity_id, created).
    """
    from knowledge.graph import create_entity
    from knowledge.resolution import ResolutionDecision, resolve_entity

    decision, existing = await resolve_entity(name, scope=scope, pool=pool)
    if decision is ResolutionDecision.MERGE and existing:
        return existing, False
    created = await create_entity(pool, pub, scope, name, label=label)
    return created["id"], True


async def _ensure_edge(
    pool, pub, src_id: str, dst_id: str, rel_type: str, properties: dict,
) -> bool:
    """Create the edge only if this live (src, dst, rel_type) is not present.

    `create_edge` mints a fresh id every call, so a re-run would add a second
    copy of every relationship — exactly the duplicate edges the graph-hygiene
    janitor was built to clean up. Returns True when a row was created.
    """
    from knowledge.graph import create_edge

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM knowledge_edges WHERE src_entity_id = %s "
                "AND dst_entity_id = %s AND rel_type = %s AND valid_to IS NULL",
                (src_id, dst_id, rel_type),
            )
            if await cur.fetchone() is not None:
                return False
    await create_edge(pool, pub, src_id, dst_id, rel_type, properties=properties)
    return True


async def import_triples(triples: list[dict], pool, pub, scope: str) -> dict:
    """Import triples into CurlyOS."""
    from memory.governance import record_episode, add
    from identity import propose_identity_fact

    stats = {
        "episodes_created": 0,
        "facts_added": 0,
        "identity_facts": 0,
        "knowledge_entities": 0,
        "knowledge_edges": 0,
        "skipped_low_confidence": 0,
        "errors": 0,
    }

    # Group triples by source file for episode creation
    by_source = {}
    for t in triples:
        src = t.get("source_file", "unknown")
        by_source.setdefault(src, []).append(t)

    for source_file, file_triples in by_source.items():
        # Filter: skip very low confidence
        file_triples = [t for t in file_triples if t.get("confidence", 0.5) >= 0.5]
        if not file_triples:
            stats["skipped_low_confidence"] += 1
            continue

        # Create an episode for this source file
        content = f"Knowledge import from {source_file}: {len(file_triples)} facts"
        try:
            epi = await record_episode(pool, pub, scope, content=content, source_ref=f"mind:{source_file}")
            epi_id = epi["epi_id"]
            stats["episodes_created"] += 1
        except Exception as e:
            print(f"  ⚠️ Episode creation failed for {source_file}: {e}")
            stats["errors"] += 1
            continue

        # Process each triple
        for t in file_triples:
            try:
                subject = t.get("subject", "Hiten")
                predicate = t.get("predicate", "related_to")
                obj = t.get("object", "")
                confidence = t.get("confidence", 0.7)
                category = classify_triple(t)

                # Build a natural-language statement
                statement = f"{subject} {predicate.replace('_', ' ')} {obj}"

                # 1. Add as a memory fact (grounded in the episode)
                await add(pool, pub, scope,
                    statement=statement,
                    source_episode_id=epi_id,
                    epistemic_status="canonical" if confidence >= 0.8 else "belief")
                stats["facts_added"] += 1

                # 2. If it's an identity fact with high confidence, also add to identity_facts
                if category == "identity" and confidence >= 0.75:
                    norm_pred = normalize_predicate(predicate)
                    await propose_identity_fact(pool, pub, scope,
                        predicate=norm_pred,
                        object=obj,
                        confidence=confidence,
                        source_episode_id=epi_id)
                    stats["identity_facts"] += 1

                # 3. Project into knowledge graph (entity + edge)
                if category in ("identity", "relationship", "project", "skill"):
                    s_id, s_new = await _resolve_or_create(
                        pool, pub, scope, subject,
                        label="Person" if subject == "Hiten" else "Entity",
                    )
                    o_id, o_new = await _resolve_or_create(
                        pool, pub, scope, obj, label=category.capitalize(),
                    )
                    stats["knowledge_entities"] += int(s_new) + int(o_new)
                    if await _ensure_edge(
                        pool, pub, s_id, o_id, predicate,
                        {"confidence": confidence, "source": source_file},
                    ):
                        stats["knowledge_edges"] += 1

            except Exception as e:
                stats["errors"] += 1
                if stats["errors"] <= 5:
                    print(f"  ⚠️ Triple import error: {e} ({t.get('subject','?')} --{t.get('predicate','?')}--> {t.get('object','?')})")

    return stats


async def main():
    # Load triples
    triples_path = MIND_DIR / "knowledge_triples_dedup.json"
    if not triples_path.exists():
        # Fall back to the raw file
        triples_path = MIND_DIR / "knowledge_triples.json"

    with open(triples_path) as f:
        triples = json.load(f)

    print(f"Loaded {len(triples)} triples from {triples_path}")

    if not DSN:
        raise SystemExit("CURLYOS_DATABASE_URL is not set (checked the environment and .env)")

    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(
        DSN, min_size=1, max_size=4, open=False,
        kwargs={"row_factory": psycopg.rows.tuple_row},
    )
    await pool.open()
    from shared.events.implementations import PgOnlyPublisher
    pub = PgOnlyPublisher()

    print(f"\nImporting into CurlyOS (scope={SCOPE})...")
    try:
        stats = await import_triples(triples, pool, pub, SCOPE)
    finally:
        await pool.close()

    print(f"\n{'='*60}")
    print("IMPORT RESULTS:")
    print(f"  Episodes created:     {stats['episodes_created']}")
    print(f"  Facts added:          {stats['facts_added']}")
    print(f"  Identity facts:       {stats['identity_facts']}")
    print(f"  Knowledge entities:   {stats['knowledge_entities']}")
    print(f"  Knowledge edges:      {stats['knowledge_edges']}")
    print(f"  Skipped (low conf):   {stats['skipped_low_confidence']}")
    print(f"  Errors:               {stats['errors']}")
    print(f"{'='*60}")

    # Verify
    conn = psycopg.connect(DSN, autocommit=True)
    for t in ["episodes", "memories", "identity_facts", "knowledge_entities", "knowledge_edges", "events"]:
        try:
            c = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            print(f"  {t}: {c} rows")
        except Exception:
            print(f"  {t}: table missing")
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
