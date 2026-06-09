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
from datetime import datetime, timezone
from pathlib import Path

# Pool adapter (works in both venvs)
import psycopg

DSN = os.environ.get("CURLYOS_DATABASE_URL", "postgresql://curlyos:***@localhost:54321/curlyos")
SCOPE = "user:usr_hiten"
MIND_DIR = Path.home() / "mind"


class SyncPool:
    def __init__(self, d): self._dsn = d
    def connection(self): return _CtxMgr(self._dsn)
class _CtxMgr:
    def __init__(self, d): self._dsn = d; self._conn = None
    async def __aenter__(self):
        self._conn = psycopg.connect(self._dsn, autocommit=False)
        return _CW(self._conn)
    async def __aexit__(self, *a):
        if self._conn: self._conn.commit(); self._conn.close()
class _CW:
    def __init__(self, c): self._c = c
    def cursor(self): return _CC(self._c)
class _CC:
    def __init__(self, c): self._c = c
    async def __aenter__(self): return _CA(self._c.cursor())
    async def __aexit__(self, *a): pass
class _CA:
    def __init__(self, c): self._c = c
    async def execute(self, q, p=None): self._c.execute(q, p)
    async def fetchone(self): return self._c.fetchone()
    async def fetchall(self): return self._c.fetchall()


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
    """Classify a triple into: identity, fact, relationship, project, health, skill, other."""
    pred = triple.get("predicate", "").lower()
    obj = triple.get("object", "").lower()

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


async def import_triples(triples: list[dict], pool, pub, scope: str) -> dict:
    """Import triples into CurlyOS."""
    from memory.governance import record_episode, add
    from identity import propose_identity_fact
    from knowledge.graph import create_entity, create_edge

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
                    # Create subject entity
                    s_ent = await create_entity(pool, scope, subject, label="Person" if subject == "Hiten" else "Entity")
                    # Create object entity
                    o_ent = await create_entity(pool, scope, obj, label=category.capitalize())
                    # Create edge
                    await create_edge(pool, s_ent["id"], o_ent["id"], predicate,
                        properties={"confidence": confidence, "source": source_file})
                    stats["knowledge_entities"] += 2
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

    pool = SyncPool(DSN)
    from shared.events.implementations import PgOnlyPublisher
    pub = PgOnlyPublisher()

    print(f"\nImporting into CurlyOS (scope={SCOPE})...")
    stats = await import_triples(triples, pool, pub, SCOPE)

    print(f"\n{'='*60}")
    print(f"IMPORT RESULTS:")
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
